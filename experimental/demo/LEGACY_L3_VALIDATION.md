# Original test-branch L3-mini validation

This is a validation of the unmodified historical implementation, not a migrated L3 scenario in the new ontology runtime. The reference checkout is pinned by `reference-baseline.json` (commit `59ceb7e7c02a728909d522dc4b5dda752a9fe1b3`). Production code must remain independent of `reference/`.

The selected scenario is `scenario_full_season_hb_dryr5r6_hn58_std_waterlimit`: 64 ridges of HEINONG58 soybean, 7.9 cm seed spacing, a 170-day season and a 4 mm field-average irrigation budget. The fixture includes a detailed public task briefing. Native means the historical branch's own physics, not the new L1 Native backend.

## Reproduction

Run from the repository root with the project virtual environment available. The reference checkout must already exist. The real-model command sends the scenario and tool results to the explicitly configured model service.

```bash
ROOT="$PWD"
SCENARIO=scenario_full_season_hb_dryr5r6_hn58_std_waterlimit
mkdir -p "$ROOT/.runs/l3_native_baseline" "$ROOT/.runs/l3_native_llm"
cd "$ROOT/reference/FAIRY-test"

"$ROOT/.venv/bin/python" -m fairy.cli --scenario "$SCENARIO" \
  --build-oracle --run-oracle-tools --replay --evaluate \
  --output-dir "$ROOT/.runs/l3_native_baseline"

"$ROOT/.venv/bin/python" -m fairy.cli --scenario "$SCENARIO" \
  --agent --provider vllm --model Qwen3.6-35B-A3B-FP8 \
  --endpoint http://100.115.106.71:8000/v1 \
  --temperature 0.1 --max-output-tokens 4096 \
  --max-tool-calls 400 --timeout-seconds 1800 \
  --output-dir "$ROOT/.runs/l3_native_llm"

"$ROOT/.venv/bin/python" -m fairy.cli --scenario "$SCENARIO" \
  --replay --evaluate \
  --workflow "$ROOT/.runs/l3_native_llm/$SCENARIO.agent_workflow.json" \
  --output-dir "$ROOT/.runs/l3_native_llm_replay"
```

Replay does not call the model. Model identity must be checked against `/v1/models`; this run uses the server's actual Qwen3.6 model, as explicitly approved by the user. The historical Qwen client disables thinking by default, uses full history unless a window is supplied, and permits parallel tool calls by default. The CLI wall-time limit is checked between calls; it is not a hard network-request timeout.

## Independent acceptance

The original scenario does not implement a meaningful `validate()` result: `Scenario.validate()` returns `None`, which `Engine.evaluate()` treats as success. Neither that flag nor the process exit status alone proves task success.

Check execution errors and returned error objects separately; then verify all 64 ridges are harvested, stored grain is positive, harvested grain awaiting storage is cleared, and cumulative irrigation stays within 4 mm. These are necessary outcome checks, not a substitute for reviewing the management trajectory: correct cultivar/spacing, evidence before treatment, targeted irrigation, maturity and weather checks before harvest, and safe storage moisture.

Report replay at two levels. Strict equality includes all fields. A diagnostic comparison separately identifies UUID substitutions and clock differences, while keeping physical, crop and inventory results exact. Random mission/inspection IDs require consistent one-to-one correspondence. Clock fields must remain visible in the audit because charge and treatment readiness affect behavior.

`fairy/time_manager.py` in the historical checkout adds wall time to simulation time. `Engine.replay_workflow()` executes recorded calls in order but does not restore the original execution times or verify their results. Therefore successful replay execution is not evidence of deterministic replay. Do not repair the reference checkout or relax physical tolerances to manufacture a passing baseline.

## Results

The initial Oracle/replay produced 262 workflow steps and a replay outcome of 64 harvested ridges, 8,373.7 kg in storage and 3.9375/4 mm irrigation. Its strict replay comparison fails; content differences are limited to timestamp fields and random mission/inspection IDs. The CLI outcome in this combined run describes replay state, not a separately captured Oracle final-state snapshot.

Independent review found two distinct timing issues: numeric content timestamps differ by at most about 0.049 seconds with no local-date changes, whereas top-level workflow step timestamps differ by up to 331,755 seconds, with 226 steps on different dates. The latter is a substantive historical timestamp-recording problem, not ordinary execution-time noise. Other fields compare exactly and UUID correspondence is bijective. The necessary outcome gates pass. See `.runs/l3_native_baseline/independent_audit.json`.

