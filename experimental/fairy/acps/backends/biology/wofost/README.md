# WOFOST biology backend

The adapter independently integrates pinned PCSE 6.0.13 `Wofost81_WLP_CWB`.
WOFOST owns soil water, growth and phenology; Native equations are never applied on top.
Public units are m², mm, m³, volumetric moisture and °C; PCSE conversions remain inside the adapter.

## Supported modes

- `backend.py`: existing single-plot L1, preserving its fixed initialization/warmup.
- `season.py`: 64 independent soil/crop columns, actual planting, local irrigation, maturity and one-time grain removal.
- Full-season selection requires an explicit `Soybean_901_proxy` scenario. HEINONG58 Native scenarios are rejected rather than relabelled or silently substituted.

`soybean.yaml` is pinned offline upstream cultivar data. `provenance.json` retains source,
commit, SHA-256 and attribution. `Soybean_901` was parameterized for Northern France;
this integration is an uncalibrated engineering proxy, not a Chinese cultivar calibration.

## Processes and deliberate limits

| Process | Ownership and decision |
| --- | --- |
| Bare soil before planting | PCSE starts at task origin; planting does not reset soil history |
| Water and growth | PCSE only; rainfall, effective irrigation, evaporation, transpiration and losses audited daily |
| Spatial variation | Independent ridge columns; optional soil capacity, rain capture and application efficiency; no lateral transport |
| Planting | Public equipment completion sends crop-start at the current engine day; declared nominal density is recorded but no unvalidated density response is added |
| Phenology | PCSE emerging/vegetative/reproductive/mature semantics; no invented mapping to soybean R1–R8 |
| Maturity | Finalize and delete crop at its integration boundary; freeze mature dry grain; soil continues, with no post-harvest canopy transpiration |
| Harvest | Convert TWSO kg/ha to gross dry kg using ridge area; remove once; physical backend applies recovery and records losses |
| Grain moisture | Engineering proxy: initial 0.30; at mature day settlement, `clamp(m - 0.018 + min(0.04, rain_mm * 0.003), 0.10, 0.35)`; not a WOFOST output or calibrated drying model |
| Drone observations | Deterministic synthetic LAI/water projections; unavailable thermal values are explicit; no yield/biomass disclosure |
| Drying and storage | Existing shared equipment/material backend; separate dry and water stocks |
| Nutrients, pests, lodging | Not added by this water-limited integration |

Shared `SeasonSpec` still serializes Native growth coefficients (GDD thresholds,
maximum LAI, RUE and grain partition) for scenario compatibility. WOFOST does
not use those coefficients: its phenology and growth come from the pinned
cultivar YAML. Likewise Native canopy/base-temperature shortcuts in `SoybeanSpec`
do not replace WOFOST parameters. Farm soil/geometry, hydraulic zones, planting
geometry and physical resource constraints retain their declared shared roles.

### Non-cultivar engineering parameters

Soil limits and initial moisture come from `FarmSpec`; `root_depth_m` configures
maximum soil rooting depth in PCSE, not a claim that its instantaneous root depth
matches Native. The adapter fixes CO₂ at 420 ppm, CRAIRC at 0.06, SOPE/KSUB/K0 at
5 cm/day, and SSI/SSMAX/NOTINF/IFUNRN at zero. These are explicit pilot assumptions,
not measured site parameters. E0, ES0 and ET0 are all supplied from the fixture's
single declared ET0; no station-specific reference-evaporation calibration is claimed.
Declared sowing depth and nominal plant spacing are operation/seed-use constraints;
this adapter does not invent density- or depth-dependent crop responses.

## Clock, persistence and performance

Runtime owns the business clock (`Asia/Shanghai`); engines are advanced once per completed
local day. PCSE rate evaluation is deferred until actual current-day weather and irrigation
are known. In full-season mode, crop finalization is completed at the maturity boundary,
not postponed until another day's rates are calculated. Engine private hooks are isolated
in the pinned adapter and covered by regression tests.

Restoration builds fresh engines and reapplies ordered planting, irrigation, daily forcing
and harvest inputs. It checks parameter identity, reconstructed summaries, public operation
history and daily ledgers. This costs O(ridges × history); it is not constant-time snapshot
loading. Full replay also re-executes the shared runtime without LLM or adviser inference.
Observations remain pure. Rejected operations and engine faults restore all columns and
physical ledgers. No lower layer imports the runtime or evaluation module.

## Reproduce

From the repository root, install `./experimental[wofost,oracle,llm,test]` and run:

```bash
.venv/bin/python -m interface.cli run --backend wofost --scenario soybean_l3_901_proxy_normal_v1
.venv/bin/python -m evaluate.scenarios.lib.l3.wofost_validation --out .runs/wofost-validation-new
.venv/bin/python -m interface.cli.oracle train --backend wofost --timesteps 2048 --out .runs/wofost-policy-new.zip
.venv/bin/python -m interface.cli.oracle evaluate --policy .runs/wofost-policy-new.zip --out .runs/wofost-evaluation-new
```

Output paths must be new. A changed process/calendar is rejected: intentionally rebuild the
WOFOST reference with `python -m evaluate.scenarios.lib.l3.wofost_validation --build-reference`
and revalidate paired probes. Never rebuild it automatically per candidate.

See [integration evidence](../../../../../demo/WOFOST_FULL_SEASON.md) for actual results and
[process audit](../PROCESS_AUDIT.md) for historical source review.
