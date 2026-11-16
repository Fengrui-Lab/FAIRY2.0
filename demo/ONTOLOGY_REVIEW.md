# Initial ontology migration review — 2026-11-15

## Scope and verification

The initial increment implements explicit object loading, operation definitions and provider bindings, registered biology factories, root-zone soil observation, mm-based irrigation and separately declared weather reports. The original three-operation L1 remains the numerical regression fixture; a five-operation weather L1 validates the new interface. No legacy runtime imports, physical engine, equipment jobs or old App compatibility are included.

Two independent subagents reviewed the implementation: an architecture reviewer inspected loading, contracts, state ownership, privacy and recovery; an acceptance reviewer authored `test_ontology.py` independently of production code. The architecture reviewer reported no remaining must-fix issue within this pilot after corrections.

| Finding | Correction | Evidence |
| --- | --- | --- |
| Replay discovered binding changes only after execution | Verify schema and binding fingerprint before warmup/replayed operations; restore verifies before loading process history | Early-drift rejection regression |
| Weather task could succeed without weather queries | Scenario declares required_queries; evaluator requires successful calls before first irrigation | Missing, invalid, late and valid query controls |
| Invalid contract declarations produced inconsistent schemas | Validate Parameter, OperationDefinition and OperationBinding at construction | Invalid type/name/bounds/timing/version/mutable capability tests |

Complete suite: **141 passed, 0 failed, 0 skipped** (68 existing pilot tests plus 73 ontology acceptance cases). Both PCSE and OpenAI optional packages were installed. Upstream PCSE/traitlets deprecation warnings remain; warning folding does not suppress test execution. Actual LLM HTTP services were not called. The new five-operation flow was tested with a mock LLM on both backends.

CLI Oracle runs and strict replay passed on both backends. Native uses 45 mm and ends at VWC 0.27082331975213964; WOFOST uses 16 mm and ends at VWC 0.287184933871243. These preserve the original numerical results and are engineering checks, not field calibration or optimality evidence.

The 0.3 wheel also ran both backends and strict replay from an extracted package outside the repository, confirming packaged ontology modules and offline WOFOST data are sufficient.

## Known boundaries

- One plot, one explicitly bound provider per public operation; no dynamic plugin discovery.
- Passive plot loading is implemented; materials/inventory and physical equipment are deferred.
- Root-zone deterministic observation is not the old six-zone 5 cm noisy probe model.
- Daily synthetic reports are not live instantaneous weather; perfect forecasts are a fixture choice.
- Business rejection can rebuild WOFOST history under the shared rollback boundary.
- FAIRY 0.3/runtime state schema 2 rejects old 0.2 artifacts; regenerate them.

## Reviewed historical sources

Primary reference: FAIRY test redirect snapshot `59ceb7e7c02a728909d522dc4b5dda752a9fe1b3`. The following hashes identify source bytes reviewed for this increment; new implementations are rewritten and do not import them.

| Relative path in reference snapshot | SHA-256 |
| --- | --- |
| `fairy/apps/app.py` | `95510c801d9a41d98427229a21609330861c0c7e48ce1d221fbbd52aa67ddfa1` |
| `fairy/tool_utils.py` | `29f6c654be567048e631aa58f53f202362181953b53f7e6cab470f8d87582b7f` |
| `fairy/apps/farm_world/sensor_app.py` | `07f95fa00d9d429399c5ad8beb0e3fe4b61a069200b8225079580dd380bbf9fd` |
| `fairy/apps/farm_world/weather_app.py` | `aab328236ec1455d9d94866b9126b2cdaab67162a870812f4cea3834c344b2d6` |
| `fairy/apps/farm_world/field_ops_app.py` | `03b3b39fd69f48d832787b1f82d39b2c2a1eda0d2d2336fd52801e2e47295c1d` |
