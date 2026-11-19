# FAIRY自动Oracle首轮实现

本轮依据“整理 FAIRY Oracle 实施文档”所引用的“分析农业Oracle构建”讨论开发，落在kun分支的experimental目录。
参考上下文已保存为本机 `.runs/oracle_autogen_context.json`。该讨论中的远端玉米/WOFOST方案不是本地能力事实；
本轮选择已重建的 `soybean_l3_hn58_normal_v1` 与Native全季后端，没有移入旧App、玉米、施肥或WOFOST全季流程。

## 目标与实施边界

人工/离线知识给出作物生命周期目标与证据需求，固定LLM决定所有实际工具调用；数值网络只建议剂量；
三值检查只反馈公开事实；运行后经验在下一轮按阶段检索；独立评价与新实例回放决定参考是否成立。
没有引入HTN/PDDL求解器，没有后台代替LLM整地、播种、等待、收获或入库。

保留层次目标、前置条件与黑盒数值顾问的思想。五阶段工作流与生物阶段分开：
播前准备、播种出苗、生长期管理、收获、收后处理；阶段由真实公开回执和作物观测确认，不能由模型宣布。
生命周期卡中的操作是非穷尽的相关操作提示，不是硬编码顺序或封闭工具白名单；统一时钟工具始终可用。

## 模块位置

| 位置 | 职责 |
|---|---|
| `fairy/agents/loop/context/kg/` | 生命周期YAML、闭合Schema、来源哈希、离线LLM提取 |
| `fairy/agents/loop/controller/autogen/` | 公开证据、阶段投影、三值检查、数值建议服务和经验快照 |
| `fairy/agents/models/numeric/` | 公开特征、动作掩码、MaskablePPO训练与冻结推理 |
| `evaluate/oracle/` | 基于现有Runtime的Gymnasium适配、独立资格审查及候选筛选 |
| `interface/cli/oracle.py` | audit / extract / train / evaluate / generate / resume / induce / qualify |

所有目录均有范围README。原执行循环新增可选公开上下文扩展，不另建作物引擎或仿真时钟。
Runtime未向扩展传递后端状态，农事仍使用原OperationRequest入口。物理事件和Agent本地服务决策分别保存。

## P0与P1 场景和知识

保留0.6.0轮包及本轮开发中每个冻结源码快照。主干人工Oracle及四场景真实LLM旧结果只作外部参考，
不把人工计划传给生成器执行，也不将其当全局最优。

已用当前Qwen服务执行真实离线提取。首个完整示例导致照抄；第二个输出出现包裹字段和哈希错误，被拒绝。
改为只让模型提取阶段内容，程序附加来源、工具契约指纹和固定证据判据后，得到已校验版本。
版本化文件为 `fairy/agents/loop/context/kg/soybean_hn58.yaml`，审查状态为machine_checked，不伪称人工批准。
没有固定日期、灌溉剂量、人工计划序列或潜在产量评分。原始响应、失败尝试及provenance均保留。

发送本地源码的离线步骤已获得本次用户明确授权。CLI默认仅提供公开契约和来源身份，
`extract --include-source-code`才会包含源码；运行期只提供阶段卡、公开观测和已验证经验，不发送源码或未来扰动配方。

## P2 三值检查和执行保护

- PASS：所检查的公开条件满足，不表示已证明所有未知资源和田间可收获质量均合法。
- BLOCKED：参数、额度、已观察到的阶段/通行/容器条件或明确的生成协议不满足。
- NEED_OBSERVATION：当前证据缺失、覆盖不足或过期，要求模型自行获取观测。

硬物理条件与生成协议分别标记来源。每日数值窗口、整田建议和建议匹配属于生成协议，
没有假称底层Runtime禁止原本合法的部分田块作业或追加pending。
预检查拦截不生成虚构的物理请求/结果，不计入实际工具错误；真实执行拒绝仍如实计数。
同状态重复拦截和无进展都有有限预算；新信息不会被误判为无进展。检查器不执行修复工具链。

