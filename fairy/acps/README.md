# Agricultural cyber-physical system

ACPS separates domain definitions in [ontology](ontology/README.md) from process implementations in [backends](backends/README.md). The runtime in `fairy/events` owns the simulation clock, scheduling, execution records, recovery and replay. Agents request public operations rather than editing engine state.

- `ontology`: agricultural objects, configurations, capabilities and operation contracts.
- `backends/physical`: equipment movement, spatial interaction and physical job execution. The Native serial equipment/material adapter is connected for L3.
- `backends/biology`: soil-water and crop processes, implemented independently by Native and WOFOST.

Public units are m, m², mm, m³, volumetric water content (m³/m³) and °C. Adapters convert engine-specific units. The Native L3 physical and biology adapters are composed by Runtime; they must not count the same duration, input or effect twice.

The original pilot remains one soybean plot with three public operations. An additional weather fixture validates explicit ontology loading with five operations. L3 adds reduced equipment execution and material ledgers; detailed dynamics and agronomic calibration remain future work. See [process audit](backends/biology/PROCESS_AUDIT.md) for sources, assumptions and validation. The primary historical reference is pinned in the repository's `reference-baseline.json`; runtime code must not import `reference/`.
