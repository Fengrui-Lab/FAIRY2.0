# L3 on the shared new runtime

This report records the 0.4.0 / schema-3 experiment. Current 0.4.1 / schema-4
review and newly generated evidence are in [CURRENT_VERSION_REVIEW.md](CURRENT_VERSION_REVIEW.md).
Historical real-model artifacts remain inspectable; they are not current-version
restore/replay inputs. Do not relabel them as a fresh 0.4.1 real-model run.

## What now shares the skeleton

`python -m interface.cli run --scenario soybean_l3_hn58_waterlimit_v1` uses
exactly `fairy.events.session.Runtime`, the same operation registry, trusted
Oracle/public LLM loop, JSON artifact, restore and strict replay as L1.
No subprocess or import of `reference/` is used for execution. The old patched
legacy checkout and its results are reference evidence only.

The task starts unplanted and supports seedbed preparation, planting, crop/soil
inspection, multispectral/thermal/robot observation, area-accounted irrigation,
equipment charging, harvest, unloading, drying and storage. All 64 ridges must
be harvested and recovered grain safely stored within resource/time limits.

The first supported L3 composition is Native biology + Native physical ledger.
WOFOST L3 fails explicitly. Both existing L1 biology backends remain supported.
The new full-season model is reduced and uncalibrated, with source assumptions
in [SEASON_AUDIT](../fairy/acps/backends/biology/native/SEASON_AUDIT.md). Original
nutrient/pest/chemical processes are not implemented, and agronomic equivalence
or field-yield prediction is not claimed.

## Default assistance and metrics

There is no detailed_briefing mode, library, Kwoo context, Oracle trace or stage
management recipe in the LLM task. The generic model prompt explains only tool
protocol, actual-history semantics and that a text-only reply ends the run.
The first real-model attempt stopped after planting; that failed artifact is
retained as `.runs/new_l3_llm_premature_finish.json`. The subsequent adapter
clarifies termination semantics and fixes temperature to 0.1; it does not add
agronomic instructions or allow the model access to audit state.

No biological-yield/FOS/gap/economic score is used. Grain dry matter is an
internal process state required for physical harvest; public observations never
expose it. Weighed stored grain, moisture, water/fuel/electricity use, tool errors
and replay consistency are separate reported outcomes.

## Commands

```bash
python -m interface.cli run --scenario soybean_l3_hn58_waterlimit_v1 --agent oracle --out .runs/new_l3_oracle.json
python -m interface.cli replay .runs/new_l3_oracle.json
python -m interface.cli evaluate .runs/new_l3_oracle.json
python -m interface.cli inspect .runs/new_l3_oracle.json
```

For a model, provide `FAIRY_LLM_API_KEY`, then pass `--agent llm --model MODEL
--base-url URL`. The evaluated local service exposes Qwen3.6-35B-A3B-FP8.
`--disable-thinking` is an explicit service-specific template option.
`--max-steps 400 --max-errors 30 --history-turns 20 --context-chars 60000 --max-output-tokens 2048
--request-timeout 240 --wall-timeout 3600` bound this experiment. The run-time
budget is checked between calls; an in-flight call is bounded by its request
timeout, not a hard real-time interruption.

Native needs no optional modeling dependency. A fake-model integration test
exercises the same LLM adapter/controller without credentials. Real-model
results are reported separately below; a fake model is not substituted for it.

The full-season real run uses linked-message adapter v3. Current v4 additionally
rejects tool replies with truncated/filtered finish reasons, even when argument
JSON is complete. This guard has regression coverage and a real service probe
(`finish_reason=tool_calls`); the normal tool path, task and runtime are unchanged.
Exact experiment settings are in `l3_new_runtime_config.json`.

The earlier short-memory run exhausted its error budget (98 calls, 30 errors).
Its failed trajectory was strictly replayed before the planting error contract
changed. It is retained at `.runs/new_l3_llm_short_memory_failure.json`, not
counted as passing evidence. Current context retains task progress derived solely
from successful public receipts, in addition to a bounded 20-pair history.

## Engineering verification

Current project suite: **183 passed**. The built wheel also completes Native L3
and replay in an isolated temporary directory with `python -I -S`, without
site-packages or access to a reference checkout.

- Shared-runtime Oracle: all 64 ridges planted/harvested; safely stored;
  0.46875 / 4 mm water; 0 tool errors; independent-process strict replay passed.
- Dry mass and grain water are conserved through harvest/unload/dry/storage.
- Spatial pending irrigation survives new-process restore without duplication.
- Restoring after a material transfer and continuing matches uninterrupted work.
- Failed jobs roll back biological state, equipment/materials, time and events.
- Malformed or inconsistent restore stocks, crop water and irrigation totals reject.
- Whole-field L1 irrigate retains its semantics when bound to an L3 provider.
- 720-hour stepping equals 15 × 48-hour stepping; every local midnight integrates once.
- Independent subagent review identified and verified fixes for recovery validation,
  hidden field-loss exposure, operation timing and shared-tool water allocation.

Runtime schema is now 3, FAIRY version 0.4.0. Older snapshots are inspect-only;
regenerate them for replay. Operations are serial atomic jobs, without parallel
or mid-job checkpoints. Model text/strategy success is not an agronomic validation.

## Real-model result

The v3 full trajectory reached 64 harvested ridges and safe storage, using
4 / 4 mm water. The model then repeatedly inspected inventory. This process
was interrupted and retained as `.runs/new_l3_llm.json` with `done=False`;
its strict replay passes, but it is not a normally terminated episode.

The shared controller now evaluates task completion before and after decisions
and terminates with `task_success`; no additional model reply is required.
Regression coverage includes successful operation sequences and restoring an
unfinished checkpoint whose task is already satisfied. Audit state used for
this trusted termination check is not sent to the model.

A checkpoint before the first successful harvest was reconstructed by replaying
only the real model's original requests; its 627 events exactly match the source
prefix. No Oracle request is substituted. Current v4 CLI resume starts a new
process at that checkpoint; state reconstruction does not call the model, while
subsequent decisions do. This is v3 real full-season history plus v4 continuation,
not a pure v4 initial-to-final experiment.

The v4 continuation completed with 7 real model calls and normal
`task_success` termination (event 651). All 64 ridges were harvested; safe stored
mass was 241.032540 kg; water use was 4 / 4 mm (75.4688 m³). Both dry-mass and
grain-water ledger residuals were zero. Two requests were rejected (planting an
already planted field; drying already dry grain), with no physical side effects;
the model recovered and completed storage. Stored mass is an engineering outcome,
not a biological-yield metric or agronomic performance claim.

Final artifact: `.runs/new_l3_llm_resumed.json`; independent-process strict replay:
`.runs/new_l3_llm_resumed_replay.json` (`task_success=true`, `replay_verified=true`).
The independent review verified the exact source/checkpoint/resume prefix,
actor identities, termination, metrics and strict replay. It also independently
ran 177 relevant tests; the root's full 183 includes six legacy-auditor tests.

Current wheel proof is `.runs/new_l3_wheel_verification.json`; reproducibility
hashes and acceptance evidence are in `.runs/new_l3_acceptance.json`. Failed and
interrupted developmental experiments are retained separately, not counted as
normally terminated passing runs.

