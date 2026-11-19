# Equipment

Scope: identifiable equipment units, including drones, tractors, robots and implements. Future implementations should keep configuration, capability limits, equipment-owned state and dedicated operations together. Fuel tanks, payloads and mounted implements need explicit ownership/references.

L1 has no equipment execution model; L3 uses the reduced Native physical implementation. Equipment operations must request runtime scheduling and use physical adapters where configured; they must not advance the shared clock themselves.

## Initial executable equipment

Full-season operation declarations cover tractor preparation/planting, combine harvesting/unloading,
unloading, multispectral and thermal drones, and a ground robot. Physical
execution and energy accounting live under backends/physical/native, never in
agent context. Sensors expose defined synthetic projections, not latent crop
mass. Hardware-specific operating limits are simplified and documented.

The current fixture declares a separate combine provider for harvest/unload.
Trailer and grain containers remain passive stocks in the physical ledger;
fuel is an explicit aggregate stock rather than per-machine tanks. Independent
vehicle locations, mounted implements and routes remain future capabilities.