前轮双阶段出现的XML样式伪工具文本现由 `chat_tools_v6` 识别为协议错误，进入原有受限反馈重试；
不会自动解析或执行文本。正常纯文本结束仍保留。收获契约明确Native要求R8。

## P3 成功和失败经验

JSONL记录包括作物/品种/后端/工具契约范围、阶段、工具、来源产物指纹、真实事件或决策步骤、局部范围和归纳方法。
只有来源可独立回放、且存在真实成功回执或成功修复对的候选才标记verified；失败运行中的局部成功不冒充全季成功。
时间推进与后续复查也提取为工作流经验，避免只保存设备操作而丢失CPS的时间语义。

规则抽取先产生可核实候选；可选LLM在候选基础上归纳文字，不能生成来源哈希、改变证据ID或灌溉参数。
模型只引用序号，程序附加原始来源。检索按阶段和兼容profile筛选，最多两条成功和两条失败。
单次运行固定经验快照及哈希；运行结束后才更新JSONL。此为AWM启发的工程机制，不声称复现论文完整方法。

## P4 真正训练的剂量网络

复用SB3-contrib的MaskablePPO算法，不复用WOFOSTGym的环境、权重或整农场动作编码。
五个剂量档位只属于数值顾问：0、5、10、20、30mm整田毛灌溉。所有农事工具选择仍由LLM决定。

输入顺序：公开阶段、根区VWC均值/最小/最大、已发布预报、当前ET0、剩余及待结算水量。
没有真实生物量、内部积温、未来天气表或Oracle答案。缺少观测不能伪装成零建议。

训练适配器仍调用现有公共工具和次日结算。固定播前/收后驱动只用于隔离剂量学习，不能算作LLM生成成绩。
训练种子0–7生成不同的逐日天气及初始土壤条件：降雨2.7–3.3mm、ET0 2.8–3.2mm、初始VWC 0.29–0.32。
这些是独立、有来源的normal条件变体；没有改写注册normal，也没有暗中加入干旱来制造收益。
留出种子100–104，另评固定normal。固定normal是已知的工程验收基线，不宣称未见过的统计测试集。

训练预算2048步，CPU两层32单元网络。记录Actor实际更新、权重哈希、特征顺序、归一化、动作掩码、依赖版本和训练/评估种子。
奖励预先固定为合法终态的实际安全入库干物质/3500，逐步扣除0.2×用水/额度；失败终态为-1。
它是本实验目标，不成为FAIRY的通用综合分，不使用潜在生物产量。

当前评估的负结果保留：固定normal的零投入、固定10mm探针和PPO均入库约3224.917kg，
后两者用水60mm，零投入用水0。网络真实训练和接线已成立，数值收益尚未证实，不能称合格最优专家。

## P5 候选与审查

实际LLM轨迹才可成为自动工作流参考；训练驱动或脚本测试不能冒充LLM。
资格审查重新执行物理事件，并核对决策轨迹覆盖、实际请求/结果指纹、唯一数值决策及灌溉建议绑定。
分别报告任务成功、物理/协议错误、预检查拦截、数值窗口遗漏和独立回放。

候选筛选不因“使用学习网络”而过滤掉表现更好的非学习基线：
`selected`为所有合格实际工作流中的最佳已知参考；`selected_trained_stack`另报告学习组件完整运行的参考。
使用已声明的实际入库/用水目标，只在列出的尝试中比较，不证明全局最优。

首轮0.7.0的三条真实轨迹都发生无进展终止，已用其冻结源码在无可选依赖的新进程回放通过。
发现同日反复重新计算建议和零建议未结束窗口后，0.7.1改为每日一次数值决策：
正建议可以在不改变剂量的情况下对新公开证据重新签发证书；零建议完成本窗口。
反馈明确时钟未推进及下个本地窗口，程序仍不自动等待。第二轮真实结果另行写入验收记录，不覆盖首轮。

## 复现入口

以下从仓库根执行，输出路径必须是全新路径：

