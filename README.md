# FAIRY 2.0 · 统一 L1 / L3 骨架

基础场景为**大豆单地块补水**，另有天气查询与 Ontology 加载验证场景。两个场景均在 Native 和 WOFOST 后端运行。基础场景的公开工具为 `observe_soil`、`irrigate(depth_mm)`、`advance_time(hours)`。先观测、灌溉、推进到次日，再复查。L3 另外接入了重写的装备作业、合成观测和物料账本。

## 迁移参考基线

主要参考用户指定的 [FAIRY/test](https://github.com/Fengrui-Lab/FAIRY/tree/test)。2026-11-13 核对时，GitHub 将该分支接口重定向到 `test_history_window`，本地参考副本为 `reference/FAIRY-test/`，固定提交 `59ceb7e7c02a728909d522dc4b5dda752a9fe1b3`。身份和复核文件记录在 [reference-baseline.json](reference-baseline.json)。

后续工具、场景、环境、Agent 循环和 Oracle 的移植均优先对照这份原始实现。原有 `FAIRY-jev`、`FAIRY-visualization`、`FAIRY-multiobjective` 仅作历史辅助，具体扩展确有需要时再单独审查；WOFOST 参数继续以其固定上游来源为准。原分支仍包含多套大豆场景，当前新骨架包含补水 L1、天气 L1，以及重建的 64 垄全季 L3；历史 L3 独立副本的通过结果不计入新骨架验收。

切换参考不改变已实现模型的数值行为，也不把此前参考扩展分支的实现重新归因为原始分支；两者的来源与差异见过程说明。

## 安装与复现

Python ≥3.10。Native 本体只使用标准库；WOFOST、LLM、测试依赖分别可选。

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[wofost,llm,test]'
.venv/bin/python -m interface.cli run --backend native --out .runs/native_oracle.json
.venv/bin/python -m interface.cli run --backend wofost --out .runs/wofost_oracle.json
.venv/bin/python -m interface.cli replay .runs/native_oracle.json
.venv/bin/python -m interface.cli replay .runs/wofost_oracle.json
.venv/bin/python -m pytest -q --disable-warnings
```

安装后也可以用 `fairy run ...`。仅需要 Native 时使用 `pip install -e '.[test]'`。`.runs/` 是忽略的本地输出目录；重跑命令会替换指定产物。

## 目录与依赖

ACPS 按 `ontology` 与 `backends/{physical,biology}` 组织，无新增顶层业务目录。验收测试置于已有 `demo/`。

| 位置 | 本轮职责 |
| --- | --- |
| `fairy/acps/ontology/farm` | 农场、单地块尺寸和土壤参数 |
| `fairy/acps/ontology/crops/lib` | 大豆参数 |
| `fairy/acps/ontology` | 对象规格、操作契约与农业操作提供者 |
| `fairy/acps/backends/biology/{native,wofost}` | 独立的生物／水分过程适配器 |
| `fairy/events` | 唯一时钟、事件、待处理输入、恢复和回放 |
| `fairy/agents` | 特权参考策略、公共上下文、模型适配和循环 |
| `evaluate` | 声明式 L1 / L3 场景与按任务分派的指标 |
| `interface/cli` | 运行、恢复、查看、回放、评价入口 |

场景不构造引擎，工具不直接写引擎私有状态，模型不获得运行时对象。`web`、`jev`、`kg`、各 `custom`、L2/L4 目录原样保留，暂无实现。

## 阅读顺序

1. [物理／生物过程迁移说明](fairy/acps/backends/biology/PROCESS_AUDIT.md)：历史来源、保留和省略的过程、方程与参数变化、科学边界。
2. [运行时语义](fairy/events/README.md)：时间、事件、权限、恢复与回放。
3. [评测说明](evaluate/README.md)：成功条件与指标。
4. [复现实验](demo/README.md)：本轮结果和验证命令。
5. [Agent 接入](fairy/agents/README.md) 与 [CLI](interface/README.md)。

本轮验证架构和水分操作闭环，不构成田间校准或大豆产量模型验证。历史场景 ID、Oracle 产物和评分均不兼容新骨架。运行及新骨架测试不读取 `reference/`；独立的历史审计测试使用明确指定的参考来源。

## Ontology 初步移植（0.3）

场景显式声明被动地块、土壤观测资源、灌溉资源和可选天气服务；运行时加载对象、检查引用、绑定后端并仅开放指定操作。Native/WOFOST 通过显式工厂注册接入。原 L1 保留三个操作；`--scenario soybean_l1_weather_irrigation_v1` 增加天气和预报查询，公开数据与引擎 forcing 分开。

当前 0.4.1 恢复状态为 schema 4，校验对象／操作／生物与物理后端绑定指纹。旧产物仅能 inspect，运行／回放需重新生成。L3 已接入装备作业和物料账本；Native 全季过程为未校准的简化重建，不代表原版全部农艺能力。迁移范围与后续批次见 [过程审计](fairy/acps/backends/biology/PROCESS_AUDIT.md)。

## Shared full-season L3 (0.4)

```bash
python -m interface.cli run --scenario soybean_l3_hn58_waterlimit_v1 --backend native --agent oracle --out .runs/l3.json
python -m interface.cli replay .runs/l3.json
python -m interface.cli evaluate .runs/l3.json
```

L1 and L3 use the same Runtime, event schema, operation registry, agent loop, CLI,
artifact and replay implementation. The task chooses a declared evaluator and
trusted reference strategy. No runtime imports or executes `reference/` or the
legacy validation copy. Default task context has no detail/library/expert prompt.

L3 models 64 spatial soil/crop units, sowing, equipment work, observation,
area-accounted irrigation, harvest, unloading, drying and safe storage. Grain
comes from a reduced radiation-driven process, never a fixed harvest fixture.
It is **not calibrated HEINONG58 yield prediction**. Nutrient/pesticide processes,
full legacy agronomic equivalence, and WOFOST L3 are not implemented. Requesting
WOFOST L3 fails explicitly; Native and WOFOST L1 remain available.

See [new-runtime validation and limitations](demo/L3_NEW_RUNTIME.md) and
[full-season process audit](fairy/acps/backends/biology/native/SEASON_AUDIT.md).

当前版本全方位审阅、修复项与未覆盖能力见 [版本审阅报告](demo/CURRENT_VERSION_REVIEW.md)。
