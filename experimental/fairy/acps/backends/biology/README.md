# Soil-water and crop backends

`contracts.py` defines WeatherDay, BackendCapabilities, SoilObservation and BiologyBackend, plus explicit Native/WOFOST construction and restore entry points. Initialization uses the same declarative farm, crop, weather and management prefix. Each adapter owns its process state.

- [Native](native/README.md): simplified root-zone water balance and temperature-driven development.
- [WOFOST](wofost/README.md): independent PCSE model with pinned offline cultivar data.

One `advance_day` consumes one completed local day's weather and queued irrigation. Observation does not integrate processes. The runtime determines when day boundaries are crossed and saves pending water; adapters do not advance the public simulation clock. Units and public observation semantics are shared, numerical trajectories need not match.

Detailed equations, historical sources, assumptions and exclusions are in [PROCESS_AUDIT.md](PROCESS_AUDIT.md).

## Explicit backend registration

BiologyBackendRegistry is a frozen table of backend names and factories. Built-in Native and WOFOST factories import only the selected optional engine. A new backend supplies the same observe/advance_day/audit/export_state/load_state interface; construction validates its identity and methods, and runtime checks crop, timestep and required actions before pre-running.

This adopts explicit definition/implementation binding without introducing device-key priority or fallback. The selected backend owns its complete process state for the session. Public irrigation is a domain operation: quota checks and queued water belong to runtime, while its eventual physical input is consumed by the registered biology adapter. One operation need not equal one engine call. For full-season L3, explicit season configuration selects the Native spatial process and Native physical ledger. This composition has additional planting/harvest methods; it does not imply PCSE supports them. Unsupported combinations fail, without fallback.

## Full season (0.8.0)

`SeasonBiologyBackend` adds immutable `RidgeStatus` management views, planting,
spatial irrigation, harvest projection/removal and observations. Consumers do
not inspect engine-specific dictionaries. Native HEINONG58 and explicitly named
WOFOST Soybean_901 proxy scenarios share this interface; they retain independent
processes, parameters and training weights. [WOFOST process notes](wofost/README.md).
