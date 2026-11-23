# Scenario declarations

## 场景库目录

| 目录 | 内容 |
| --- | --- |
| `lib/l1`、`lib/l3` | Python 场景声明和共享构建函数 |
| `lib/l{1,2,3,4}/configs` | 对应级别的场景源 JSON 与区域配置，目前只有 L3 有配置 |
| `lib/data/weather` | 天气序列和来源记录 |
| `lib/baselines/l3` | 用于验证场景构建的参考轨迹摘要 |
| `historical` | 历史场景的源文件加载、天气归档、编译和恢复校验 |

离线运行、对照验收和 L1/L2 切片放在 [experiments](../experiments/README.md)。
历史场景的冻结输入由 `ScenarioSpec` 校验。源布局调整会改变运行产物的
源码指纹。旧产物使用生成时的源码快照严格回放。

`spec.py` defines shared ScenarioSpec. `season.py` supplies the initial full-season
composition: cultivar process parameters, field subdivisions and resource limits.
These are declarative inputs, not engine objects or mutable environment state.

`registry.py` explicitly lists runnable L1 and L3 factories. `lib/l1` and `lib/l3`
contain task definitions with initial conditions, fixed weather and exposed
provider bindings. Difficulty levels never select a different execution framework.
No reference runtime, prompt library or Oracle workflow is constructed here.

## 0.6.0 normal与阶段干旱组合

[构建合同](CONSTRUCTION.md)定义独立normal、冻结参考日历、降雨扰动组合及公开/审计边界。
新增四个L3入口共享同一运行时：normal、R1干旱、R5干旱、R1＋R5组合。
`lib/l3/normal_validation.py`是显式离线参考/对照验证程序，不是场景声明工厂；只有它会创建验证引擎。
新版历史天气场景、数据来源和 L1/L2 切片见 [哈尔滨历史天气试点](HISTORICAL_PILOT.md)。
