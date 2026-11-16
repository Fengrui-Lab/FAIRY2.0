# Reduced full-season Native process audit

## Identity and scope

This implementation reconstructs the selected `test` L3 task on the shared new
Runtime. It is not a wrapper around App, a copy of the historical orchestrator,
or a claim of numerical/agronomic equivalence. L1 retains its existing bucket
model; `ScenarioSpec.season` explicitly selects the spatial season model for
Native. Unsupported WOFOST full-season requests fail before warmup.

Sources inspected: FAIRY-test commit
`59ceb7e7c02a728909d522dc4b5dda752a9fe1b3`, and supplementary test_Knowledge
`6da01b058a1504ef0b888cdc8c76bc92d0b8f793`. Individual source/data SHA-256 values
are recorded in `season_provenance.json`. Runtime never loads those sources.

## Process decisions

| Historical process/source | New decision and implementation | Limit |
| --- | --- | --- |
| Selected dry R5/R6 HEINONG58 scenario | Preserve 64 ridges, 268 m length, 1.1 m ridge width, seed spacing 7.9 cm, depth 4 cm, 4 mm field-average water quota, unplanted start and 170-day deadline | Reconstructed task ID distinguishes new process behavior |
| Profile weather generator / profiles.py | Export a fixed 171-day synthetic trace once, preserving seed 1202 and historical weather events; package and hash it | This is generated weather, not historical station observations |
| Hydraulic modifiers | Explicit scenario zone 22–43: FC .285, rain capture .82, irrigation efficiency .90; other ridges use FarmSpec | Single daily root bucket per ridge; old two-layer drainage/infiltration not copied |
| Old hourly irrigation behavior | Queue depth mm per selected ridge, calculate field-average quota and volume, settle once at midnight | Irrigation is an instantaneous request, not a simulated pump job |
| Native water balance | Rain, effective irrigation, saturation runoff, drainage to FC, exposed-soil evaporation, water-stressed transpiration | ET0 estimated as max(.5, .15 × solar MJ/m²); vapor pressure fixed at 12 hPa; engineering weather conversions, not calibrated evapotranspiration |
| HEINONG58 thermal parameters | Emergence 90 GDD, maturity 1120 GDD from historical proxy; temperature base 8°C from new crop spec | No photoperiod, chilling, emergence mortality or detailed temperature response; stage dates may differ from the historical model |
| Canopy growth | LAI starts at .1 after emergence, expands logistically at .059/day up to 4.95, then senesces after 83% thermal progress | Fixed engineering stage fractions; no cultivar calibration |
| Radiation-driven dry matter | PAR=.48 solar, interception=1−exp(−.6 LAI), RUE=2.35 g/MJ, growth limited by actual/potential transpiration | No nitrogen, respiration or organ-resolved carbon pool model |
| Grain formation | After 55% thermal progress, .44 of each day's dry-matter increment enters grain; stops at maturity | This is a reduced partition model, **not** historical max-ever `biological_yield` or validated yield prediction |
| Harvest transfer and recovery | Biology removes gross mature grain once; physical equipment applies scenario `harvest_recovery_fraction` (default .96), recording recovered and lost dry grain/water separately | Fixed engineering recovery assumption; no dynamic preharvest/shattering or machine-condition model; no public unmeasured loss field |
| Grain water | At maturity, daily moisture falls .018 with rain rewetting up to .04/day, bounded [.10,.35] | No humidity-dependent equilibrium/sorption model |
| Observation | Rounded soil VWC, visual stage/grain moisture, NDVI from LAI, thermal anomaly from soil-water stress | Deterministic synthetic instruments; no sensor calibration or measurement-noise claim |
| Fertilizer/nutrients/pests | Not ported, not scored, no corresponding working operations advertised | The original complete agronomic management scope remains outside this implementation |
| FOS/yield gap/economic scores | Not ported | Success is harvest/storage/resource/deadline consistency only |
| Detail=true/library/Kwoo prompts | Not ported; generic model system instruction only | This is a default unassisted task condition, not a knowledge-retrieval experiment |

## Units and accounting

Farm area is m², root-zone storage and irrigation are mm, irrigation volume is
m³. Grain uses separate dry-kg and water-kg pools; moisture is water / total mass.
Seed stocks are integral counts; fuel litres and electricity kWh are explicit.
The runtime counts water upon acceptance and checks pending plus settled water
on recovery. Soil water is checked independently per ridge; harvest dry mass
must agree across biology and physical ledgers. Drying never creates dry mass.

Internal assimilated dry matter and grain reserve are audit-only. Public harvest
receipts expose container quantities, not potential yield or field-loss truth.
No fixed positive yield is inserted to make acceptance pass. Do not compare
this model's stored kilograms against the historical 8-tonne run as evidence of
agricultural performance. Scientific calibration is still required.

## Evidence

`demo/test_l3.py` checks Oracle outcome, strict replay, source/snapshot isolation,
water/seed/fuel/dry-mass accounting, invalid actions, late-failure rollback across
midnight, spatial irrigation, new-process resume before water settlement and
after material transfer, privacy, malformed restore rejection and no-credential
model-interface execution. The existing L1 suite also remains required.

Real LLM configuration/results and exact replay are reported separately in
`demo/L3_NEW_RUNTIME.md`. A model's success does not validate agronomic equations.

## Review correction (0.4.1)

The earlier `field_loss_dry_kg` incorrectly named a fixed harvest-operation
loss. It is now `harvest_loss_dry_kg` with an explicit grain-water loss ledger.
Mechanical recovery belongs to the physical adapter; biology projects/removes
gross grain. This is a semantic and responsibility correction, not a newly
calibrated loss model. Runtime schema 4 rejects earlier snapshots.
