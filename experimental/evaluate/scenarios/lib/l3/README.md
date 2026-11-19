# L3 full-season scenarios

Declarative task inputs only; no engine construction or legacy App registration.
`soybean_full_season.py` reconstructs the selected 64-ridge, HEINONG58, 4 mm quota
scenario from an unplanted field to safe storage. No detail/library context or
Oracle workflow is embedded in the task prompt. `weather.json` is fixed synthetic
forcing with source/hash in the Native process audit. Public forecast access is
limited to the declared weather service; full future forcing is not in context.

This is a new reduced process implementation, not numerical or agronomic
equivalence to the historical scenario. Nutrient and biotic management are not
implemented. The same CLI and Runtime used by L1 execute this scenario.
# 0.5.0 的固定干旱挑战

当前 `soybean_full_season()` 和 CLI `soybean_l3_hn58_waterlimit_v2` 使用
`weather_v2.json`。历史 `weather.json` 未修改，v1 入口保留为历史工程对照。
v2 是新的合成场景，不能冒充原版数值/农艺等价移植。

固定变换仅将7月15—31日降雨设为0，其余字段保持来源 trace 不变。
来源和派生哈希、固定参考播期及变换记录在 `weather_v2_provenance.json`。
在该参考播期，17天落在R5/R6。对照与处理使用完全相同的冻结天气，不随候选策略移动事件。

离线验证：`python -m evaluate.scenarios.lib.l3.validation`。
它通过公共操作建植、推进、收获与入库，检查目标阶段覆盖、实际低墒情和固定灌溉的正向入库干物质响应，
并对两条轨迹严格回放。该固定干预是因果工程探针，不是最优剂量、真实产量校准或默认 Agent 指令。
