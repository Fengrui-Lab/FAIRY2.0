# Agricultural infrastructure

Scope: fixed installations and systems such as irrigation networks, sensor systems, charging stations and storage facilities. Instances may reference equipment components when such models are added.

- `soil_sensor.py`: SoilSensor owns identity, plot target and the observe_soil declaration. It projects deterministic root-zone VWC rounded to 0.001 from the current biology backend. It is not the legacy 5 cm, six-zone noisy sensor model. Reads neither advance time nor integrate crop processes.
- `irrigation.py`: IrrigationSystem owns identity, plot target and the irrigate declaration. It validates available quota and converts gross mm into m³ using plot area. Runtime registers pending input and settles it once at local midnight. No pump flow, power, operation duration or source-water stock is modeled yet.

Providers receive only the interfaces needed for their behavior. They never own an independent clock or retain a second copy of crop/soil state. Runtime restores or replaces the backend without leaving providers bound to an old instance.

`season.py` owns the spatial irrigation, grain-facility and charging contracts.
Their execution uses the shared Runtime and physical ledger; irrigation queues
water for daily biology settlement. It does not simulate a finite reservoir.
