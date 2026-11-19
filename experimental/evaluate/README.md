# Evaluate

场景负载放在现有 `scenarios`，分项指标放在 `metrics`。L2/L3/L4 和 custom 目录保留，不根据规模预先绑定 Multi-Agent。

## 本轮场景

`soybean_l1_irrigation_v1`：1000 m²单地块，5月1日播种，5月9日07:00进入任务，截止5月10日07:00，时区 `Asia/Shanghai`，灌溉上限45 mm。天气全为明确的合成日序列；无随机抽样，seed=0仅作为未来随机实验的身份字段。

场景定义农场、作物、天气、预运行管理、工具、额度和目标。后端在运行时选择，不维护 `_V2` 等后端专属场景副本。Native 与 WOFOST 的预运行结果和合适灌溉剂量允许不同。

成功须同时满足：

1. 灌溉前有公共土壤观测。
2. 灌溉后至少跨过一个当地日期，并有复查观测。
3. 无待结算灌溉，真实终态根区含水率在0.25–0.32 m³/m³。

仅达到含水率目标而没有操作／复查链，不计任务成功。该 L1 不要求收获或最终产量。

## 指标

| 字段 | 口径 |
| --- | --- |
| `task_success` | 上述任务条件同时成立 |
| `required_queries_completed` | 声明要求的天气查询均在首次灌溉前成功；原场景无此要求时为 true |
| `target_reached` | 真实终态含水率在目标区间 |
| `observed_before_irrigation` | 第一次灌溉前有观测 |
| `observed_after_settlement` | 最后一次灌溉日期之后有复查 |
| `root_vwc` | 后端真实根区含水率，仅用于评测／审计 |
| `water_used_mm/m3` | 已占用的毛灌溉水量，包含尚未结算的登记量 |
| `tool_errors` | 工具参数／能力／预算拒绝次数 |
| `replay_verified` | 默认 null；严格回放通过时为 true，失败命令直接报错 |

成功不等于最优，任务失败不等于回放失败。没有迁入历史产量缺口、经济收益或流程综合分。

## Ontology migration fixtures

ScenarioSpec now declares objects, operation_bindings, public_weather, weather_in_context and required_queries in addition to the existing physical inputs. It never constructs a backend. Built-in CLI fixtures are soybean_l1_irrigation_v1 (three operations) and soybean_l1_weather_irrigation_v1 (five).

Public weather is separate from process forcing. Only explicitly allowed operation schemas enter model context. For the weather fixture, required_queries_completed checks successful required queries before the first irrigation; task_success requires that condition in addition to the established hydration checks. The original scene requires no weather queries. Query requirements are declared rather than inferred from scenario IDs.

## Unified L3 task evaluation

`scenarios/registry.py` registers both L1 and L3. ScenarioSpec.task_type selects
`metrics.evaluate`, shared by CLI, artifact generation and strict replay. L3
requires all 64 ridges planted/harvested, positive recovered grain safely stored,
empty equipment containers, conserved dry matter/water, quota and deadline
compliance, and no pending irrigation. Stored mass is reported as a simulated
outcome, never a yield score. No FOS, potential-yield gap or detail/library mode.

Oracle dose-training adapters and independent reference qualification live in [oracle/](oracle/README.md). They reuse scenario declarations, Runtime operations and the existing task evaluator; they do not modify normal inputs or add a potential-yield score.
