# Native biology backend

`backend.py` implements the pilot's simplified root-zone water balance: rainfall, effective irrigation, runoff, drainage, evaporation, water-stressed transpiration and storage change. Development uses accumulated temperature with fixed representative canopy parameters. Biomass, final yield, nitrogen, pests and disease are not modeled.

The adapter owns full numerical state and supports direct state restoration. It receives completed-day inputs from the runtime and projects public soil observations. See the parent process audit for parameters, equations and validation limits.

## Full season

`season.py` is a separate capability-selected Native process used by the shared
Runtime for L3. The L1 `backend.py` stays unchanged. See `SEASON_AUDIT.md` for
source parameters, equations, simplifications and limits. Neither model imports
legacy runtime code. L3 is not validated field-yield prediction.
