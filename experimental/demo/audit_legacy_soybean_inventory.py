"""Build the complete static soybean scenario inventory from a pinned Git tree."""

import argparse
import ast
from collections import Counter
import csv
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

REV = "1e71d132a80c2819180ba805b95cb4e8d7cd29c2"
ROOT = "fairy/scenarios"
BATCH = ROOT + "/farm_world_fullseason_v2"
CATALOG = BATCH + "/harbin_l3_batch_catalog.py"
PROFILE = "fairy/physics/profiles.py"
GROUPS = {
    "baseline_farm_world": "L1",
    "farm_world_physics": "L1",
    "farm_worldpp_physics": "L2",
    "farm_world_fullseason": "L3",
    "farm_world_fullseason_v2": "L3",
}

CAPABILITIES = {
    "冷种床/物候": r"cold|wetcold|heat_stress|cool_august|coolwet|low_radiation|lateplant|planting_window|短季|低温|冷春|冷湿|低辐射|热害",
    "病害": r"fungicide|disease|rust|病害|病压|杀菌",
    "虫害": r"insect|aphid|leaf_feeder|虫害|虫压|蚜虫|食叶",
    "杂草": r"herbicide|weed|杂草|草害|除草",
    "养分": r"fertiliz|fertigation|nutrient|fertility|potassium|micronutrient|nodulation|养分|缺钾|施肥|基肥|追肥",
    "缺苗/补种": r"replant|skip_rows|stand_gap|crusting|compacted_headland|补种|漏播|缺苗|结皮",
    "分区品种/播期": r"staggered|three_cultivar|early_vs_standard|hn84_hn58|hn50_hn84|错期|多品种|分区品种",
    "观测/设施故障": r"sensor_failure|breakdown|aeration_failure|故障",
    "设施/资源策略": r"capacity|budget|waterlimit|water_limit|quota|fuel_limit|power_limit|harvester_days|allowed_inputs|low.carbon|market_discount|resource_limit|仓容|容量|预算|额度|限额|燃油",
    "水力/墒情": r"irrigat|dry_spell|drought|dryr5|dry_patch|fastdrain|hydraulic|poordrain|trafficability|停雨|缺水|干旱|灌溉|排水|通行",
    "收获/收储": r"harvest|drydown|store|grain_moisture|shattering|收获|收储|入库|粮食",
}

GAPS = {
    "冷种床/物候": "需土温门槛/品种物候，天气本身不足",
    "病害": "缺病害演化、症状与处置残效",
    "虫害": "缺虫压、叶损、复查和处置",
    "杂草": "缺草害竞争与除草反馈",
    "养分": "缺养分状态和施肥效果",
    "缺苗/补种": "缺建苗异常、局部补种和苗龄分区",
    "分区品种/播期": "需保留分区品种或播种日，独立校验成熟窗口",
    "观测/设施故障": "缺故障触发、退化/恢复或替代证据",
    "设施/资源策略": "按库存/设备合同表达，现有容量与水额只覆盖部分规则",
    "水力/墒情": "已有天气、分区水力和用水账本；核对参数粒度和结算时点",
    "收获/收储": "已有作业与账本；核对成熟、湿度、批量与时限",
}

NON_PHASE = {"缺苗/补种", "观测/设施故障", "设施/资源策略", "分区品种/播期"}
ACTION = re.compile(
    r"^build_.*events$|^_?(?:irrigat|apply_?fert|fertiliz|spray|apply_?pestic|treat|"
    r"replant|plant|level|form_ridge|scout|harvest|unload|dry_grain|store_grain|"
    r"advance_time|read_soil|survey|capture|measure|inspect|apply_prior|configure_).*"
)


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args])


def assignments(tree):
    result = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            result.update({t.id: node.value for t in node.targets if isinstance(t, ast.Name)})
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
            result[node.target.id] = node.value
    return result


def call_name(node):
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute):
            return node.func.attr
    return ""


def expression(node):
    if node is None:
        return ""
    if isinstance(node, ast.Constant):
        return str(node.value)
    return ast.unparse(node)


def compact(value):
    return " ".join(value.split())


def snapshot(repo, paths):
    """Read immutable source/checkpoint blobs in one Git process."""
    data = git(repo, "archive", "--format=tar", REV, *paths)
    import tarfile

    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        return {
            m.name: archive.extractfile(m).read().decode("utf-8")
            for m in archive.getmembers()
            if m.isfile()
        }