A separate Oracle run captured both engines' final outcome summaries in `.runs/l3_native_oracle_independent/outcomes.json`: Oracle and replay summaries are exactly equal, including 8,373.7 kg stored grain. This adds the independent Oracle final-state evidence absent from the combined CLI report. The timestamp cause is visible in `Engine._run_captured_oracle_events()`: it records precomputed dependency timestamps, which count explicit SystemApp time advances but omit elapsed equipment work, while replay records the actual engine clock.

Reproduce the independent artifact audit without importing or executing historical code:

```bash
.venv/bin/python demo/audit_legacy_l3.py \
  --source .runs/l3_native_baseline/scenario_full_season_hb_dryr5r6_hn58_std_waterlimit.oracle.json \
  --replay .runs/l3_native_baseline/scenario_full_season_hb_dryr5r6_hn58_std_waterlimit.replay.json \
  --report .runs/l3_native_baseline/scenario_full_season_hb_dryr5r6_hn58_std_waterlimit.run_report.json \
  --out .runs/l3_native_baseline/independent_audit.json
```

The artifact audit deliberately reports findings rather than treating filtered equality as acceptance. Five focused regression tests exercise state changes, UUID mapping conflicts, date boundaries, call order and misleading legacy validation. Full project regression: 146 passed on 2026-11-15.

Real LLM validation and its independent replay are recorded separately under `.runs/l3_native_llm` and `.runs/l3_native_llm_replay`. These artifacts are local and ignored by Git. The consolidated result is `.runs/l3_native_llm/validation_summary.json`.

| Measured result | Original test / Qwen3.6-35B-A3B-FP8 |
| --- | --- |
| Agent termination | `agent_finished`, no uncaught exception |
| Wall time | 1,149.713 seconds |
| Model requests / tool calls / workflow steps | 118 / 129 / 130 |
| Harvested ridges / stored grain | 64 / 7,505.17 kg |
| Irrigation / quota | 0 / 4 mm |
| Stored grain moisture | 11.084%, below 13.5% |
| Actual business tool errors | 10; original telemetry incorrectly reports 0 |
| Original versus replay final outcome summaries | Exactly equal |
| Necessary outcome gates, original and replay | Pass |
| Strict step-by-step replay | Fail |

The replay audit found 71 sensor value differences (30 VWC, 30 soil temperature and 11 canopy proxy values), 14 `current_datetime` differences, and missing `time_before/time_after` on all 129 replayed calls, in addition to explicitly classified UUID and time fields. Do not hide these differences under a general tolerance.

The sensor cause is `App.set_seed()`, which uses `hash(f"{seed}_{self.name}")`. Python randomizes string hashing between processes. Two independent processes reproduced different seeds and samples; explicitly setting `PYTHONHASHSEED=0` made those probes equal. That environment setting was **not** applied retrospectively to the recorded LLM run and would not fix wall-time drift, UUIDs or missing replay metadata. Original same-process Oracle/replay agreement therefore did not establish cross-process reproducibility.

The model's completed harvest is not proof of full agronomic compliance. It chose no irrigation, skipped some requested ground verification, and obtained a different outcome than the privileged Oracle. No optimality comparison is claimed. Mavic3M returns canopy temperature in this historical implementation, so lack of a Matrice4T call alone does not prove absence of thermal evidence; the old common drone implementation itself blurs equipment capability boundaries.

Independent trajectory review confirmed correct HEINONG58 planting on all 64 ridges (4 cm depth, 7.9 cm spacing), and subsequent recovery from all ten errors: missing base fertilizer, insufficient hopper seed, two rainy harvest attempts, one wet-grain attempt, four grain-bin overflow attempts and one oversized residue-treatment range. The dry-grain call skipped drying because the batch was already safe; do not count it as actual drying.

The only robot ground inspection occurred on June 20, covering ridges 22–29 with 30–32 explicitly uncovered. There was no new R5/R6 ground inspection. The last soil sensor check was September 3, before the September 29 harvest, and no forecast query was made. Successful operation guards and final storage therefore do not certify the requested evidence-before-action management process.

Conclusion: Oracle, real LLM and replay execution paths run end to end on this fixture; final outcome summaries reproduce. The claim that the original implementation is completely correct or strictly replayable is **not accepted**. Clock semantics, stable random seeds, replay metadata, error accounting and evidence-based task validation remain migration requirements.
