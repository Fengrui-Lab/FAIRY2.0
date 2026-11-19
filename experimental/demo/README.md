# 大豆 L1 验收与复现

当前版本审阅与验收见 [CURRENT_VERSION_REVIEW.md](CURRENT_VERSION_REVIEW.md)。
下文按日期保留历史测试与实验结果，不代表当前版本测试总数。

在仓库根目录执行 README 中的安装命令，然后运行：

```bash
.venv/bin/python -m pytest -q --disable-warnings
.venv/bin/python -m interface.cli run --backend native --out .runs/native_oracle.json
.venv/bin/python -m interface.cli run --backend wofost --out .runs/wofost_oracle.json
.venv/bin/python -m interface.cli replay .runs/native_oracle.json
.venv/bin/python -m interface.cli replay .runs/wofost_oracle.json
```

2026-11-10 首轮实跑：Python 3.14.6，PCSE 6.0.13，pytest 9.2.0。OpenAI SDK 2.54.0用于模拟客户端接口验证。测试时 PCSE 的 traitlets 兼容层产生弃用警告，`--disable-warnings` 只折叠输出，不改变测试结果。

最终验收：**68 passed，0 failed，0 skipped**。另已构建 wheel，在仓库外的临时目录中直接使用打包内容完成两后端 Oracle 与严格回放；验证离线参数已随包携带。CLI 中间状态恢复、继续执行与再次回放也通过。实际 SDK 的 HTTP 请求使用本地 MockTransport 验证，没有访问模型服务。

2026-11-13 目录迁移复验：将子进程测试和文档示例中的旧 `fairy.runtime` 导入更新为 `fairy.events`，完整测试再次通过：**68 passed，0 failed，0 skipped**，包括 Native／WOFOST 跨进程恢复和 Native 无可选依赖运行。

| 后端 | 初始真实含水率 | Oracle 毛灌溉 | 终态真实含水率 | 任务／严格回放 |
| --- | --- | --- | --- | --- |
| Native | 0.1742500671 | 45 mm / 45 m³ | 0.2708233198 | 通过／通过 |
| WOFOST | 0.1883923589 | 16 mm / 16 m³ | 0.2871849339 | 通过／通过 |

这是固定合成输入的工程结果。两种后端根系深度、蒸散和水分过程不同，不能由剂量差判断谁更接近真实农业生产。Oracle 网格搜索以目标区间中点为目标，不是最少水量策略。

`test_pilot.py` 覆盖：正常闭环和无灌溉对照、非法参数／超额拒绝、只读观测、午夜单次积分、分段与多日推进、毛水量与有效水量、守恒、待结算水跨进程恢复、恢复后的单次结算、异常回滚、版本／参数指纹不兼容、回放篡改检测、公开上下文隔离与裁剪、模拟模型工具调用、错误／步数预算、无可选依赖的 Native 运行、CLI 和纯日志查看。

无 PCSE 时，Native 与模拟 LLM 相关测试仍运行，WOFOST 测试会明确 skip；完整验收要求安装 wofost extra 并检查无相关 skip。

真实 LLM 未在本轮调用：没有读取用户既有凭证或向付费服务发送请求。接入已经通过模拟工具调用验证；自行按 `fairy/agents/README.md` 配置后可实跑，产物记录模型、调用次数和用量，应单独报告真实任务成功率。

## 手动保存中间状态

```python
from evaluate.scenarios.lib.l1.soybean_irrigation import soybean_irrigation
from fairy.acps.ontology.contracts import OperationRequest
from fairy.events.session import Runtime
from fairy.events.artifacts import artifact, write_json

r = Runtime(soybean_irrigation(), 'native')
r.invoke(OperationRequest('observe_soil'))
r.invoke(OperationRequest('irrigate', {'depth_mm': 40}))
write_json('.runs/pending.json', artifact(r))
```

再用 `python -m interface.cli resume .runs/pending.json --out .runs/resumed.json` 在新实例中继续，或通过 Python API 直接加载状态。无需再次调用 LLM。

## Ontology loading and weather migration (0.3)

Run the additional fixture with either installed biology backend:

```bash
.venv/bin/python -m interface.cli run --scenario soybean_l1_weather_irrigation_v1 --backend native --out .runs/ontology_native.json
.venv/bin/python -m interface.cli run --scenario soybean_l1_weather_irrigation_v1 --backend wofost --out .runs/ontology_wofost.json
.venv/bin/python -m interface.cli replay .runs/ontology_native.json
.venv/bin/python -m interface.cli replay .runs/ontology_wofost.json
```

The default scenario still exposes three operations. The weather fixture exposes five and requires successful daily-report and forecast queries before irrigation. Objects are explicitly declared, including a passive plot. Domain providers use the selected backend through the existing process interface; the runtime provides advance_time.

FAIRY 0.3 / runtime schema 2 includes object/operation binding fingerprints; regenerate earlier pilot artifacts. New regressions are in `test_ontology.py`. No real LLM request is necessary; the controller can be checked using the same mock client approach as the original pilot.

2026-11-15 initial migration verification: **141 passed, 0 failed, 0 skipped**. Independent review findings, corrections and historical source hashes are recorded in [ONTOLOGY_REVIEW.md](ONTOLOGY_REVIEW.md).

## Original L3-mini and supplementary branch audit

The subsequent original-test real-model run is separate from the new L1 implementation. See [LEGACY_L3_VALIDATION.md](LEGACY_L3_VALIDATION.md) for Oracle, Qwen3.6 and replay results, including failed strict replay checks. [KNOWLEDGE_BRANCH_REVIEW.md](KNOWLEDGE_BRANCH_REVIEW.md) records the pinned `test_Knowledge` reference and its independently reviewed migration boundaries.

`audit_legacy_l3.py` reads historical artifacts without importing the reference implementation or calling a model. Its five focused tests bring the current project regression total to **146 passed**; the supplementary branch's 48 tests / 42 subtests are a separate reference-only verification.

The subsequent [isolated determinism fixes](LEGACY_L3_DETERMINISM.md) include a reproducible patch, 78 reference-copy smoke/determinism tests plus 10 replay-contract tests, and an exact comparison mode for the artifact audit. The original reference remains unchanged. Patched Oracle and real-model runs both pass exact cross-process replay and necessary task-outcome gates; independent review confirms completion within the user's engineering/outcome scope. Current-project regressions now total **147 passed**.

## Shared-runtime L3

See [L3_NEW_RUNTIME.md](L3_NEW_RUNTIME.md) for the new CLI/Runtime implementation,
its independent tests, default unassisted context and limitations. Historical
`LEGACY_L3_*` documents are reference-only and do not constitute migration acceptance.

0.6.0四场景真实服务测试与失败分析见[HEINONG58真实LLM验证](HN58_REAL_LLM_VALIDATION.md)。

自动Oracle模块、有限候选生成及消融复现见[AUTO_ORACLE_IMPLEMENTATION.md](AUTO_ORACLE_IMPLEMENTATION.md)。
可使用 `python experimental/demo/oracle_mvp.py --help` 查看端到端实验入口；它不会在pytest中自动调用真实模型。

自动Oracle的实际训练、真实LLM候选与失败修复证据见[验收记录](AUTO_ORACLE_VALIDATION.md)。

WOFOST 0.8.1真实LLM全季完成、零灌溉/脚本Oracle对照及PPO时机诊断见[专项报告](WOFOST_REAL_LLM_COMPARISON.md)。
