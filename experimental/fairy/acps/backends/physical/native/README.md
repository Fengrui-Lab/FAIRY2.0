# Native equipment and material ledger

`backend.py` implements pure job preflight and commit after Runtime advances the
clock. Duration is in hours; no method reads wall time or advances simulation.
Seed counts, fuel litres, battery percentages, electricity kWh, dry grain kg and
grain-water kg are distinct ledger quantities. Drying removes only water;
unloading/storage conserve both components. Warehouse moisture is wet basis.

Engineering assumptions (not calibrated machinery): seedbed 0.08 h and 0.6 L
per ridge; planting 0.06 h and 0.4 L; harvest 0.10 h and 0.9 L. Unload/store each
0.25 h. Drone 0.01 h and 1% battery per ridge; robot 0.04 h and 1.2%. Charging
0.75 h and 0.01 kWh per restored percentage point. Dryer removes 100 kg water/h
(minimum 0.25 h), using 0.8 kWh/kg water. Daily rain >10 mm or VWC >0.36 blocks
tractor work. Rain >10 mm or wind >8 m/s blocks flights. These are explicit
synthetic daily-window thresholds, not physical equipment certifications.

Combine/trailer/storage capacities and initial stocks are scenario inputs.
Successful harvest weighs recovered dry grain and water. The scenario recovery fraction (default 96%) is applied here to gross grain
provided by biology. The complementary harvest-operation loss is recorded
separately for dry matter and grain water in audit, never public observations.
No dynamic preharvest loss or machine-condition response is implemented. No nutrient or pesticide
response, geometric path planning, loading implements or arbitrary robot skills
are claimed. Failed operations roll back through Runtime, including crossed days.
