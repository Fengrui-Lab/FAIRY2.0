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
