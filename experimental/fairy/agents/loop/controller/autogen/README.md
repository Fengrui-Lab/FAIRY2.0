# Public-evidence Oracle generation

This module coordinates a public LLM through lifecycle cards, three-valued preflight checks, frozen numerical advice and workflow memory. It receives public context and actual public results, never Runtime, backend state or future forcing.

`public.py` derives evidence and workflow phase; `guards.py` returns PASS, BLOCKED or NEED_OBSERVATION. Generator-protocol restrictions (daily windows, full-field advice and no repeated pending advice) are labelled separately from backend legality: the underlying runtime still supports its original capabilities. A PASS covers evaluated public checks, not unknown field grain mass or every possible engine rejection.

`extension.py` offers a local advice service without creating physical events or advancing the clock. The LLM must issue the unchanged actual irrigation request. Advice is bound to the public evidence version and consumed daily window. Service calls and blocked attempts have a separate decision trace and budget; repeated blocks cannot run forever.

`memory.py` conservatively extracts local accepted workflows and verified recovery pairs from independently replayed artifacts. JSONL memories are immutable snapshots within a run and updated only between runs. The rule-based initial inducer is an AWM-inspired engineering baseline, not a reproduction of the paper's learned inducer. It does not store irrigation dose/date recipes or infer causes from final stored mass.

The entry point is `python -m interface.cli.oracle`; independent qualification belongs to `evaluate/oracle`. Scripted test models and untrained advisers cannot qualify as trained automatic references.

0.7.1 records one numerical decision per local day. A zero decision completes that window; a positive decision can be revalidated against updated public evidence without recomputing its dose. Distinct decision and certificate IDs preserve this distinction. Window feedback and low-progress warnings never perform advance_time for the LLM. Restores pin knowledge, memory and policy, including active advice, completed windows and cached decisions.

0.7.2 retains current public observations when service turns replace physical history. Crop evidence predating a same-day accepted planting is invalidated. Neither rule advances simulation time.
