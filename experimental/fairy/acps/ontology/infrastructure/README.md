# Agricultural infrastructure

Scope: fixed installations and systems such as irrigation networks, sensor systems, charging stations and storage facilities. Instances may reference equipment components when such models are added.

- `soil_sensor.py`: SoilSensor owns identity, plot target and the observe_soil declaration. It projects deterministic root-zone VWC rounded to 0.001 from the current biology backend. It is not the legacy 5 cm, six-zone noisy sensor model. Reads neither advance time nor integrate crop processes.
- `irrigation.py`: IrrigationSystem owns identity, plot target and the irrigate declaration. It validates available quota and converts gross mm into m³ using plot area. Runtime registers pending input and settles it once at local midnight. No pump flow, power, operation duration or source-water stock is modeled yet.

Providers receive only the interfaces needed for their behavior. They never own an independent clock or retain a second copy of crop/soil state. Runtime restores or replaces the backend without leaving providers bound to an old instance.

`irrigation.py` 也定义全季分垄灌溉。`crop_sensor.py`、`seedbed_sensor.py`
和 `stand_sensor.py` 分别声明作物和土壤观测、种床观测和苗情观测。
`surface_drainage.py` 声明积水观测与排水，`charging_station.py` 声明设备充电。

`dryer.py` 定义烘干机、烘干和维护，`grain_warehouse.py` 定义粮库与安全入库，
`supply_warehouse.py` 定义种子和燃油收发。物理后端在 Runtime 事务中更新
作业与物料状态。
