# Numerical irrigation model

`modeling.py` defines `IrrigationActorCriticPolicy`, the trainable model used by
MaskablePPO. It gives the actor and critic separate two-layer MLPs (32 → 32,
Tanh), both reading the same public feature vector. The actor produces one
logit per irrigation dose, with illegal actions masked by MaskablePPO; the
critic predicts one scalar state value. To explore a different network, start
here. Training records both branch widths and the activation from the model.

`irrigation.py` defines the features, masks and frozen checkpoint adviser.
`training.py` runs CPU training and held-out evaluation. No weights or simulator
are imported from WOFOSTGym. Existing checkpoints remain tied to their recorded
source fingerprints and should be replayed against the matching source.

The dose vocabulary is 0, 5, 10, 20 and 30 mm over the full field. Zero is a genuine policy output; missing observations raise NEED_OBSERVATION. The trained model never executes work. Live LLM and training use the same feature extraction, dose semantics, resource masks, real tools and midnight settlement.

Policy inputs are public crop stage, root-zone VWC, issued forecast/current weather and public remaining/pending water. Hidden crop matter, future forcing outside issued forecasts, audit state and reference answers are not inputs. Terminal reward uses actual safe stored dry mass; water cost and reward scaling are declared before evaluation. Training weights, weight change, packages, seed split and hashes are recorded. A loaded network is not evidence of superiority.

Install the `oracle` optional dependency group. Importing the main FAIRY runtime does not import the ML stack.

## Backend scope (0.8.0)

Policy schema 2 / feature version 2 uses the common coarse public phase rather
than assuming Native R1–R8. Train explicitly with `--backend native|wofost`.
WOFOST requires an explicit Soybean_901 proxy scenario; its weights and
qualification list are separate. Existing version-1 weights require retraining.
Candidate selection groups source/backend/scenario/objective domains.

## Supervised water-stress probe

Run the offline experiment from the repository root with the WOFOST and oracle
extras installed and both historical weather archives available.

```sh
PYTHONPATH=experimental .venv/bin/python -m evaluate.oracle.stress_supervision \
  --harbin .runs/historical-era5-openmeteo-1991-2020/archive.json \
  --changchun .runs/pcse-weather-skill-changchun-mnDCeb/archive.json \
  --output .runs/soil-stress --seed 42 --workers 4
```

The dataset uses the migrated historical scene compiler, Soybean_901_proxy
and PCSE 6.0.13. The zone label is mean `1 − RFWS` for the last completed
local day. RFWS is read before PCSE integrates and clears its daily rates.
Days without a living, transpiring crop do not provide labels. The deficit
classification uses a severity threshold of 0.10.

The six input features come from public tool receipts. They include root-zone
water content at the observation time, rainfall, reference ET, radiation
and mean temperature for the last settled day, and the public crop phase.
Each input contains seven consecutive days from the same zone. No numeric
date, scenario identifier, WOFOST truth or future weather enters the network.
The model has a 16-unit GRU and scalar output. It estimates deficit severity
for the most recently settled day.

Splits are by weather year, grouping irrigation variants together. Harbin test
years are 2001, 2007 and 2016. Changchun 2016 provides a separate regional
test case. Training-only normalization and validation-year checkpoint
selection precede test scoring. The experiment compares a trained one-day
variant and a root-VWC threshold of 0.18. Artifacts include `dataset.npz`,
`manifest.json`, `metrics.json` and the two local weights files with hashes.

Use `StressObservation.from_public` to turn the public tool responses into
one day of inputs. `SoilStressPredictor.load` checks the manifest and weights,
and `predict` accepts the chronological observations. The LLM can use the
prediction as an observation when making its own management decisions.
The experiment covers water stress in the WOFOST simulator. This configuration
has no soil nitrogen limitation, so it provides no nitrogen-deficit labels.

### First archived experiment

Seed 42 produced 15 trajectories and 2370 zone-day windows. The training,
validation and held-out partitions contain 1294, 306 and 770 windows,
respectively. The held-out partition contains 158 deficit labels.

| Method | Parameters | Severity MAE | F1 | False positives | False negatives |
| --- | ---: | ---: | ---: | ---: | ---: |
| Seven-day GRU | 1169 | 0.0324 | 0.932 | 8 | 13 |
| Single-day GRU | 1169 | 0.0402 | 0.925 | 22 | 3 |
| Root VWC below 0.18 | 0 | — | 0.885 | 12 | 23 |

