# 首轮物理／生物过程审查与迁移说明

初次实现日期：2026-11-10；迁移参考调整与复核：2026-11-13。范围：大豆单地块补水 L1，版本 FAIRY 0.2.0。本文将“有过程方程”“代码可执行”“工程验收通过”“田间校准”分别看待；本轮只验证前两项及列出的工程不变量，没有田间标定数据。

## 1. 从哪些历史实现中选择了什么

### 当前主要参考：用户指定的 test 分支

用户提供的链接为 https://github.com/Fengrui-Lab/FAIRY/tree/test 。GitHub 的 `branches/test` 接口现返回 `Moved Permanently`，明确指向 `test_history_window`；本轮固定其提交 `59ceb7e7c02a728909d522dc4b5dda752a9fe1b3`，本地目录为 `reference/FAIRY-test/`。身份与已核对文件的哈希另存于仓库根目录 `reference-baseline.json`。后续默认以这份原始实现为移植对照。

核对该提交的文件树，未发现 Jev、独立多作物目录或 WOFOST 适配器。它仍包含多套大豆场景、原有 Agent／专家协作内容，以及固定历史窗口改动；因此参考更集中不等于可以整体复制全部场景。

| 原始基线入口（相对 FAIRY-test） | 核对结果 | 对新骨架的约束 |
| --- | --- | --- |
| fairy/physics/soil_engine.py | 原始两层土壤模型；根深0.4 m、灌溉效率0.92，土壤阈值0.14/0.36/0.48 | 优先作为过程语义对照；新单根区模型及其0.31/0.45阈值仍是明确的简化／参数变化 |
| fairy/physics/phenology_engine.py | 大豆STANDARD代理采用出苗95、成熟1100积温，并有光周期／胁迫修正 | 当前95/1100已与原始基线复核；新积温算法不因此与原算法等效 |
| fairy/apps/farm_world/field_ops_app.py | 原始灌溉实现位于此文件；作业会推进关联时钟，非物理路径登记延迟VWC增量，物理路径登记管理输入 | 新工具继续使用mm毛水深及统一午夜结算，不复制小时到VWC增量或工具内部推进时钟 |
| fairy/scenarios/baseline_farm_world/scenario_irrigation.py | 传感器检查、定向灌溉、等待、复查及Oracle工作流；同时初始化多种设备 | 只提取原始任务语义，保留当前单地块、三个工具的L1边界 |
| fairy/agents/agent/history_window.py | 按完整LLM/tool turn选择提示历史，与持久历史分离 | 当前有界上下文只作为简化实现；后续原始循环移植从完整turn语义审查 |

这次调整是参考优先级与来源复核，不是将新骨架回退成旧框架。当前运行代码、工具契约、场景、数值参数及验收结果均未改动。特别是原始基线没有WOFOST，现有PCSE适配与离线参数仍保留各自的独立来源，不虚称来自test。

### 首次实现的历史辅助来源

以下记录2026-11-10首次实现时实际阅读／核对的入口，保留以便追溯；三个扩展版本从现在起不再作为默认迁移基线。它们均不是运行依赖，也没有整分支复制。未对全部旧场景出具正确性结论。

