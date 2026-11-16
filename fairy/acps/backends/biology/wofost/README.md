# WOFOST biology backend

`backend.py` independently integrates PCSE 6.0.13 Wofost81_WLP_CWB. It owns its water/growth processes; Native processes are not applied on top. The adapter converts engine units and projects the same public observation semantics.

`soybean.yaml` is pinned offline cultivar data; `provenance.json` records origin and hash, verified when loaded. Recovery rebuilds from fixed initialization and daily input history with O(history length) cost. Missing PCSE or unsupported inputs fail explicitly. See the parent process audit for upstream provenance, model assumptions and exclusions.
