---
name: pcse-historical-weather
description: 通过 PCSE 的 Open-Meteo 历史天气入口获取明确选定的 ERA5 小时再分析天气，按指定地区时区归档为逐日天气轨迹。用于更换 FAIRY 农场位置、准备作物仿真气象驱动或比较历史年份时。
---

# PCSE 历史天气轨迹

确定农场的 WGS84 经纬度、IANA 业务时区和首末当地日期（均包含）。需要比较多个生长季时，先取得连续的参考时段，再按作物、地区和实验问题选择比较窗口。场景起点如果要发布上一完整当地日的天气，请将首日提前至少一天。

使用包含 PCSE 的 Python 环境。脚本用 PCSE 6.0.13 实测。路径相对于本技能目录。

```bash
python scripts/fetch_era5.py --latitude 43.82 --longitude 125.32 \
  --timezone Asia/Shanghai --start 2016-05-01 --end 2016-10-22 \
  --raw-dir .runs/changchun-era5/raw --out .runs/changchun-era5/archive.json
```

- 脚本复用 `pcse.input.OpenMeteoWeatherDataProvider` 的历史端点与请求处理，显式选择 `era5`、最近格点、UTC 小时数据和原始格点高程。保存分块请求及响应。相同参数可以加 `--offline` 并指定新输出路径，仅用已归档分块重建。
- 输出包含逐日序列 `daily`、请求坐标、实际格点和高程、本地时区、日期范围、原始分块 SHA-256、PCSE 版本和归档指纹。输出成功代表所请求日期连续且每个当地日字段齐全。遇到缺测时保留原始响应，先查清范围或数据可用性，再决定是否修订输入。
- 瞬时变量使用当地日 `[00:00, 次日00:00)`，降水和短波使用前一小时的统计量，归入 `(00:00, 次日00:00]`。脚本按 IANA 时区处理整小时的历史夏令时；当地日界落在 UTC 非整点时，需先明确小时内插值和通量分摊口径。总降水为液态当量，ET₀ 采用日尺度 FAO-56 Penman–Monteith。格点资料代表区域天气，使用地块级极值时需另做站点或独立数据源检验。

## 用作 FAIRY 场景输入

`daily` 中的记录可构造 FAIRY 的 `WeatherDay`。将归档指纹、实际格点、时区和截取日期绑定到场景，核对全季与预热日期覆盖。引擎使用离线归档，智能体只获取场景允许的已完成日公开观测。现有哈尔滨历史试点编译器固定坐标、年份与场景语义。其他地区复用天气获取，接入时需在场景层配置对应地区和农艺窗口。

比较常年和干旱年时，可从当地相关农艺窗口累计降水减 ET₀ 排序，再用实际模拟物候与土壤响应检验候选年。保留完整历史天气序列与选年规则。

变量语义见 [Open-Meteo 历史天气 API](https://open-meteo.com/en/docs/historical-weather-api) 和 [FAO-56 日参考蒸散](https://www.fao.org/4/X0490E/x0490e08.htm)。
