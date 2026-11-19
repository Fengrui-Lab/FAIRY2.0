# Agents

`models` 接入决策模型，`loop/context` 构建公开上下文，`loop/controller` 维护执行和停止条件。现有 Jev、KG 目录继续保留，本轮未接入。

## 特权参考策略

`IrrigationOracle` 在天气场景先调用当前日天气与次日预报，再按基础策略调用土壤观测，再读取完整恢复状态，在独立副本中按1 mm网格搜索当前小配额内的灌溉量（含分数配额端点），大配额最多取128个候选，并分段试算至截止时刻，选择终态含水率最接近目标区间中点的剂量。实际运行再调用灌溉、推进和复查。

它知道固定未来天气并有完整状态权限。搜索目标是最接近区间中点，没有把节水作为第二目标，也没有证明连续动作空间的全局最优。产物标记其权限、策略版本和 `optimality=not_proven`。WOFOST 每个候选需重建历史，本版优先可审计性，尚未做搜索性能优化。

## LLM

适配器使用一种 OpenAI-compatible chat tools 接口，每步至多一个工具调用；程序化适配器默认每次最多512输出 token、超时30秒；CLI 默认1024输出 token、请求超时120秒。不自动重试。SDK仅在选择 LLM 时需要。

```bash
export FAIRY_LLM_MODEL=your-model-name
export FAIRY_LLM_BASE_URL=https://your-provider.example/v1
export FAIRY_LLM_API_KEY=your-key
.venv/bin/python -m interface.cli run --backend native --agent llm --out .runs/native_llm.json
```

`OPENAI_API_KEY` 可作为凭证回退；不提供地址时使用 SDK 默认地址。不要把凭证写入仓库。CLI 的 `--model`、`--base-url` 优先于环境配置。

程序化循环默认公开上下文预算为12000个字符，保留至多8个完整请求／结果对；CLI 默认60000字符，L1保留8对、L3保留20对。超限从最旧的整对记录裁剪。保留任务、当前时钟、水量和待结算水；不发送内部轨迹或隐藏状态。这是字符预算，不能视为准确 token 预算。

程序化循环默认最多12次工具调用、累计3次工具错误即终止；CLI 默认L1为12次、L3为400次，累计错误上限均为10次。错误额度跨恢复累计，已耗尽时不再调用模型。错误 JSON、多工具响应和截断响应记为独立协议错误，不创建虚构农场调用，也不占用业务工具错误额度；协议错误默认累计3次终止，可用 --max-protocol-errors 配置。参数类型及业务预算仍由运行时校验。模型以正常文本结束时，仅表示模型停止，任务成功仍由评价器判断。原始模型文字不作为物理事实。

API 故障使循环终止并保存已执行轨迹，不记录可能带敏感信息的异常正文。用 `demo/test_pilot.py` 的模拟客户端可在无凭证情况下验证完整工具调用链；真实模型效果需另行运行，不能由模拟测试推断。

Operation schemas now come from the session registry. Model context includes only public object identities/type/target links; the weather fixture omits automatic future weather and obtains reports through its service operations. Core fixture task text and operation contracts use English.

## Shared task-independent loop

0.5.0 uses an explicit trusted strategy allowlist (IrrigationOracle / SeasonOracle).
Only those exact built-in types receive Runtime. LLMs receive task, public
constraints, objects, tools and bounded result history. The model system prompt
contains no irrigation sequence or L3 management recipe. Context adds VWC goals
only for irrigation tasks; L3 constraints declare cultivar, geometry and storage
requirements. Knowledge-library and expert-context experiments are not enabled.

The adapter reconstructs linked assistant/tool messages from bounded public
history. L3 also retains a compact progress summary from successful operation
receipts (planting/harvest coverage and storage transfers); it never reads crop
truth for this summary. Truncated/filtered tool replies are rejected even if
the argument JSON parses. The final model text is retained for diagnosis.

运行产物记录脱敏后的实际服务地址、请求超时及执行预算。URL 用户信息、
查询参数和凭证不保存。控制器在任务达成后自动终止；该可信判断不进入模型上下文。

## 0.5.0 续跑与诊断

循环从公开 operation_result/cancelled 事件重建历史，连续运行和恢复具有相同的公开证据来源。
除完整历史窗口，还保留最多12个最近观测目标和8个未被相同成功请求解除的拒绝；均附原记录时间，
不将旧观测称为当前真值。超预算按整条移除并记录遗漏数量，不读取隐藏状态补全。

最近32次公开回执产生时间/管理动作/充电/重复结果诊断；低进展只提供提示和审计，不强制等待或执行农事。
L3 Oracle 的等待同时遵守场景单次推进上限和总期限。

`chat_tools_v6` 保存每次请求/响应内容指纹、规范 JSON 字符/UTF-8 字节、finish_reason、
原始/有效调用数、原因码和服务返回 token 用量。字节计量指 SDK 传输前的规范逻辑负载，不是 wire 字节或 token 估算。
协议失败不进入物理事件，单独计额；取消、错误及随后有效响应均可追踪。API 异常只记录类型，不保存异常正文。
CLI 续跑用新 attempt 保存本段调用，并引用原不可覆盖产物，累计此前调用、token 与协议错误。

## 0.7.2 automatic reference generation

The optional public-context extension uses lifecycle knowledge, three-valued checks, a trained numerical adviser and frozen workflow memories. Actual work still uses this shared loop. Agent services never receive Runtime. See [implementation and limits](../../demo/AUTO_ORACLE_IMPLEMENTATION.md).
