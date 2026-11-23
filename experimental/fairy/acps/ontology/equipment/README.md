# Equipment

Scope: identifiable equipment units, including drones, tractors, robots and implements. Future implementations should keep configuration, capability limits, equipment-owned state and dedicated operations together. Fuel tanks, payloads and mounted implements need explicit ownership/references.

L1 has no equipment execution model; L3 uses the reduced Native physical implementation. Equipment operations must request runtime scheduling and use physical adapters where configured; they must not advance the shared clock themselves.

## 实体模块

| 文件 | 实体与职责 |
| --- | --- |
| `tractor.py` | 整地、播种、补种和机具换挂 |
| `combine.py` | 收割及旧式整仓卸粮 |
| `trailer.py` | 拖车湿重容量与指定批次卸入 |
| `multispectral_drone.py` | 多光谱无人机航测 |
| `thermal_drone.py` | 热红外无人机航测 |
| `ground_robot.py` | 地面机器人巡检 |
| `seedbed_implement.py` | 整地机具身份和能力 |
| `planter_implement.py` | 播种机具身份和能力 |

`implement.py` 定义机具共享规格。`supply.py` 汇总跨设备补给动作和每垄
耗油规则。`operations.py` 汇总场景提供者目录。物理后端维护挂载、种箱、
油箱及粮食批次位置。