| 历史来源（均在 reference 下） | 发现／取舍 | 本轮去向 |
| --- | --- | --- |
| FAIRY-jev/fairy/physics/soil_engine.py 及同名说明 | 两层土壤、蒸散、排水、土温、通行性、氮相关过程混合在较大系统中 | 只采用“显式输入—储水—显式输出”的水量平衡思想，独立重写单根区模型 |
| FAIRY-jev/fairy/physics/phenology_engine.py | 积温、光周期、胁迫、播种深度等共同决定阶段，包含工程品种代理参数 | 只保留日均温积温与粗粒度进度；不复制品种／阶段规则 |
| FAIRY-jev/fairy/apps/farm_world/irrigation_app.py | 小时流量折算、旧世界对象、即时与日更新路径耦合 | 改为直接指定毛水深 mm，由唯一日步进结算 |
| FAIRY-jev/fairy/scenarios/baseline_farm_world/scenario_irrigation.py 及 physics action/tick 变体 | 补水流程可作为试点，但初始化了大量与本任务无关的设备 | 仅重建观测—补水—推进—复查链；从播种预运行，不手工写中期作物变量 |
| FAIRY-jev/fairy/physics/wofost_backend.py | PCSE 8.1、水氮过程、外接生物胁迫、旧物理状态镜像和逐日日志恢复 | 使用独立 PCSE 适配器；保留逐日日志重建、延后下一天 rates 的必要时序做法；去掉旧世界镜像及外接胁迫 |
| FAIRY-jev/docs/known-issues.md | UTC 物理日界与北京时间显示不一致 | 本版从场景显式传播业务时区，午夜统一日积分 |
| FAIRY-visualization/fairy/visualization/adapter.py | 区分公开投影与审计状态，视图读取不应改变世界 | 采用权限和只读投影原则，不迁入 UI、Three.js 或设备绑定 |
| FAIRY-multiobjective/docs/management-replay-metrics.md | 任务成功、严格回放、产量、经济口径需要区分 | 仅保留分项指标原则，旧流程分／产量／经济指标不迁入 |
| FAIRY-multiobjective/fairy/scenarios/management_extensions | 多作物、经营和轮作属于扩展任务 | 本轮均不迁入 |

核对时三个版本的 `soil_engine.py` SHA-256相同：`dd20e5536b5f16c5a18fc8d00eee6645f77585bae42643cdbb7c196b1905a119`。jev 与 visualization 的 `phenology_engine.py` 相同：`04a59f0b217c24a7af8e30c2a0cfb91ed7be9da02b50ce8de1f486beb8d3a8e7`；multiobjective 为 `ba2236c82fe98e3a1ea87e09bccbe15423ab92a9d920cd9098ab604f98e1ec1f`。因此不把分支能力表误读为所有基础过程实现完全不同，也不据此认定三个分支全部等价。

首次核对扩展版本时，`phenology_engine.md` 与代码的成熟积温存在历史不一致：说明中的1850和当时代码 STANDARD 的1100并不相同。本版显式采用1100作为简化工程假设，现已复核原始基线也包含STANDARD=1100；不称其为已校准品种系数，也不沿用旧报告的效果结论。

## 2. 过程纳入清单

| 过程 | Native 首版 | WOFOST 首版 | 相较历史版本的变化／尚未纳入部分 |
| --- | --- | --- | --- |
| 天气 | 消费固定逐日输入 | 消费相同天气日序列 | 不迁入天气生成器、NASA采集或概率事件；当前数据明确为合成 |
| 降雨 | 全部进入地块水量平衡，超饱和部分径流 | PCSE经典水量模型处理 | 不沿用旧0.85捕获系数及集水／外来汇流逻辑 |
| 灌溉 | 毛水量×效率进入根区 | 同一毛水量转cm、发送一次灌溉信号 | 不再由小时直接修改VWC；登记与结算分离 |
| 应用损失 | 单列毛水量与有效水量之差 | 同样单列，不额外施加Native损失 | 不是空间均匀度或管网模型 |
| 土壤储水 | 固定0.4 m单根区水桶 | PCSE动态根层及下层土壤 | 删除Native表土层、层间再分配与局部垄差异 |
| 径流／排水 | 饱和上限截流，然后当天自由排水至田间持水量 | PCSE原生入渗、根层渗漏和下层损失 | Native不保留历史排水速率；不能推断小时内入渗 |
| 蒸发 | 固定冠层比例分配潜在需求、受可用水约束 | PCSE原生土壤／水面蒸发 | 不迁入历史辐射／温度／风速经验ET近似；ET0直接是显式天气输入 |
| 蒸腾与水分胁迫 | 固定冠层分配，根区含水率线性限制蒸腾 | PCSE原生蒸腾和胁迫 | Native胁迫仅限制蒸腾，不作用于未实现的生物量 |
| 生育进度 | 日均温积温，出苗门槛及成熟积温比例 | PCSE原生出苗、物候和DVS | 不把Native积温比例与DVS或详细生长阶段等同 |
| 冠层与根生长 | 固定冠层／根深假设 | 原生LAI、根深变化 | Native动态冠层、根系扩展、光周期、播种深度响应均未实现 |
| 光合／生物量／分配／呼吸 | 未实现 | PCSE模型内部运行 | 只保留WOFOST审计值，不输出产量评价；不能用Native预测收成 |
| 氮 | 未实现 | 水分受限模型内采用潜在氮供给，保留原生作物组织氮过程 | 从历史NWLP改为WLP；不迁入受限土壤氮循环、施肥、外接氮事件 |
| 土温与通行性 | 未实现 | 不对公共工具承诺 | 不迁入旧土温滞后和设备作业可行性 |
| 病虫草、药效和残效 | 未实现 | 不加历史外接同化胁迫 | 防止把外接经验损伤误称作原生病理模型 |
| 种植设备、收获、烘干、仓储 | 未实现 | 无公共操作入口 | 不迁入旧农机、商品水分换算、库存与收获账本 |
| 成本、补贴、经营、轮作 | 未实现 | 未实现 | 不迁入multiobjective扩展 |
| 空间、多农场、多主体 | 单矩形地块 | 单PCSE实例 | 未实现垄间流动、共享设备或Multi-Agent；不据此定义L4 |

