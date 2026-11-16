# Interface

本轮仅实现现有 `cli`；`web` 保留为空，Three.js 素材与工具绑定后续再做。

```bash
python -m interface.cli run --backend native --agent oracle --out .runs/native.json
python -m interface.cli run --backend wofost --agent oracle --out .runs/wofost.json
python -m interface.cli replay .runs/wofost.json
python -m interface.cli evaluate .runs/wofost.json
python -m interface.cli inspect .runs/wofost.json
```

`run` 默认 Native/Oracle；使用不同后端时建议明确指定输出路径。`run --agent llm --model NAME --base-url URL` 使用配置的模型服务。`--max-steps` 控制工具循环次数。

`replay` 重新执行并严格比较；`evaluate` 恢复后计算指标，结果不会自动标记回放已验证；`inspect` 仅输出 JSON。

`resume INPUT --out OUTPUT` 默认将尚未终止的产物交给特权 Oracle 继续执行；`resume INPUT --agent llm --model NAME --base-url URL --out OUTPUT` 可在新进程恢复后交给真实模型继续决策。恢复本身不调用模型，后续新决策才调用；产物记录来源指纹与续跑起始事件。程序化调用可用 `Runtime.from_state(record['state'])` 后自行继续工具调用。不允许将已终止实验清除 done 标记后伪装成同一实验。

退出码：0=命令成功（run/resume 同时要求任务成功），1=运行正常结束但任务未成功，2=配置／引擎／回放／服务错误。执行循环中的异常也会保留已执行产物；初始化失败尚没有可保存的运行状态。

`run --scenario soybean_l1_weather_irrigation_v1` selects the ontology/weather validation fixture. Default remains soybean_l1_irrigation_v1. `--backend` still selects biology Native or WOFOST; L3 additionally uses the Native physical backend. Runtime state schema 4 requires FAIRY 0.4.1 artifacts; inspect can still display old JSON without running it.

## L3 on the same CLI (0.4)

Use `run --scenario soybean_l3_hn58_waterlimit_v1 --backend native`; `--agent`
selects oracle or llm. L3 defaults to 400 steps, L1 to 12. `--max-errors`,
`--context-chars`, `--request-timeout`, `--max-output-tokens` and optional
`--disable-thinking` bound/configure model execution. No model is called during
state restoration, resume with Oracle, evaluation, inspect or replay. L3 WOFOST is explicitly
unsupported. Runtime schema 4 includes physical material/equipment state.

The shared controller terminates with `task_success` when the scenario outcome
predicate is satisfied, without waiting for an additional model text response.
This trusted check does not expose audit state to the model.
