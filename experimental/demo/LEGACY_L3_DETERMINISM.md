# Original L3-mini: isolated validation fixes

This follow-up addresses the failures measured in [LEGACY_L3_VALIDATION.md](LEGACY_L3_VALIDATION.md). It is **original test at 59ceb7e plus a local validation patch**, not a claim that the unmodified branch passes. The primary reference remains unchanged. No new L1 production module imports legacy code.

The reviewable implementation is [legacy_l3_validation.patch](legacy_l3_validation.patch), currently applied to `.runs/l3_test_validation`. Its changes are:

* Simulation time advances explicitly; model latency no longer advances physics. Wall-clock timeout and usage measurements remain real-time based.
* SHA-256 derives stable per-App seeds; Scenario.seed is applied to the Apps. Noise remains enabled. Independent operation counters replace UUIDs without consuming observation randomness.
* Executed Oracle steps record actual before/after times and explicit scheduled waits. A plan-only workflow retains planned timestamps and performs no operations. This fixture uses a serial dependency chain; the patch does not establish a general concurrent-DAG scheduler.
* Replay regenerates content and time fields, including explicit waits. Operation intent/metadata such as op_type, error_stage and dependencies still comes from the source workflow. Equality is enforced by the independent audit command, not the historical replay CLI's success flag.
* SystemApp's string date output now uses Asia/Shanghai consistently with Engine.
* Returned business errors are counted and tagged, without silently replacing the original business rules.

Counters are run-local. This validation uses initialization plus replay, not direct restoration of an intermediate legacy engine. It does not claim to fix all legacy physical side effects, control every physics RNG through Scenario.seed, or migrate L3 into the new ontology runtime.

## Reproduce the isolated implementation

From the project root, choose a new destination:

```bash
git clone --no-hardlinks reference/FAIRY-test .runs/l3_test_validation_fresh
git -C .runs/l3_test_validation_fresh apply ../../demo/legacy_l3_validation.patch
```

Run its tests with the project interpreter, explicitly isolating pytest configuration from the parent project. Placeholder keys are only needed because legacy client-construction tests read imported defaults; the tests stub actual model requests.

```bash
cd .runs/l3_test_validation_fresh
DEEPSEEK_API_KEY=test-key OPENAI_API_KEY=test-key \
  ../../.venv/bin/python -m pytest -c /dev/null --rootdir . -q \
  tests/test_fairy_migration_smoke.py tests/test_l3_determinism.py --disable-warnings
```

Observed: **78 passed**. The new tests compare complete JSON-normalized Oracle/replay trajectories across two processes with different PYTHONHASHSEED values, introduce 10 ms real waiting at every tool invocation, verify effective scenario noise seeds, and ensure changing recorded observation/time values does not change independently recomputed results.

## Strict acceptance

The user explicitly confirmed the acceptance scope: engineering execution and task outcomes. Oracle and real LLM must complete, harvest/store successfully within the water quota, and pass strict replay. Full agronomic evidence-chain quality is reported separately and is not a blocking gate for this run. This does not waive strict replay, actual outcome checks, or truthful tool-error accounting.

Generate the original run and its replay into separate directories using this patched checkout's CLI. Then use the root project's standard-library audit:

```bash
.venv/bin/python demo/audit_legacy_l3.py \
  --source SOURCE_WORKFLOW.json --source-report SOURCE_REPORT.json \
  --replay REPLAY_WORKFLOW.json --report REPLAY_REPORT.json \
  --out AUDIT.json --require-exact
```

This command fails unless complete workflows and final outcome summaries match exactly and both original/replay satisfy the necessary harvest, storage and water-budget gates. A missing original report also fails. Even a small timestamp difference fails strict mode. These necessary gates are separate from the full evidence-before-treatment trajectory review.

Measured Oracle acceptance: `.runs/l3_fixed_oracle_source` versus a new process in `.runs/l3_fixed_oracle_crossprocess`, with `PYTHONHASHSEED=321`. `strict_audit.json` reports exact workflow equality, exact final outcomes and passing necessary gates; strict command exit code is zero.

## Real-model verification

The exact non-secret run settings and supplemental public prompt are also preserved in [l3_validation_config.json](l3_validation_config.json), so they are available independently of ignored local artifacts. Replay reuses the scenario_kwargs from that file and makes no LLM calls.

The patched real-model experiment is in `.runs/l3_fixed_llm`. `config.json` records the baseline commit, patch SHA-256, actual server model (Qwen3.6-35B-A3B-FP8), request settings and complete supplemental public checklist. The checklist reiterates the task's stage checks and asks for at most five days per time advance; it provides no hidden priority area, internal state or Oracle dose. History is limited to 20 turns; output to 2,048 tokens; tool calls to 600; run wall time to 3,600 seconds. Request timeout is 240 seconds. Qwen thinking remains disabled by the original client's default.

This differs from the unmodified experiment's prompt and history settings and must not be presented as a controlled attribution of policy improvement to the runtime patch.

## Completed engineering acceptance

| Check | Oracle | Real LLM |
| --- | --- | --- |
| Normal completion | Yes | `agent_finished`, no uncaught exception |
| Workflow steps | 262 | 129 (128 tool calls) |
| Harvested ridges | 64 | 64 |
| Stored grain | 8,373.7 kg | 7,838.1 kg |
| Irrigation / quota | 3.9375 / 4 mm | 0 / 4 mm |
| Independent-process exact workflow comparison | Pass | Pass |
| Exact final outcome comparison | Pass | Pass |
| Original and replay outcome gates | Pass | Pass |

The real LLM made 125 model requests in 855.151 seconds. Its eight business errors (one insufficient seed, one premature harvest, one rainy harvest and five grain-bin overflow attempts) were followed by successful corrective actions. Independent workflow counting, `metrics.tool_error_count`, and `controller_telemetry.error_count` all equal eight. All grain was stored safely and harvested inventory was cleared. Errors were not removed from replay.

The real-model replay ran in a fresh process with `PYTHONHASHSEED=789`, with no model calls. Strict audit exited zero and found no workflow differences. Evidence: `.runs/l3_fixed_llm/strict_audit.json`, `.runs/l3_fixed_llm_replay/`, and `.runs/l3_fixed_llm/verification.json`. The verification manifest records artifact SHA-256 values, the run's patch identity, every necessary outcome gate, and error-counter agreement. The original reference checkout remains clean.

Validation counts: 78 smoke/determinism tests plus 10 additional historical replay-contract tests passed in the validation copy; 147 current-project tests passed. An independent subagent reviewed the patch and final artifacts, directly compared source/replay JSON, confirmed the eight errors were recovered, and checked that the recorded prompt was actually sent. The stored batch was dried to 13.0% moisture. Independent final acceptance found no blocking issues within the user-confirmed scope.

Policy quality remains separate: the model did not consistently follow the supplemental five-day recommendation and did not complete the full R5/R6 ground evidence chain. This run establishes the user-confirmed engineering and task-outcome goal, not full agronomic compliance, optimality, or general success across all L3-mini scenarios.