def read_profiles(tree):
    profiles = {}
    for node in ast.walk(tree):
        if call_name(node) == "add_l3_profile" and node.args:
            profiles[expression(node.args[0])] = {kw.arg: kw.value for kw in node.keywords}
            if (
                len(node.args) > 1
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value >= 1700
            ):
                profiles[expression(node.args[0])]["common_window"] = ast.Constant(
                    "另叠加 >=1700 系列共用作业天气（见 add_l3_profile）"
                )
        elif isinstance(node, ast.Assign) and call_name(node.value) == "PhysicsProfile":
            for target in node.targets:
                if isinstance(target, ast.Subscript) and expression(target.value) == "profiles":
                    key = expression(target.slice)
                    profiles[key] = {kw.arg: kw.value for kw in node.value.keywords}
    return profiles


def profile_events(profiles, key, seen=frozenset()):
    if key == "harbin_baseline_2026_seed_101" and key not in profiles:
        key = "_BASELINE_NAME"
    data = profiles.get(key, {})
    if key in seen:
        return ""
    inherited = []
    for field in ("events", "weather_events", "outbreaks", "biotic_outbreaks"):
        node = data.get(field)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Subscript):
            parent = expression(node.value.slice)
            details = profile_events(profiles, parent, seen | {key})
            if details:
                inherited.append(f"引用配置 {parent}: {details}")
    own = [
        f"{label}={compact(expression(data[field]))}"
        for field, label in (
            ("climate", "气候"),
            ("monthly_climate", "气候"),
            ("events", "天气"),
            ("weather_events", "天气"),
            ("outbreaks", "病虫"),
            ("biotic_outbreaks", "病虫"),
            ("common_window", "共享窗口"),
        )
        if field in data
    ]
    return "；".join(own + inherited)


def proposal(level, scenario, stage, windows, has_weather, parent):
    """Keep inferred anchors separate from measured stage and reference actions."""
    if parent:
        return "以快照实际分区阶段切片；" + (
            "从行动就绪点开始" if level == "L1" else "从待判断/等待的窗口开始"
        )
    if "tangyan5" in scenario:
        return "按原工作簿/观测日期保留播种、出苗、追肥、成熟和收获"
    if level != "L3":
        if "postharvest" in scenario:
            return "收获后：生成湿粮库存和烘干/仓储条件"
        if stage:
            return "以原版所列初始阶段重生成状态"
        return "播前/未播种：按任务形成整地、种床或作业就绪状态"
    matches = [
        (r"coldspring|cold_spring|wetcold|preplant|planting_window", "播前/播种：种床与作业窗口"),
        (
            r"emergence|skip_rows|replant|crusting|stand_gap|compacted|establishment",
            "播后/出苗：检查建苗与缺苗",
        ),
        (r"flowering|wetjune|wet_june", "营养后期至开花：核对湿期/养分暴露"),
        (r"dryr5|r5_|pod.fill|dry_august|dry_patch|drought", "R5/R6 附近：核对灌浆期扰动"),
        (r"late.?rain|harvest|storage|dryer|moisture_delivery", "R8/收后：核对收获和收储窗口"),
    ]
    hints = [value for pattern, value in matches if re.search(pattern, scenario, re.I)]
    if windows:
        hints.append("参考作业窗口见独立列（不能直接当作扰动起点）")
    if has_weather:
        hints.append("天气按源日期/相对日重建，再逐区校验实际暴露阶段")
    return "；".join(hints) or "未声明独立阶段事件；按初态/约束和全季基线运行"


def classify(evidence, *, calibrated=False, checkpoint=False, extra_constraints=False):
    themes = [key for key, pattern in CAPABILITIES.items() if re.search(pattern, evidence, re.I)]
    if calibrated:
        return "实测/表格基准，不可等价重采样", "需原气象、品种/小区和标定口径", "；".join(themes)
    gaps = "；".join(GAPS[key] for key in themes)
    if checkpoint:
        verdict = "可切片；须由新版前缀重建状态"
    elif extra_constraints or NON_PHASE.intersection(themes):
        verdict = "仅阶段扰动不足；须增加初态/资源/作业规则"
    elif themes:
        verdict = "可按窗口定义；需匹配过程与操作"
    else:
        verdict = "基线/作业起点；无需额外生育扰动"
    return verdict, gaps, "；".join(themes)


