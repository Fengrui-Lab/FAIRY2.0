# Farm and plot definitions

`spec.py` defines the pilot FarmSpec: plot identity, rectangular geometry, area, location and declared root-zone/soil parameters. It supplies initialization inputs, not live soil-water state. Runtime lifecycle and backend integration live outside this directory.

`lib` is reserved for reusable built-in farm presets; `custom` is reserved for explicit user presets. Neither is populated with working presets yet.

`observations.py` declares synthetic crop and spatial soil observations for the
initial 64-ridge fixture. Farm geometry stays passive; sensor providers expose
these operations. Root-zone water/latent crop mass live in biology, not geometry.