WOFOST “内部在运行”不表示本轮已经验证那些生物过程的农业有效性。尤其不能将审计中的 TAGP/TWSO 当成已验收的全季产量。

## 3. Native 方程、顺序和参数

实现位置：`native/backend.py`。每天先处理全部水输入，继而径流、排水、蒸发、蒸腾，最后累积积温。令根区厚度为 Z（mm）、含水率 θ、储水 W=θZ，雨量 P，毛灌溉 I，效率 η。

```text
W1 = W + P + ηI
R  = max(0, W1 - θsat Z)       # 饱和以上的水离开为径流
W2 = W1 - R
D  = max(0, W2 - θfc Z)        # 每日自由排水至田间持水量
W3 = W2 - D
ETdemand = ET0 × Kc
E = min(ETdemand × (1-cover), max(0, W3 - θwp Z))
W4 = W3 - E
stress = clip((W4/Z - θwp)/(θfc - θwp), 0, 1)
T = min(ETdemand × cover × stress, max(0, W4 - θwp Z))
Wnext = W4 - T
application_loss = (1-η)I
balance_error = W + P + I - Wnext - R - D - E - T - application_loss
GDDnext = GDD + max(0, (Tmin+Tmax)/2 - Tbase)
```

这是一种守恒的简化水量模型，不是 Richards 方程求解器，也没有标定成FAO-56完整算法。蒸发优先于蒸腾，会影响可用水分配；模型将低于萎蔫点的水视为不可继续蒸发／吸收，这是明确的简化。

| 参数 | 本版值 | 来源／性质 |
| --- | --- | --- |
| 地块 | 100 × 10 m，1000 m² | 新场景工程设置 |
| 固定根深 | 0.4 m | 已与FAIRY-test原始土壤默认根深复核；非实测 |
| 萎蔫点／田间持水量／饱和度 | 0.14 / 0.31 / 0.45 | 显式工程假设；历史默认为0.14 / 0.36 / 0.48，不能当作不变迁移 |
| 播种时含水率 | 0.22 | 同一场景初始输入，由后端各自解释 |
| 灌溉效率 | 0.92 | 已与FAIRY-test原始模型默认值复核，未做场地效率校准 |
| 固定冠层比例／Kc | 0.35 / 1.0 | 新增简化参数，不继承旧动态冠层 |
| 基温 | 8°C | 明示的大豆简化积温假设，无品种校准 |
| 出苗／成熟积温 | 95 / 1100 °C·d | 首次核对jev，现已与FAIRY-test的STANDARD代理数值复核；计算方法已改变，不保证物候等效 |
| 日最低／最高温 | 16 / 28°C | 固定合成输入，不是观测 |
| 雨量／ET0 | 0 / 3 mm/day | 固定合成强迫；不从温度或辐射重新计算ET0 |
| 辐射／风速／水汽压 | 18 MJ/m²/day / 2 m/s / 12 hPa | 合成天气；Native目前不消费后三项 |
| 地理位置 | 45.7°N，126.6°E，150 m | 沿用旧示例位置作为工程输入，不声称本地校准 |