```bash
.venv/bin/python -m pip install -e './experimental[oracle,llm,test]'
.venv/bin/python -m interface.cli.oracle audit --out .runs/new-oracle/audit.json
.venv/bin/python -m interface.cli.oracle train --out .runs/new-oracle/policy.zip --timesteps 2048
.venv/bin/python -m interface.cli.oracle evaluate --policy .runs/new-oracle/policy.zip --out .runs/new-oracle/numeric-evaluation
.venv/bin/python -m interface.cli.oracle generate --policy .runs/new-oracle/policy.zip --out .runs/new-oracle/candidate
.venv/bin/python -m interface.cli.oracle induce .runs/new-oracle/candidate/run.json --memory .runs/new-oracle/memory.jsonl
.venv/bin/python -m interface.cli.oracle qualify .runs/new-oracle/candidate/run.json --out .runs/new-oracle/qualification.json
```

真实模型通过已有 `FAIRY_LLM_MODEL`、`FAIRY_LLM_BASE_URL`、`FAIRY_LLM_API_KEY`配置；不在仓库保存凭证。
`generate --numeric zero`为显式无学习对照；`induce --llm-condense --model ... --out ...`启用实际模型归纳。
`resume`只接受未终止检查点，复用其知识、经验和建议状态，累计预算；变更来源或权重会明确拒绝。

本机证据目录：`.runs/oracle_autogen_v1/`（首轮全部失败）与 `.runs/oracle_autogen_v2/`（修正后复测）。
这些目录不入库。第三轮证据为`.runs/oracle_autogen_v3/`。本轮冻结代码为0.7.2，Runtime状态schema仍为6，新增的是Agent元数据而非物理状态字段。
旧产物应使用匹配的冻结轮包或源码快照回放，不改写版本/指纹伪装兼容。

## 0.7.2 观测保留修复与完整复现

第二轮仍失败后，受控复现确认：插入本地建议回执会替换近期物理历史，但旧观测记忆误以为那些物理回执仍保留，
导致模型实际可见的四种当前观测从四种变成零种。这是上下文拼接错误，不能只归因于模型。
0.7.2在混合历史后重新构建公开观测记忆，保持原始证据和时间戳；同日播种也会使播前作物观测失效。
新增了针对这两类问题的回归测试。没有改写物理状态、隐藏工具或自动推进时间。第三轮结果以独立验收记录为准。

仓库内提供可配置的有限尝试入口，不依赖本机`.runs`中的专用脚本：

```bash
.venv/bin/python experimental/demo/oracle_mvp.py --out .runs/new-oracle-experiment --attempts 2 --zero-control --llm-memory
```

它使用环境中的模型配置，自动训练（或加载`--policy`）、评估、生成候选、独立回放、在运行间归纳经验、筛选并导出参考。
输出目录必须全新；每轮失败都保留。非学习基线与学习组件完整参考分开报告。


## 数值留出集的负结果

五个normal天气变体（种子100–104）的每种策略均完成5/5任务并通过5/5严格回放。
按训练前声明的入库/3500减去0.2×用水/60目标：

| 剂量策略 | 平均实际入库干物质kg | 平均灌溉mm | 平均实验目标 |
|---|---:|---:|---:|
| 零灌溉对照 | 3130.631 | 0 | 0.894466 |
| 固定10mm探针 | 3146.722 | 60 | 0.699063 |
| 2048步PPO | 3157.903 | 60 | 0.702258 |

PPO比零灌溉多入库约27.272kg，但消耗全部水额，声明的目标更差。不能用仅报告入库量的方式声称训练成功。
18次比较包含这15次变体和3次固定normal运行；它们属于数值训练驱动，不计入真实LLM工作流成功次数。
证据：本机`.runs/oracle_autogen_v3/numeric_evaluation_final/comparison.json`。


最终真实结果、两条合格参考及第三条因工作区并行更新至0.8.0而中断的说明见[验收记录](AUTO_ORACLE_VALIDATION.md)。本说明描述冻结0.7.2，复现旧产物须使用匹配轮包。
