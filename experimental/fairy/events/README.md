# Runtime

`Runtime` 是场景、ACPS 与 Agent 的桥梁。它拥有唯一仿真时间及有序事件，不负责决定何时灌溉。

## 时间与操作

- 场景显式使用 `Asia/Shanghai`，时间输出含偏移；推进小时表示实际流逝时长。
- 初态从5月1日播种预运行至5月9日，使用同一组逐日固定合成天气和空管理前缀。Native/WOFOST 分别生成自己的阶段状态，不复制中期变量。
- `observe_soil` 只读取根区含水率并四舍五入到0.001；它增加观测记录，不改变物理或时钟。本版是无噪声传感器，未来再实现受控随机采样。
- `irrigate` 登记毛灌溉水深并立即占用预算，不推进时钟。多次登记合并为待处理输入。
- 每跨过当地午夜，**前一天**天气和截至该边界的待处理灌溉积分一次，再清空待处理量。例如5月9日07:00登记的水在5月10日00:00随5月9日的日模型结算。没有小时内物理过程。
- 同一天内推进、07:59／08:00读取均不重复计算。连续推进和分段推进物理终态一致，但时间调用事件数量可以不同。
- 公共非法调用返回稳定错误码，无物理和水量副作用。NaN/Infinity 等非 JSON 请求在序列化边界抛出异常且不写事件。
- 引擎异常回滚该次推进中的物理、时钟、待处理水和暂存日事件，记录 `engine_failed` 后向调用方抛出。循环终止为 `execution_error`，不伪造成功结果。

## 事件与公开边界

事件是已发生事实，`EventRecord` 包含 `seq/kind/time/cause/payload`，载荷内部保存不可变 JSON 字符串；读回得到副本。

`operation_requested` → `environment_day_completed`／`time_advanced`（适用时）→ `operation_result` 通过请求序号关联。另有 `run_started`、`run_terminated` 和 `engine_failed`。本版无需通用调度队列；待处理灌溉和下一午夜构成最小调度状态，不把可执行回调保存为事件。

Agent 的上下文包含任务、公开对象身份、公共预算／时钟、已开放操作说明及最近的完整请求／结果对。原场景另含声明的公开合成天气；新天气场景通过操作获取报告。内部状态、参数日志、WOFOST 变量和快照不进入模型请求。此边界针对模型数据访问；Python 插件自身属于受信任代码，不提供进程沙箱。

Oracle 路径明确标记 `privileged=true`，可以读取完整状态并在副本上试算，但实际动作仍经 `Runtime.invoke` 校验和结算。

## 状态、产物与回放

`export_state()` 保存场景、输入指纹、FAIRY／后端版本、时钟、配额、待处理水、观测、事件及后端恢复包。`Runtime.from_state()` 恢复到新实例，校验版本、输入、引擎日游标和时钟。

- Native 保存完整数值状态，物理恢复成本与已走过天数无关。
- WOFOST 保存初始化参数身份和完整逐日输入日志；在新 PCSE 实例中从播种重建，成本为 **O(历史天数)**。不 pickle PCSE 对象，不声称已经实现常数成本快照。
- 事件历史仍随轨迹增长，模型上下文独立受限。本轮未实现日志分段、增量 checkpoint 或 Oracle 缓存。
- 顶层产物额外保存审计终态、独立指标、Agent 身份／用量和 Python／依赖版本。凭证、URL 用户信息与查询参数、模型原始推理和 API 异常正文不写入；记录脱敏后的服务地址与请求超时。

`replay` 从场景初态重新执行原始请求，不调用 LLM，不把结果字段直接灌回状态。比较事件、错误码、时钟、待处理输入、恢复包、审计终态和指标；浮点绝对容差1e-9、相对容差1e-10，结构和非数值字段必须一致。引擎或输入不兼容时失败，不放宽校验。

`inspect` 仅解析和输出原 JSON，无仿真副作用；`evaluate` 对恢复的状态计算指标，WOFOST 的恢复会重建历史；它不等于独立严格回放。工程异常轨迹可能无法成功重放，应保持回放失败状态。

恢复包和实验产物是受信任的本地格式，当前没有签名或针对恶意篡改的完整性保证；需要证明重现时执行 `replay`。

## Explicit ontology loading and operation execution (0.3)

Each ScenarioSpec declares immutable object specifications, ordered operation bindings and separate public weather reports. Loading uses an explicit type allowlist, with no filesystem discovery or import-time registration:

1. Validate object IDs, passive plot identity, target references and provider-operation pairs.
2. Build a per-session operation registry from the exposed bindings. Objects can offer more operations than the scenario exposes.
3. Construct the selected biology backend, check identity/interface/crop/actions/local-day support, and compute the binding fingerprint.
4. Pre-run the shared initialization prefix only after those checks pass.

`registry.py` owns object composition and immutable operation mappings. `operations.py` declares runtime time advancement. Provider modules own their agricultural operation declarations/behavior. Parameter schemas and validation share the same Parameter definitions. The model sees only identity/type/target links and exposed tool schemas, never the object registry, backend instances or audit state.

