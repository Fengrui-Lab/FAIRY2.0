# test_Knowledge reference review

User-selected supplementary reference, reviewed on 2026-11-15:
[FAIRY at 6da01b0](https://github.com/Fengrui-Lab/FAIRY/tree/6da01b058a1504ef0b888cdc8c76bc92d0b8f793).
The original test baseline remains pinned separately. GitHub's comparison reports diverged history (15 commits ahead, one behind); this is not simply a fast-forward replacement.

The pinned GitHub archive is extracted at `reference/FAIRY-test_Knowledge`. It is a source snapshot without its own `.git`; `reference-baseline.json` records the commit and archive SHA-256. Do not infer its identity using `git -C` because that would find the parent project's repository.

| Observed implementation | Relevance to the new runtime | Boundary |
| --- | --- | --- |
| `tool_utils.py` parses Description, Args and Relation separately; exports argument descriptions and optional Relation text | Improve public operation documentation from one definition; avoid duplicated whole docstrings in every schema | Relation is natural-language operation usage guidance, not typed ontology object relationships or executable prerequisites |
| Agent classifies malformed arguments, exceptions and returned `error` values; records `TOOL_ERROR` and consistent feedback | Count business failures even when a Python call returns normally; preserve causal request/result links | Do not expose raw exceptions, arguments or private state through the new LLM error view |
| Empty/truncated model replies are rejected before tool execution and can be retried | Add bounded response validation; record rejected requests and usage separately | Retrying a model response must not repeat already executed physical operations |
| Tool errors enter tool history; rejected-response feedback is sent once with `feedback_for_llm_call_id` | Separate executed-operation history from model-protocol recovery | This remains an agent/controller concern, outside ACPS ontology |
| CLI exposes tool relation inclusion and explicit vLLM thinking override | Record effective experiment settings; enable fair ablations | Original test defaults thinking off for Qwen; this branch inherits server defaults unless explicitly overridden |

The branch retains the App/tool registration structure. It does not implement the proposed ontology or change `TimeManager` / `Engine.replay_workflow()` in this comparison. Before/after time logging alone does not establish deterministic simulation or replay.

An independently reviewed regression, `tests/test_error_contract.py::test_business_error_retains_reason_return_value_and_side_effects`, intentionally preserves legacy side effects on a failed tool call, including advancing the clock. This behavior conflicts with the new runtime's rejected-request atomicity requirement. Adopt the diagnostic ideas after adapting them to the new contracts; do not copy that execution behavior.

The `Fixes-Tools-Doc-Schema/` documents contain proposed documentation revisions. Treat proposals as audit material and inspect executable definitions before claiming a feature is active. The original-test L3 experiment is left unchanged so its results are attributable to that baseline; this review is not a real-model benchmark of test_Knowledge.

Focused local verification: **48 tests and 42 subtests passed**, without model calls. Run from the reference snapshot with the project Python interpreter:

```bash
/Users/ouyangkun/PHD/Project/FAIRY2.0/.venv/bin/python -m pytest \
  -c /dev/null --rootdir . -q \
  tests/test_error_contract.py tests/test_tool_relations_cli.py --disable-warnings
```

Explicit pytest configuration isolation is necessary: otherwise the parent project's `pythonpath` setting imports the new FAIRY package instead of the historical package, causing collection errors. The passing command tests the reference implementation.
