# Native biology backend

`backend.py` implements the pilot's simplified root-zone water balance: rainfall, effective irrigation, runoff, drainage, evaporation, water-stressed transpiration and storage change. Development uses accumulated temperature with fixed representative canopy parameters. Biomass, final yield, nitrogen, pests and disease are not modeled.

The adapter owns full numerical state and supports direct state restoration. It receives completed-day inputs from the runtime and projects public soil observations. See the parent process audit for parameters, equations and validation limits.

## Full season

`season.py` adapts the original FAIRY soil and canopy kernels to the shared L3
Runtime. See [migration and validation](MIGRATION.md) for the current process,
compatibility and test results. [SEASON_AUDIT.md](SEASON_AUDIT.md) describes the
previous reduced model. L1 stays unchanged.