`Runtime.invoke` resolves and validates a registered operation, then executes its handler under a rollback boundary. Successful results are checked against declared result fields and copied into events. Expected failures return stable public codes; unexpected failures roll back process state, time, pending water, quota and observations, record engine_failed and propagate only to the trusted controller. Handlers resolve the current backend, so rollback replacement cannot leave stale bindings.

The first version has one plot and one bound provider per public operation. A default operation target is explicitly represented by its provider ObjectSpec.target_id. L3 adds ridge ranges within the declared plot, material stock transactions and serial atomic physical jobs. Dynamic provider registration and parallel/preemptible jobs are not implemented.

## Recovery compatibility

FAIRY 0.5.0 uses runtime state schema 5. Operation definitions (including versions, arguments/results and timing), object links, provider bindings and backend capability identity contribute to a binding fingerprint. Restore rebuilds objects and handlers and verifies this fingerprint before loading backend history. Replay verifies the state schema and current binding before prefix warmup or replayed operations. Scenario fingerprints also include public reports and exposed/required operations. No callable is serialized.

Artifacts retain outer schema 1 but must contain the new runtime state and FAIRY version. Older pilot artifacts are explicitly rejected; regenerate them. Historical FAIRY App names and trajectories are not accepted. Operation implementations must bump their declaration version when semantics change, and backend models must maintain their own version metadata.

Current implementation limits: object construction and provider handler binding use explicit built-in branches. Adding a new provider requires extending that composition point; this is not a plugin-discovery framework. Every resolved request snapshots backend state before execution. Expected business rejection (for example quota exceeded) currently restores that state too, so WOFOST can incur O(history) reconstruction on rejection. Schema errors and hidden-operation attempts also execute within the cancellation-safe intent boundary. Future preflight optimization must preserve rollback and no-side-effect guarantees.

## Shared full-season state

The same Runtime builds a declared season biology adapter and physical ledger.
Both are included in fingerprints, snapshots, rollback and replay. The physical
adapter plans without mutation; Runtime advances through all crossed midnights,
then revalidates and commits the job. A rejected completion rolls back the clock,
biological changes, physical ledger and intermediate events as one transaction.
`ScheduledJob` is a proposed schedule, distinct from immutable occurred events.
Jobs are serial, non-preemptible and cannot be restored mid-job.

Spatial irrigation queues mm on each selected ridge. The runtime counts the
area-weighted field-average mm and m3 exactly once at request acceptance, then
settles each queue at the next local midnight. Restore checks spatial pending
and settled totals against the runtime quota ledger and checks harvested dry
mass against the physical inventory. Public tool results do not contain biomass,
latent grain reserves, or unmeasured field-loss truth.

## 0.4.1 恢复校验补充

恢复验证遍历事件因果、时钟、终止、错误、观测和资源账本，并核对
Native 累计天气及 WOFOST 固定输入日志。Native 无需重新执行物理；
结构检查成本为 O(events)。这不替代严格回放，也不是签名认证。
极小推进若在 datetime 精度下未改变时间，返回 TIME_STEP_TOO_SMALL。

## 0.5.0 中断与执行身份

状态 schema 5 记录可执行 `.py` 和打包 `.json/.yaml` 的内容指纹；顶层环境保存逐文件清单。
从 wheel 或源码读取相同包内容可获得同一身份，不依赖本机绝对路径或 Git 可用性。
源码/参数内容不同即拒绝恢复及回放；旧 schema 4 保留 inspect，不修改版本号伪装兼容。

可捕获的 KeyboardInterrupt 会回滚整次操作，包括已更新的日状态、装备、时钟和错误计数；
记录原请求及 `operation_cancelled`。执行循环记录 `run_suspended`，done 保持 false，CLI 保存后返回130。
严格回放只复现取消事实，不执行被取消操作；恢复后可重新选择下一操作，待处理水只结算一次。
该机制不承诺 SIGKILL、断电或连续中断保存过程时的持久恢复，也不提供作业中途检查点。

实验 JSON 默认原子且拒绝覆盖，CLI 同时保留独立进度文件和唯一 attempt_id。
`write_json(..., overwrite=True)` 仅供明确需要替换的开发快照；正式 CLI 没有覆盖选项。

## 0.6.0 构建输入审计

当前状态schema为6。可选construction_json保存normal来源、管理模板、冻结日历、配方与最终天气指纹。
场景恢复会重新检查“正常输入＋降雨配方”与持久化天气完全相符；不执行normal参考、不重排扰动。
构建场景的每日environment_day_completed增加forcing审计：构建指纹和当天生效的扰动ID。
重复推进、恢复或读取不能重复应用同一天；未来计划不进入公开模型上下文。
旧0.5.0/schema5说明保留为历史行为记录，旧产物仅inspect。详见[构建合同](../../evaluate/scenarios/CONSTRUCTION.md)。
