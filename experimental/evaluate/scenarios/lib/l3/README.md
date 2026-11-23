# L3 full-season scenarios

Declarative task inputs only; no engine construction or legacy App registration.
`soybean_full_season.py` reconstructs the selected 64-ridge, HEINONG58, 4 mm quota
scenario from an unplanted field to safe storage. No detail/library context or
Oracle workflow is embedded in the task prompt. `weather.json` is fixed synthetic
forcing with source/hash in the Native process audit. Public forecast access is
limited to the declared weather service; full future forcing is not in context.

## 历史场景源 JSON

集成 L3 的输入是 [harbin_integrated_1995.json](configs/harbin_integrated_1995.json)，
烘干机故障的输入是 [harbin_dryer_failure_2016.json](configs/harbin_dryer_failure_2016.json)。
对应的 `build-integrated-season` 和 `build-dryer-failure` 直接读取这两份文件。

| 字段 | 含义 |
| --- | --- |
| `space`、`objects` | 地块几何和设备布局、仓储约束。当前布局使用等宽垄。 |
| `initial_state` | 起始日期、土壤含水率、作物建植状态和物料库存。布局仍负责初始化设备。 |
| `evolution.calendar` | 绑定历史天气归档。分析窗口用于核查暴露，天气数值来自归档。 |
| `evolution.crop_stage` | 预留给根据运行时物候触发的扰动。本版遇到非空声明会拒绝加载。 |
| `evolution.equipment` | 作业触发事件。示例在第一批由该烘干机烘干的粮食入库后锁定设备。 |
| `task` | 结束日期和灌溉额度。 |

加载时校验声明，绑定天气归档的产品、格点和指纹，生成原有格式的
`l3.json`。引擎继续从冻结的 `l3.json` 执行，L1/L2 继续继承母场景和
检查点。当前只有这两个入口使用该源格式。实际物候、操作结果和事件发生
时刻由运行生成。后续扩展需要为阶段触发规则、空间分区和各类实体状态另行
定义字段与验证。

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

离线验证：`python -m evaluate.experiments.validation.drought`。
它通过公共操作建植、推进、收获与入库，检查目标阶段覆盖、实际低墒情和固定灌溉的正向入库干物质响应，
并对两条轨迹严格回放。该固定干预是因果工程探针，不是最优剂量、真实产量校准或默认 Agent 指令。
