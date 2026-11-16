# Scenario declarations

`spec.py` defines shared ScenarioSpec. `season.py` supplies the initial full-season
composition: cultivar process parameters, field subdivisions and resource limits.
These are declarative inputs, not engine objects or mutable environment state.

`registry.py` explicitly lists runnable L1 and L3 factories. `lib/l1` and `lib/l3`
contain task definitions with initial conditions, fixed weather and exposed
provider bindings. Difficulty levels never select a different execution framework.
No reference runtime, prompt library or Oracle workflow is constructed here.