`thermal_progress=min(1,GDD/1100)` 只是热量进度，`stage` 只区分出苗前与出苗后。不提供完整的V/R阶段或产量函数。

## 4. WOFOST 的独立适配

实现位置：`wofost/backend.py`。使用固定 **PCSE 6.0.13 / Wofost81_WLP_CWB**。相比历史 `Wofost81_NWLP_CWB_CNB`，本轮移除土壤氮限制，以聚焦补水任务；WOFOST 8.1仍包含组织氮与潜在氮供给过程，不能描述为“完全没有任何氮过程”。

离线 `soybean.yaml` 原样来自：

- 上游仓库：https://github.com/ajwdewit/WOFOST_crop_parameters
- 提交：`1ca91d7dbb5aaf6b41462162de03a8254812d3a5`
- SHA-256：`b65cf37a478542d74564094dc0760c69bd3cda99452eb6e7d327e457f87ba9a9`
- 选择：`Soybean_901`，北法区域参考参数，不映射成黑农84等中国商业品种。
- 授权：原YAML中的European Union Public License元数据及作者保留；源文件未声明EUPL具体版本。本次没有复制PCSE实现源码，PCSE作为独立依赖安装。

YAML通过本地解析展开继承，使用原参数值，不修改TSUM、密度或初始生物量以追求与Native相同的结果。唯一额外作物环境参数为显式CO₂=420 ppm。土壤SMW/SMFCF/SM0来自场景；RDMSOL=0.4 m转40 cm；SOPE/KSUB/K0=5 cm/day、CRAIRC=0.06为本轮明确的未校准假设。初始WAV=(θinitial−θwp)×土层厚度(cm)，SMLIM=θinitial；无地表蓄水、无非入渗降雨比例、无地下水。

公开输入 mm→PCSE cm（÷10）、MJ/m²→J/m²（×10⁶）、m→cm（×100）。ET0及开阔水面／裸土潜在蒸发在本合成场景均设为3 mm/day，属简化强迫，不是假称分别由Penman–Monteith计算。纬经度／辐射仍由原生冠层过程消费。

PCSE一般在 `run(1)` 末尾就计算下一天的速率。适配器延后该计算，在Runtime提供实际当天的天气与水输入后算 rates，再积分一天。下一天天气占位不参与积分；没有覆盖或改写PCSE的水分、生长方程。此做法依赖PCSE时序接口，故固定版本并用恢复、守恒和日界测试约束。

根区含水率是原生SM，根深是RD；Native则固定根深。因此，同场景定义不意味着两个引擎在任务起点拥有完全相同的状态或同剂量响应。全剖面水量守恒使用原生WWLOW+SS及蒸发、蒸腾、下层损失、径流通量检查，不把动态根区储水变化直接误判为漏水。

## 5. 验证证据与未覆盖项

运行命令、实测工程结果见根目录 README 和 `demo/README.md`。验收源代码为 `demo/test_pilot.py`；生成产物位于忽略的 `.runs/`，可重建。每个运行产物带场景指纹、引擎版本、完整请求／结果、逐日通量和审计终态。

| 主张 | 验证方式 | 不能由此推断 |
| --- | --- | --- |
| 补水改变状态、参考策略完成任务 | 两后端Oracle及无灌溉对照 | 真实灌溉推荐或全局最优 |
| 水量守恒与单位正确 | 两后端日平衡、Native大雨溢流、地块面积换算 | 分层水文或小时级响应正确 |
| 日界唯一、调用顺序可控 | 午夜、同日读取、分段与多日推进 | 全部全球时区／历史日期都已验证 |
| 待结算水不会重复应用 | 新Python进程恢复、结算后恢复、继续推进 | WOFOST恢复已达长轨迹性能目标 |
| 轨迹可再执行 | 新实例严格回放、篡改回执／审计检测 | 历史旧格式产物兼容 |
| Agent只接收公开投影 | 模拟客户端捕获实际请求、检查隐藏字段 | 对任意恶意Python插件的安全隔离 |
| 工具与引擎故障边界清晰 | 非法／超额拒绝、积分后注入异常回滚 | 对所有PCSE异常已有恢复保证 |