def build_inventory(repo):
    names = git(repo, "ls-tree", "-r", "--name-only", REV, ROOT, PROFILE).decode().splitlines()
    sources = snapshot(repo, [p for p in names if p.endswith(".py")])
    trees = {p: ast.parse(body) for p, body in sources.items()}
    variables = {p: assignments(tree) for p, tree in trees.items()}

    def resolve(path, node, seen=frozenset()):
        if isinstance(node, ast.Attribute) and expression(node.value) == "SPEC":
            spec = variables[path].get("SPEC")
            if call_name(spec) == "get_spec":
                return expression(catalog[resolve(path, spec.args[0], seen)][node.attr])
        if not isinstance(node, ast.Name) or (path, node.id) in seen:
            return expression(node)
        next_seen = seen | {(path, node.id)}
        if node.id in variables[path]:
            return resolve(path, variables[path][node.id], next_seen)
        for item in trees[path].body:
            if not isinstance(item, ast.ImportFrom):
                continue
            for alias in item.names:
                if (alias.asname or alias.name) != node.id:
                    continue
                base = path.split("/")[: -item.level] if item.level else []
                parts = item.module.split(".") if item.module else []
                other = "/".join(base + parts) + ".py"
                if other in variables and alias.name in variables[other]:
                    return resolve(other, variables[other][alias.name], next_seen)
        return expression(node)

    catalog = {}
    for node in ast.walk(trees[CATALOG]):
        if call_name(node) == "ScenarioSpec":
            kw = {k.arg: k.value for k in node.keywords}
            catalog[expression(kw["slug"])] = kw
    profiles = read_profiles(trees[PROFILE])
    rows = []
    anchors = {}
    for path, tree in sorted(trees.items()):
        group = path.split("/")[2] if path.startswith(ROOT + "/") else ""
        if group not in GROUPS:
            continue
        module_vars = variables[path]
        for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
            attrs = assignments(cls)
            register = [d for d in cls.decorator_list if call_name(d) == "register_scenario"]
            scenario = resolve(path, register[0].args[0] if register else attrs.get("scenario_id"))
            if not scenario:
                continue
            level = (
                "L1"
                if scenario.startswith("scenario_l1_")
                else ("L2" if scenario.startswith("scenario_l2_") else GROUPS[group])
            )
            parent = resolve(path, attrs.get("source_l3_scenario_id"))
            label = resolve(path, attrs.get("source_checkpoint_label"))
            spec_expr = module_vars.get("SPEC")
            spec = (
                catalog.get(resolve(path, spec_expr.args[0]), {})
                if call_name(spec_expr) == "get_spec"
                else {}
            )
            profile = expression(spec.get("profile_name"))
            if not profile:
                profile = next(
                    (
                        resolve(path, kw.value)
                        for node in ast.walk(tree)
                        if isinstance(node, ast.Call)
                        for kw in node.keywords
                        if kw.arg == "profile_name"
                    ),
                    "",
                )
            if not profile:
                profile = resolve(path, module_vars.get("PROFILE_NAME"))
            description = expression(spec.get("description")) or compact(
                ast.get_docstring(cls) or ""
            )
            init_conditions = []
            for node in ast.walk(cls):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Attribute) and target.attr in {
                            "growth_stage",
                            "soil_vwc",
                            "water_stress",
                            "grain_moisture_pct",
                            "nutrient_level",
                            "pest_pressure",
                            "disease_pressure",
                            "weed_pressure",
                        }:
                            init_conditions.append(compact(expression(node)))
            dynamic_inputs = "；".join(
                f"{k}={compact(expression(spec[k]))}"
                for k in (
                    "planting_zones",
                    "initial_vwc",
                    "prior_histories",
                    "custom_histories",
                    "hydraulic_modifiers",
                    "management_regime",
                    "postharvest_market",
                    "enforce_planting_windows",
                    "harvest_zones",
                    "harvest_zone_waits",
                    "postharvest_drying_zones",
                )
                if k in spec
            )
            weather = profile_events(profiles, profile)
            action_windows = (
                "；".join(
                    compact(expression(a))
                    for a in ast.walk(spec["actions"])
                    if call_name(a) == "ScenarioAction"
                )
                if "actions" in spec
                else ""
            )
            actions = ", ".join(
                sorted(
                    {
                        name
                        for node in ast.walk(cls)
                        if (name := call_name(node)) and ACTION.match(name)
                    }
                )
            )
            if spec and not actions:
                actions = "build_batch_events（全季标准流程；定向作业见参考窗口）"
            anchor = resolve(path, attrs.get("source_growth_stage")) or "；".join(
                dict.fromkeys(s for s in init_conditions if ".growth_stage" in s)
            )
            date = resolve(path, attrs.get("source_checkpoint_date")) or expression(
                spec.get("start_date")
            )
            extra = bool(
                set(spec)
                & {
                    "management_regime",
                    "postharvest_market",
                    "prior_histories",
                    "custom_histories",
                    "planting_zones",
                    "hydraulic_modifiers",
                }
            )
            verdict, gap, themes = classify(
                " ".join(
                    [
                        scenario,
                        description,
                        weather,
                        action_windows,
                        dynamic_inputs,
                        *init_conditions,
                    ]
                ),
                calibrated="tangyan5" in scenario,
                checkpoint=bool(parent),
                extra_constraints=extra,
            )
            if (
                not parent
                and group in {"baseline_farm_world", "farm_world_physics"}
                and re.search(
                    r"field_prep_plus|rain-stop|battery-constrained|drone_survey_plus|hopper|window closes",
                    scenario + " " + description,
                    re.I,
                )
            ):
                verdict = "仅阶段扰动不足；需作业环境、装备状态或子日事件"
                gap = (gap + "；" if gap else "") + "核对装备、作业窗口和原子操作粒度"
            row = dict(
                level=level,
                scenario_id=scenario,
                family=group,
                source_path=path,
                source_line=cls.lineno,
                parent_l3=parent,
                checkpoint=label,
                source_date=date,
                observed_stage=anchor,
                variety=expression(spec.get("primary_seed")),
                profile=profile,
                disturbance=weather,
                initial_or_resource_rules=dynamic_inputs
                or "；".join(dict.fromkeys(init_conditions)),
                reference_windows=action_windows,
                task_or_tool=actions,
                description=description,
                migration=verdict,
                gaps=gap or "核验新版公开操作和任务起点",
                themes=themes,
                proposed_anchor=proposal(
                    level, scenario, anchor, action_windows, bool(weather), parent
                ),
            )
            rows.append(row)
            if label:
                candidate = str(PurePosixPath(path).parent / "checkpoints" / (label + ".json"))
                if candidate in names:
                    anchors[scenario] = candidate
    table_path = next(p for p in trees if p.endswith("/scenario_tangyan5_table_fitted.py"))
    for plot in ast.literal_eval(variables[table_path]["TANGYAN5_TABLE_PLOT_IDS"]):
        row = dict.fromkeys(rows[0], "")
        row.update(
            level="L3",
            scenario_id=f"scenario_tangyan5_table_fitted_{plot.lower()}",
            family="farm_world_fullseason",
            source_path=table_path,
            source_line=variables[table_path]["TANGYAN5_TABLE_PLOT_IDS"].lineno,
            profile="tangyan5_table_fitted_2025",
            variety=plot.split("_", 1)[1],
            description=f"原工作簿小区 {plot}：日期或密度组，按小区回放与对照实测。",
            proposed_anchor="按原工作簿时间轴；不随机替换处理和天气",
            initial_or_resource_rules=f"工作簿小区 {plot} 的播期、密度、养分和田间数据",
            migration="实测/表格基准，不可等价重采样",
            gaps="需原工作簿、实际气象、标定参数和收获评价；不能由随机阶段天气等价重建。",
        )
        rows.append(row)

    checkpoints = snapshot(repo, sorted(set(anchors.values()))) if anchors else {}
    by_id = {row["scenario_id"]: row for row in rows}
    for row in rows:
        path = anchors.get(row["scenario_id"])
        if not path:
            continue
        checkpoint = json.loads(checkpoints[path])
        row["source_date"] = checkpoint["farm_world_state"].get("sim_date") or row["source_date"]
        row["description"] = (
            row["description"]
            or f"由 {row['parent_l3']} 的 {row['checkpoint']} 切片；操作见独立列。"
        )
        if not row["disturbance"]:
            row["disturbance"] = f"继承母场景 {row['parent_l3']}，从该日期继续；不重播此前扰动"
        if not row["variety"]:
            row["variety"] = by_id[row["parent_l3"]]["variety"]
        states = (checkpoint.get("physics_state") or {}).get("phenology_states", {})
        if states:
            counts = Counter(s["stage"] for s in states.values())
            row["observed_stage"] = " / ".join(
                f"{stage}×{n}" for stage, n in sorted(counts.items())
            )
    registered = [r["scenario_id"] for r in rows]
    missing_parents = {
        r["parent_l3"] for r in rows if r["parent_l3"] and r["parent_l3"] not in registered
    }
    if missing_parents:
        raise ValueError(f"Unresolved parent L3 IDs: {missing_parents}")
    splits = [r for r in rows if r["parent_l3"]]
    if any(r["scenario_id"] not in anchors for r in splits):
        raise ValueError("A declared derivative checkpoint was not located in the pinned tree")
    if len(rows) != len({row["scenario_id"] for row in rows}):
        raise ValueError("Duplicate registered scenario IDs")
    return sorted(rows, key=lambda r: (r["level"], r["family"], r["scenario_id"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    rows = build_inventory(args.repository)
    with args.output.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(Counter((row["level"], row["family"]) for row in rows))
    print("Total:", len(rows))


if __name__ == "__main__":
    main()
