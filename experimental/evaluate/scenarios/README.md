# Scenario declarations

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