The seven-day model reduces aggregate severity error and false alarms,
while the single-day model detects more positives. The seven-day F1 is
0.880 on the held-out Changchun case and 0.846 on the held-out irrigated
Harbin 2016 case. Each weather year remains in one partition and adjacent
windows within the same trajectory share observations. This measures
agreement with the WOFOST proxy and its simulated public sensors.
The recorded run is at `.runs/soil-stress-w3YInf/trial`.

## Retrospective diagnosis ablation

Run the following command with the original supervised experiment and the same
weather archives. The collector checks that the original features, labels, case
IDs, dates, zones and year partitions reproduce exactly before fitting.

```sh
PYTHONPATH=experimental .venv/bin/python -m evaluate.oracle.stress_ablation \
  --harbin .runs/historical-era5-openmeteo-1991-2020/archive.json \
  --changchun .runs/pcse-weather-skill-changchun-mnDCeb/archive.json \
  --original .runs/soil-stress-w3YInf/trial \
  --output .runs/stress-ablation --workers 6
```

For each diagnosed day, the collector records public root-zone VWC before that
day's water balance integrates, the completed day's ET₀, and the depth from any
successful irrigation receipt for that day and zone. The analytical baseline
predicts each ridge separately using static field capacity, wilting point and
the cultivar's CFET and DEPNR. It applies the PCSE equation to rounded public
VWC and averages ridge predictions. The four fitted GRUs share the training
objective and held-out windows. They compare one and seven days with and
without irrigation receipts. Positives have severity at least 0.10. A severe
miss has measured severity at least 0.20 and predicted severity below 0.10.

These results describe the previous completed day. A current-state diagnostic
for the LLM requires targets and observations anchored to the decision time.
It can describe current deficit and recent water application while leaving the
irrigation decision to the LLM.

The archived run contains 770 held-out windows, including 158 positives and
142 severe positives. The analytical baseline recorded zero false positives
and false negatives, with severity MAE 0.000974. The original seven-day GRU
recorded 8 false positives, 13 false negatives and 6 severe misses.

| Aligned GRU inputs | False positives | False negatives | Severe misses |
| --- | ---: | ---: | ---: |
| One day | 16 | 0 | 0 |
| One day plus receipt | 14 | 0 | 0 |
| Seven days | 9 | 7 | 2 |
| Seven days plus receipts | 12 | 6 | 0 |

Only two held-out zone-days have a nonzero same-day irrigation receipt,
compared with four in training and zero in validation. The observed changes
do not establish a general receipt effect. The analytical baseline is a
reconstruction of this simulator's label using declared site and cultivar
parameters. Its accuracy does not establish field accuracy. The archived
results are in `.runs/stress-ablation-RimTvC/trial`.

## Current root-water availability for the LLM

The WOFOST soybean full-season public context can include `water_diagnostics`.
It projects up to four recently observed ridge ranges from public tool receipts.
For each range, `current_root_water` records the observation timestamp and the
mean of the per-ridge ratios
`clip((VWC − wilting point) / (field capacity − wilting point), 0, 1)`.
The baseline labels that mean severe at or below 0.20 and low at or below
0.50. These are fixed thresholds for modeled root-water availability. The
same-day observation remains valid across an accepted irrigation request
because water settles at local midnight. After the local date changes, a
previous-day soil observation produces `needs_fresh_soil_observation`.

`irrigation` reports pending and last-midnight settled depths separately.
Each amount is the average over the requested observation range, and the
receipt covers only its intersecting ridges. The context flags a classification
as near a boundary when rounding the public VWC to 0.001 can change the class.
The separate `yesterday_stress` entry is dated and available when public
prior-day soil and crop observations and completed-day weather cover the
range. It uses the pinned Soybean_901 formula. These fields provide evidence
for the LLM and do not trigger any farm work.

```sh
PYTHONPATH=experimental .venv/bin/python -m evaluate.oracle.current_water \
  --harbin .runs/historical-era5-openmeteo-1991-2020/archive.json \
  --changchun .runs/pcse-weather-skill-changchun-mnDCeb/archive.json \
  --output .runs/current-water --workers 6
```