尚未做的科学验证：土壤／冠层参数敏感性、区域或品种校准、真实天气对照、蒸散实测对照、生物量／产量验证。尚未做的工程优化：日志分段、WOFOST增量快照、Oracle搜索缓存、多主体调度、概率事件、LLM真实服务效果评价。

## 6. 后续迁移门槛

首轮验收通过后，仍应逐项引入能力：先为新增物理过程补参数来源、守恒／响应测试和过程说明，再增加相应工具及场景。阶段管理／全季需要先补Native动态冠层、生物量、阶段语义及终态目标；农机需要独立审查作业时间、资源和失败副作用；经济与多季需要独立账本；可视化再消费既定公开／审计投影并绑定工具。

不以旧Oracle“通过”直接替代新模型验证，不把旧场景整体复制视为已迁移。

## 技术依据

- PCSE模型和变量说明：https://pcse.readthedocs.io/en/stable/code.html
- PCSE运行与参数接口：https://pcse.readthedocs.io/en/stable/user_guide.html
- PCSE实现：https://github.com/ajwdewit/pcse （本轮验证依赖6.0.13本地安装源码）

这些依据解释PCSE接口与原生模型边界；Native的简化方程和场景参数属于本文明确披露的工程选择，不能借官方来源声称已完成田间校准。

## Initial ontology/runtime migration — 2026-11-15

This migration uses the pinned FAIRY-test source as a semantic audit reference; no historical runtime imports or App compatibility wrappers are introduced. The new code keeps provider definitions and operations together and separates passive objects, explicitly exposed operations and backend process interfaces.

| Historical source | Decision and new location | Difference / acceptance evidence |
| --- | --- | --- |
| `apps/app.py`, `tool_utils.py` | Replace with ontology contracts and explicit runtime registry | No class scanning or universal clock-bearing App base; schemas and argument checks share declarations; duplicate/hidden/provider checks tested |
| `apps/farm_world/sensor_app.py` | Initial SoilSensor in ontology infrastructure | Retain observation intent; do not copy six ridge zones, 5 cm claim or per-read noise; both backends expose deterministic root-zone VWC; purity/recovery tested |
| `apps/farm_world/field_ops_app.py` | Initial IrrigationSystem in ontology infrastructure | Merge irrigation aliases into mm-based irrigate; no hours-to-fixed-VWC increase, no physical update during validation; quota/volume/single settlement tested |
| `apps/farm_world/weather_app.py` | WeatherService in ontology service | Explicit daily reports and forecast queries, separate from private forcing; no cached field VWC, implicit day changes or silent days clamping; timezone/privacy/horizon tests |
| `apps/system.py` | Runtime-owned advance_time | Unique public clock and daily boundary; no second commit_daily_physics operation; waiting/notification scheduler deferred |
| `apps/agent_user_interface.py` | Existing task/context and controller termination retained | No agent-callable send_message_to_agent; broader communication migration deferred |
| `apps/farm_world/farm_world_app.py` | No whole-class migration | Plot configuration and backend-owned state already separated; inventories, storage and further world behavior require individual review |
| `apps/farm_world/drone_app.py`, `robot_app.py` | Future equipment increment | Requires spatial/coverage model, energy accounting and observation semantics supported by selected backend |
| `apps/farm_world/tractor_app.py` | Future equipment/material increment | Split loading/implements/inventory from planting/protection/harvest; implement only after relevant biological processes and resource accounting are validated |

Migration roadmap: (1) current explicit loading plus soil/irrigation/weather; (2) one drone survey scenario with declared coverage, duration and energy; (3) robot observation where process support exists; (4) tractor state/implements/material transfers, then individual agricultural jobs; (5) full-season composition and postharvest processes. Each increment needs independent acceptance before scenario expansion. This is a roadmap, not a claim those capabilities already exist.

Original L1 remains a three-operation numerical regression fixture. A separate weather L1 adds current daily weather and next-day forecast; success requires successful queries before irrigation as well as the original moisture task. Public reports are perfect synthetic weather in this fixture, not validated field forecasts. Physical execution, noise, water-source inventory and live forecasting remain unmodeled.
