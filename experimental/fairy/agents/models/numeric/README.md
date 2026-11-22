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
