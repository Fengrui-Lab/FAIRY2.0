# FAIRY2.0 开发交接

交接基线：`kun` / `b7a46cb`（FAIRY `0.8.2`，Runtime schema 7，数值策略 schema 2，特征版本 2）。该代码快照已推送到 [个人私有仓库](https://github.com/StarryNight2953/FAIRY2.0)，默认分支为 `kun`。继续在 `kun` 开发；原组织公开仓库和 `main` 不作为实验代码的推送目标。

## 在另一台设备接手

用有权访问私有仓库的 GitHub 账号克隆。以下命令适用于 macOS/Linux，从仓库根目录执行：

```bash
git clone --branch kun --single-branch https://github.com/StarryNight2953/FAIRY2.0.git
cd FAIRY2.0
git rev-parse --short HEAD
python3 -m venv .venv
.venv/bin/python -m pip install -e './experimental[wofost,llm,oracle,test,typing]'
.venv/bin/python -m pytest -q --disable-warnings
.venv/bin/python -m mypy --config-file experimental/pyproject.toml
.venv/bin/python -m interface.cli run --backend native
```

完整验证环境使用 Python 3.14、PCSE 6.0.13；换 Python 版本后应重新跑验证。仅验证 Native 可安装 `./experimental[test]`；完整检查需要相应额外依赖。IDE 选择项目虚拟环境，若仍无法解析导入，可将 `./experimental` 加入分析路径。克隆包含代码、测试、受版本控制的场景数据和技术说明；`reference/`、`.runs/`、`.venv/` 不随仓库传输。历史源码可按 [参考版本索引](reference-baseline.json) 单独准备，正常运行不依赖它。真实模型服务需要另行配置；本地脚本验收、模拟模型测试和严格回放不需要真实模型凭证。

## 已落地的结构和边界

| 路径 | 职责与接手入口 |
| --- | --- |
| `fairy/acps/ontology` | 农场、作物、装备、设施、服务等对象及其公共操作。被动资源不必成为 Agent 可调用项，旧版 App 不作为新运行时分组。 |
| `fairy/acps/backends` | `biology` 对接 Native / WOFOST 土壤与作物过程；`physical` 负责装备作业及收储账本。同一个场景的公共操作映射到各后端，由适配器负责单位转换。不支持的能力显式拒绝。 |
| `fairy/events` | 统一仿真时钟、操作注册、调度、事件、恢复与严格回放；业务时区 `Asia/Shanghai`。L1 和 L3 使用同一套运行入口、事件与权限边界，按任务使用不同能力。 |
| `fairy/agents` | 脚本 Oracle 与 LLM 通过公共操作执行。Oracle 是可读完整状态的特权参考；LLM 只接收任务、工具契约和公开上下文。 |
| `evaluate`、`interface/cli`、`demo` | 场景与指标、运行与评价入口、回归测试及实验记录。 |

L1 覆盖单地块大豆观测、灌溉、推进到次日与复查。L3 已接通全季作业、收获与入库。HEINONG58 场景以 normal 为基线，组合 R1、R5 单阶段和双阶段干旱；洪涝和虫害未接入。新增场景应复用标准流程并声明扰动，避免为每种组合复制全季流程。WOFOST 全季使用代理品种 Soybean_901，不能解释为黑农58校准结果。

## 验证结果与解释边界

- **0.8.2 代码快照**：干净检出 396 项测试通过，68 个生产源文件通过 mypy；四组 Native/WOFOST 前后对照的指标、阶段日期和结束时间一致。类型和注释强化覆盖后端协议、工厂、公开观测、状态、Agent 上下文及评价；动态 JSON 和第三方接口仍有明确的动态边界。IDE 对抽象后端可先跳到协议，再用“转到实现”找具体适配器。详见 [类型与导航](TYPING.md)。
- **场景验收**：normal、R1、R5 和组合场景已有构建、对照、恢复及严格回放验证。[场景构建合同](evaluate/scenarios/CONSTRUCTION.md) 和 [首轮验收](demo/SCENARIO_CONSTRUCTION.md) 记录基线来源、扰动定位与运行方法。严格回放在新实例重执行操作，不重新调用 LLM；WOFOST 恢复依赖固定初始化和输入日志，成本随历史增长。
- **WOFOST 生长审计**：相同参数和输入的六组探针与原版 PCSE 对照一致。normal 是合成雨量约 3 mm/日、ET0 约 3 mm/日的无水分胁迫条件，额外灌溉未提高原始籽粒产量；生殖期长停雨时，零灌溉与适时补水存在明显差异。当前实例采用水分限制、潜在氮供应的模型，不能据此检验施肥收益；氧胁迫未启用。见 [生长敏感性审计](demo/GROWTH_SENSITIVITY_AUDIT.md)。
- **历史实验（0.8.1）**：Qwen3.6-35B-A3B-FP8 联合 128 步冒烟 PPO 在 WOFOST normal 完成 64 垄收获、入库及严格回放；消耗 60 mm，安全入库干粮约 6530.05 kg，与同场景零灌溉和脚本 Oracle 一致。运行有 8 次工具错误，三次 HTTP 500 后靠重建已执行前缀续跑。该结果证明工程闭环和任务结果，不能视为无错完成、农艺最优或 0.8.2 已重新进行真实 LLM 验收。
- **自动 Oracle / PPO**：原型将生命周期知识、操作模板和数值建议分层。2048 步 PPO 在长停雨案例提前耗尽水额，在既定扣水成本下输给零灌溉；等量、改时机的对照恢复了大量入库干粮。主要排查方向是训练只见 normal 变体、旱期来临前的时序决策和奖励设置。脚本 Oracle 与 PPO 的空间动作粒度也不同。现有结果不支持 PPO 优于脚本或已找到全局最优。见 [自动 Oracle 实施](demo/AUTO_ORACLE_IMPLEMENTATION.md) 和 [真实 LLM 与 PPO 对照](demo/WOFOST_REAL_LLM_COMPARISON.md)。

指标解读时区分模型原始产量、收获回收损失与实际安全入库量；不能用模型生物产量替代任务结果。任务成功、水耗、工具错误和严格回放分别报告。旧版的 `detail=true`、`detail=library` 等机制是审计线索，后续迁移不应因旧版存在就恢复。

## 建议继续处理的事项

1. **先修 Native 的可复现边界。** 供水和光温充分时，`ET0=0` 会因零潜在蒸腾分支把生长胁迫系数置零，导致零生物量和零籽粒。用对照测试明确预期后再改。播深和密度目前不改变生长结果，应明确能力边界，待有依据再建模。
2. **把作物响应做成可区分的验收。** 同天气、同初态下比较零灌溉、固定水额不同时间与脚本 Oracle，分别记录胁迫、储水平衡、原始产量、回收和入库。WOFOST normal 适合做无胁迫对照；用旱期场景检验水分干预，别把 normal 的产量平局当作引擎失灵。
3. **完善恢复和模型对照。** 测试暂时性 HTTP 500 后可从合法前缀恢复，目前历史试验使用人工重建流程；同时检查收获容量拒绝及错过阶段窗口。待这些边界明确后，再用同一版本、任务、水额和评价口径比较真实 LLM、脚本和自动候选。工程闭环与农艺证据链分别报告。
4. **扩展前先重验基线。** 修改打包代码会改变源指纹；历史轨迹和策略应使用匹配的冻结版本，不能改指纹强行回放。对来源绑定的场景阶段日历和知识资产，先跑生成器、确认行为或内容差异，再更新来源记录。

## 导航与需要另行迁移的证据

从 [代码审查指导书](CODE_REVIEW_GUIDE.md) 的 M00–M14 检查表开始，结合 [HSCC 教训与设计准则](HSCC失败教训与FAIRY2.0设计准则.md) 和 [修复报告](demo/HSCC_FIXES.md) 核对历史问题。参考 WOFOSTGym 的目的、动作编码和奖励分析见 [WOFOSTGym 总结](WOFOSTGym分析总结.md)。

如要在另一台机器独立检查历史证据，需从原机器单独复制选定产物：`.runs/typing_upgrade_20261117/` 保存类型检查、回归、数值对照和 0.8.2 轮包；`.runs/wofost_followup_081/` 保存 WOFOST 原始轨迹、失败及续跑、对照和策略诊断；`.runs/wofost_081_wheel/` 保存匹配的冻结轮包。先按各报告核对清单、版本与指纹，不需要把整个运行目录纳入 Git。
