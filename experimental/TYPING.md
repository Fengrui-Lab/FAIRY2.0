# 类型、注释与IDE导航说明（0.8.2）

本轮覆盖`fairy`、`evaluate`、`interface`的68个生产Python文件。
所有298个函数签名（含协议、重载及局部辅助函数）都有参数/返回注解，核心类、方法和属性补充了职责、单位、
状态所有者、时间副作用与异常说明。这里的类型增强不改变生长公式，也不代表模型已校准。

## 从backend跳到哪里

现在`SoilSensor.observe`明确声明：

```python
def observe(self, backend: BiologyBackend, now: datetime) -> SoilReading:
    ...
```

在IDE中对`backend.observe`使用“转到定义”，目标应为
`fairy/acps/backends/biology/contracts.py:BiologyBackend.observe`。
使用“转到实现”或在该协议查看实现，可定位到显式实现协议的：

- `NativeBackend`：L1简化水量模型。
- `NativeSeasonBackend`：Native全季。
- `WofostBackend`：单PCSE土柱。
- `WofostSeasonBackend`：64土柱全季管理。

如果变量来自运行时配置字符串，静态分析不能预知它最终选哪一个引擎；显示共同协议是正确行为。
当调用工厂时给出明确的名称和全季配置，`make_backend`的重载会推断具体类。
`Runtime.backend`是共同协议；`season_backend`、`season_physical`、`season_config`只在全季场景中可用，
在L1误用会明确报错，而不是将全季方法伪装成所有后端都支持。

## 本轮补强的边界

| 层级 | 主要类型与作用 |
|---|---|
| Ontology | `ObjectSpec`、`OperationDefinition`、`OperationTiming`、请求/结果与提供者声明 |
| 生物后端 | `BiologyBackend`、继承它的`SeasonBiologyBackend`、类型化工厂与具体实现重载 |
| 公开生物数据 | `SoilReading`、`CropObservation`、`SoilRidgeObservation`、`SurveyObservation`、`HarvestProjection` |
| Native状态 | `NativeWaterState`、`NativeRidgeState`、`NativeSeasonState`，水量/生物量单位就地说明 |
| WOFOST状态 | `WofostRidgeState`保存适配器管理数据，PCSE仍拥有自己的生长/水量真值 |
| 物理账本 | `PhysicalBackend`、`PhysicalState`、`GrainBatch`、`StoredBatch`、`WorkPlan` |
| Runtime | 场景、对象目录、操作目录、后端、时钟、pending、事件等关键属性显式注解 |
| Agent | 泛型`Agent`区分`PublicAgent`与`PrivilegedAgent`；`Decision`只表达意图 |
| 上下文 | `PublicContext`、`PublicStatus`、`HistoryTurn`区分公开结构与可信恢复数据 |
| 自动Oracle | `ContextExtension`、三值`Check`、`NumericAdvisor`和`IrrigationAdvice` |
| 评价与来源 | `TaskMetrics`、`SourceIdentity`；任务附加指标不混成通用总分 |
| 服务/CLI | 最小`ChatClient`协议、完整函数签名；可选SDK继续懒加载 |

绑定处理器由带类型的工厂创建，替代依赖默认参数捕获的lambda；仍在执行时读取当前Runtime，
因此事务回滚替换后端后不会继续调用旧实例。

## 静态检查怎么运行

从仓库根执行：

```bash
.venv/bin/python -m pip install -e './experimental[typing,test]'
.venv/bin/python -m mypy --config-file experimental/pyproject.toml
.venv/bin/python -m pytest -q experimental/demo/test_typing.py
```

本轮在已有PCSE、OpenAI、NumPy、Gymnasium及训练依赖的Python3.14开发环境中检查通过。
全模块开发检查会分析这些集成边界；仅安装Native运行依赖与完整开发环境是不同用途。
生产代码的语法另按Python3.10解析检查通过，不等于已经在所有Python版本上完成运行测试。

配置启用`check_untyped_defs`、`disallow_untyped_defs`，覆盖全部三个生产包。
PCSE、Torch、SB3-contrib的外部实现由薄适配器隔离，配置仅对这些第三方模块跳过深入分析；
没有对项目模块设置`ignore_errors`，没有用全局`type: ignore`压制错误。

`demo/typing_contracts/positive.py`用`assert_type`验证具体工厂返回类型、公开观测和Agent边界；
`negative.py`包含6个故意错误，测试必须确认它们被拒绝：错误后端、时间/天气类型、
对共同后端调用全季方法、错误粮食字段，以及给公开Agent传Runtime。
这些文件只交给检查器，不会执行引擎或真实模型请求。未安装mypy时对应pytest测试会明确跳过。

## IDE配置建议

先选择仓库`.venv/bin/python`解释器并确保可编辑安装路径指向`experimental`。
若Pylance不能解析`fairy`，在已有工作区设置中合并下面一项，而非覆盖整个设置文件：

```json
{
  "python.analysis.extraPaths": ["./experimental"]
}
```

三个顶层包都包含`py.typed`，轮包也携带类型信息。
不同IDE的“转到实现”支持不同；共同协议、显式实现类和带重载的工厂均可直接作为导航入口。

## 保留动态类型的地方

`JsonObject = dict[str, Any]`仅用于工具JSON、外部SDK、元数据、审计/恢复序列化等动态边界。
进入领域后使用数据类、TypedDict或Protocol；类型注解不代替运行时Schema、单位、额度和守恒验证。
`argparse.Namespace`及部分外部引擎状态仍是动态接口，当前并非完全消除Any的严格形式化类型系统。
同为float的mm和cm也不能靠mypy自动区分，仍由适配器转换、注释和单位测试保证。

## 版本与数值行为

- 版本更新为0.8.2；Runtime schema7、数值特征/策略schema2不变。
- 明确重建了源码绑定的Native/WOFOST参考日历，阶段日期与基线输入身份不变。
- HEINONG58知识卡的阶段内容与公开操作契约不变，仅重验证新源码来源；原始LLM提取身份保留在provenance，
  新条目标记`new_llm_extraction=false`，没有把本地复核冒充新的模型提取。
- 同一公共操作驱动分别运行冻结0.8.1和当前0.8.2，Native normal/双阶段干旱、WOFOST normal/干旱补水
  四组的全部评价指标、阶段日历与结束时刻一致。
- 全量回归396项通过；新类型契约测试2项。此前生长敏感性审计发现的零ET0、密度/播深等限制没有在本轮悄悄修改。

即使只是注释/类型变更，全包源码指纹也会改变。旧权重与旧产物继续使用匹配冻结轮包，
不修改其版本或哈希绕过保护。0.8.2需新生成运行产物，继续训练时也需绑定本版来源。

本机证据在`.runs/typing_upgrade_20261117/`，包含mypy、回归、参考资料重验证、旧新版行为比较和轮包检查。
本轮未发起真实LLM全季重跑；运行逻辑由静态检查、现有回归及数值对照验证。
