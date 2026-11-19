# Agricultural ontology

Ontology contains domain definitions, passive objects and operation providers. Public operations are explicit declarations; importing a module never registers an executable object. Types, instances and exposed operations are separate.

| Directory | Scope | Pilot implementation |
| --- | --- | --- |
| `farm` | Plot identity, geometry and declared soil configuration | Passive FarmSpec referenced by plot ObjectSpec |
| `crops` | Crop/cultivar definitions and model parameters | SoybeanSpec |
| `equipment` | Drones, tractors, robots and implements | L3 tractor, multispectral/thermal drone and robot operation providers |
| `infrastructure` | Fixed installations and observation systems | SoilSensor, irrigation, grain facility and charging contracts |
| `service` | External information/operational interfaces | Declared daily WeatherService |
| `human` | Workers and work teams | Reserved |
| `material` | Material definitions, batches, located stocks | L3 seed/fuel/grain ledger; general material objects remain future work |

`contracts.py` defines immutable ObjectSpec, OperationBinding, Parameter and OperationDefinition. Definitions own argument/result fields and time semantics. Parameter declarations generate the model schema and validate runtime requests. The supported schema subset has required fields only, numeric bounds and primitive/object/array types; nested weather data comes from validated WeatherDay declarations. OperationRequest retains the pilot JSON field `tool`; it names an operation, not a domain object. OperationResult is detached at the runtime boundary.

ObjectSpec contains an ID, an approved type ID and an optional target link. The pilot explicitly declares its plot and providers targeting that plot. The loader rejects duplicate/reserved IDs, unknown types, missing targets and invalid provider-operation pairs. Passive plots need no callable interface. The single-plot pilot binds each exposed operation to exactly one provider; multiple providers of one public operation need a future explicit routing extension, not a silent overwrite.

Provider contracts belong to their domain directories. Equipment/infra execution is delegated to explicit backends by Runtime. Only the scenario's ordered operation bindings become executable or visible to the model. Runtime-owned `advance_time` is declared in `fairy/events/operations.py`; it has no fabricated agricultural provider. The implementation does not import historical Apps, decorators or scenario scripts.

Objects do not own the public clock or duplicate backend water/growth state. Sensors read the currently bound backend's public projection; irrigation prepares a receipt while runtime owns budget/pending water settlement; weather reads separate declared public reports. See [runtime loading](../../events/README.md) and [migration audit](../backends/biology/PROCESS_AUDIT.md).
