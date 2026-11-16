# Crop definitions

Crop and cultivar parameters describe what is planted and how adapters initialize their models. `lib/soybean.py` contains the pilot SoybeanSpec, simplified Native parameters and the explicit WOFOST cultivar identity. Live phenology and water state belong to the selected backend.

`lib` contains built-in definitions; `custom` is reserved for explicit extensions. This directory does not schedule management or provide agent policies.

Full-season experiment composition (including resource stocks and hydraulic
zones) belongs to `evaluate/scenarios/season.py`, not the crop ontology. The
Native full-season process audit documents its uncalibrated cultivar parameters.