The benchmark keeps the original year partitions and adds paired irrigation
probes in the held-out years. Current public measurements are compared with
unrounded PCSE soil moisture at the same decision time, using the same
availability thresholds. The 1328 held-out windows include 202 severe
references. Seven three-class predictions differ, all within public
measurement rounding of a class boundary. Severe classification has two
false positives and three false negatives. The eight pending and eight
just-settled window pairs all match reference classifications. Four
pairs start at severe deficit and remain severe after queuing before
moving to low after settlement. The recorded run is at
`.runs/current-water-hkraeA/trial`. The benchmark measures a simulator
state projection, and the LLM payload is verified with a mock chat endpoint.

## Decision-day crop-water expert

For WOFOST soybean, the LLM adapter receives a compact crop diagnosis for an
observed range. The expert uses the first same-day soil receipt once current
living crop observations cover the range. Queuing water changes the separate
settlement status. The next local day requires fresh soil and crop evidence.
The public model input includes the evidence status, classification, observed
time, range and rounding warning, plus categorical coverage for pending and
settled water. Yesterday's stress stays separately dated. Per-ridge soil
numbers are stored in the event history and replaced with a compact receipt
when building the LLM messages.

The expert has two fitted parameters, slope and intercept, operating on
normalized root water. The offline label applies the pinned Soybean_901
water-limitation equation to unrounded decision-time soil from the homogeneous
evaluation zones under a fixed reference ET₀ of 4 mm/day. Actual same-day or
future weather is not used. This diagnoses reference-demand water limitation
rather than attempting to predict the realized rate through the day. The
decision thresholds are 0.10 for water limitation and 0.20 for severe
limitation. The LLM receives categories and a rounding-boundary flag.
Nitrogen diagnosis is unavailable in the current configuration.

Reproduce the fit with the saved decision-time dataset.

```sh
PYTHONPATH=experimental .venv/bin/python -m evaluate.oracle.fit_water_expert \
  .runs/current-water-hkraeA/trial/records.json .runs/reproduced-crop-water-expert
```

The fit uses training years only. In 1328 held-out windows, there are 234
reference-positive and 216 severe windows. The expert has zero binary false
positives and false negatives, MAE 0.00127, and one severe case classified as
water-limited. This measures approximation to an engine-derived reference
under known simulated conditions. Field accuracy and downstream real-LLM
decisions require separate validation.

## Timing and ranked water-diagnosis experiment

The LLM system prompt explains the local-midnight update for crop growth and
routine soil water, and the day-scale relevance of successful observations.
Equipment, inventory and work may change within the day. For comparable
non-overlapping zones, the expert projects the order of current stress using
the limits implied by VWC rounding. Overlapping score intervals are reported
as indistinguishable. The LLM sees zone identifiers and the relative order.
The individual numeric scores and their intervals stay in the expert context.
Irrigation pending and settlement remain separate management facts.

The four-arm runner in `evaluate.oracle.llm_water_pair` prepares four matched
windows per held-out case. Its phases are `prepare` (with Harbin and Changchun
archive paths), `decide` (with the selected service URL), and `finish`.
It compares the original prompt with raw observations, timing guidance with
raw observations, timing plus compact categorical diagnosis, and timing plus
compact diagnosis with relative ranking. Redundant reads are successful
same-day soil or crop queries whose requested ridges were already covered.
The common continuation performs full-season harvest and storage, and replay
re-executes the recorded operations without contacting the model service.

The archived four-arm run used Qwen3.6-35B-A3B-FP8 at 16 matched
checkpoints. Each arm has 16 decision windows. After model decisions,
all 64 continuations completed safe storage and strict replay.

| Arm | Rechecked after settlement | Chose the drier zone first | Cumulative input tokens |
| --- | ---: | ---: | ---: |
| Raw observations | 2/4 | 1/4 | 304360 |
| Timing guidance with raw observations | 4/4 | 1/4 | 331734 |
| Timing guidance with compact grades | 4/4 | 1/4 | 348506 |
| Timing guidance with compact grades and ranking | 4/4 | 4/4 | 349248 |

Ranking improved the two same-grade cases from 0/2 to 2/2. The ranked
arm used 6.5% fewer first-request input tokens than timing with raw
observations, but 5.3% more across the whole decision window. The
original prompt already produced zero duplicate same-day soil reads
in this sample. These results describe the frozen run at
`.runs/llm-water-ranking-qAJawI/trial`, which captured the full
executable workspace at measurement time, including shared changes.
