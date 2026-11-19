"""One OpenAI-compatible chat tool-call adapter; optional SDK, bounded calls."""

from __future__ import annotations
from typing import Any, Protocol, Sequence, TYPE_CHECKING, cast

if TYPE_CHECKING:
    from fairy.agents.loop.context.public import PublicContext
from fairy.acps.ontology.contracts import JsonObject
import json
import os
from fairy.acps.ontology.contracts import OperationRequest
from fairy.agents.models.decision import Decision
from fairy.events.records import canonical


class ChatFunction(Protocol):
    name: str
    arguments: str


class ChatToolCall(Protocol):
    function: ChatFunction


class ChatMessage(Protocol):
    content: str | None
    tool_calls: Sequence[ChatToolCall] | None


class ChatChoice(Protocol):
    finish_reason: str | None
    message: ChatMessage


class ChatUsage(Protocol):
    prompt_tokens: int
    completion_tokens: int


class ChatResponse(Protocol):
    model: str
    usage: ChatUsage | None
    choices: Sequence[ChatChoice]


class ChatCompletions(Protocol):
    def create(self, **kwargs: Any) -> ChatResponse: ...


class ChatEndpoint(Protocol):
    completions: ChatCompletions


class ChatClient(Protocol):
    """兼容SDK与离线模拟客户端的最小边界；运行时仍按需导入SDK。"""

    chat: ChatEndpoint


class LLMAdapter:
    """OpenAI兼容工具调用适配器。

    只接收PublicContext并返回Decision；记录请求身份与用量，不执行工具、
    不把服务异常正文或凭证送入公共错误反馈。"""

    client: ChatClient  #: 仅在decide阶段调用；可替换为离线模拟客户端。
    metadata: JsonObject  #: 脱敏模型身份、用量和请求审计，不是LLM输入。

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        client: ChatClient | None = None,
        *,
        max_output_tokens: int = 512,
        thinking: bool | None = None,
        timeout: float = 30.0,
        temperature: float = 0.1,
    ) -> None:
        """保存显式依赖与初态；模型服务调用发生在决策阶段而非构造阶段。"""
        if type(max_output_tokens) is not int or max_output_tokens < 1:
            raise ValueError("Output token budget must be positive")
        from fairy.acps.ontology.farm.spec import finite

        finite(timeout, "timeout", 0.001)
        finite(temperature, "temperature", 0, 2)
        if thinking is not None and type(thinking) is not bool:
            raise ValueError("thinking must be a boolean or None")
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.thinking = thinking
        self.model = model or os.getenv("FAIRY_LLM_MODEL")
        if not self.model:
            raise ValueError("Set FAIRY_LLM_MODEL or pass --model")
        if client is None:
            from openai import OpenAI

            key = api_key or os.getenv("FAIRY_LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
            if not key:
                raise ValueError("Set FAIRY_LLM_API_KEY (or OPENAI_API_KEY)")
            client = cast(
                ChatClient,
                OpenAI(
                    api_key=key,
                    base_url=base_url or os.getenv("FAIRY_LLM_BASE_URL"),
                    timeout=timeout,
                    max_retries=0,
                ),
            )
        self.client = client
        from urllib.parse import urlsplit, urlunsplit

        endpoint = str(
            getattr(client, "base_url", base_url or os.getenv("FAIRY_LLM_BASE_URL") or "")
        )
        parts = urlsplit(endpoint)
        # Keep credentials and query parameters out of run artifacts.
        endpoint = urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))
        self.metadata: JsonObject = dict(
            kind="llm",
            privileged=False,
            model=self.model,
            adapter="chat_tools_v6",
            base_url=endpoint or None,
            request_timeout_seconds=timeout,
            max_output_tokens=max_output_tokens,
            thinking=thinking,
            temperature=temperature,
            calls=0,
            input_tokens=0,
            output_tokens=0,
            protocol_errors=0,
            model_calls=[],
        )

    def decide(self, context: PublicContext) -> Decision:
        # Rebuild the bounded conversation from public observations only.
        # Never include raw exceptions, audit data, credentials or restore packages.
        """以有限公开历史构造工具调用请求，验证服务响应并返回Decision；绝不执行文本伪工具。"""
        options = {}
        if self.thinking is not None:
            options["extra_body"] = {"chat_template_kwargs": {"enable_thinking": self.thinking}}
        messages: list[JsonObject] = [
            {
                "role": "system",
                "content": "Complete the declared task using the available tools and public results. "
                "Use one tool per response. A text-only response TERMINATES this run immediately. "
                "Do not use text-only replies for progress updates, plans, or announcements of a next action. "
                "Continue using tools until the entire declared task is complete, then finish with text. "
                "This is a simulation: read-only calls do not advance time or run processes in the background. "
                "The tool messages contain actual results; do not invent results.",
            },
            {
                "role": "user",
                "content": canonical(
                    {k: v for k, v in context.items() if k not in ("tools", "history")}
                ),
            },
        ]
        # Reconstruct only the bounded public request/result pairs, not chain of
        # thought. Valid role/tool_call_id linkage matters to tool-trained models.
        for index, turn in enumerate(context.get("history", ())):
            call_id = f"history_{index}"
            request = turn["request"]
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": request["tool"],
                                "arguments": canonical(request["arguments"]),
                            },
                        }
                    ],
                }
            )
            messages.append(
                {"role": "tool", "tool_call_id": call_id, "content": canonical(turn["result"])}
            )
        payload = dict(
            model=self.model,
            messages=messages,
            tools=context["tools"],
            temperature=self.temperature,
            parallel_tool_calls=False,
            max_completion_tokens=self.max_output_tokens,
            **options,
        )
        from fairy.events.records import fingerprint

        encoded = canonical(payload)
        self.metadata["calls"] += 1
        audit = {
            "call": self.metadata["calls"],
            "request_fingerprint": fingerprint(payload),
            "request_json_chars": len(encoded),
            "request_json_utf8_bytes": len(encoded.encode()),
            "measurement": "canonical payload before SDK transport; not provider token count",
            "finish_reason": None,
            "raw_call_count": 0,
            "effective_call_count": 0,
            "context_policy": context.get("context_policy", {}),
            "status": "requesting",
        }
        self.metadata["model_calls"].append(audit)
        try:
            response = self.client.chat.completions.create(**payload)
        except KeyboardInterrupt:
            audit["status"] = "interrupted"
            raise
        except Exception as exc:
            audit.update(status="service_error", error_class=type(exc).__name__)
            raise
        usage = getattr(response, "usage", None)
        audit["served_model"] = str(getattr(response, "model", "unknown"))[:256]
        audit["usage"] = {
            "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
        }
        self.metadata["input_tokens"] += audit["usage"]["input_tokens"]
        self.metadata["output_tokens"] += audit["usage"]["output_tokens"]
        if not response.choices:
            return self._invalid("EMPTY_CHOICES", audit)
        choice = response.choices[0]
        audit["finish_reason"] = str(choice.finish_reason)[:64]
        message = choice.message
        calls = message.tool_calls or []
        audit.update(raw_call_count=len(calls), effective_call_count=0)
        # Retain a fingerprint, never arbitrary raw response text in public feedback.
        audit["response_fingerprint"] = fingerprint(
            {
                "finish_reason": audit["finish_reason"],
                "content": message.content,
                "calls": [
                    {"name": c.function.name, "arguments": c.function.arguments} for c in calls
                ],
            }
        )
        if not calls:
            if choice.finish_reason != "stop":
                return self._invalid("INCOMPLETE_RESPONSE", audit)
            if not message.content or not str(message.content).strip():
                return self._invalid("EMPTY_RESPONSE", audit)
            # A malformed tool-channel serialization is not an intentional final answer.
            # Never parse or execute its contents; use the bounded protocol-feedback path.
            import re

            if re.match(
                r"\s*(?:```[^\n]*\n\s*)?<(?:tool_call|function(?:=|\s|>))",
                str(message.content),
                re.I,
            ):
                return self._invalid("UNSTRUCTURED_TOOL_CALL", audit)
            self.metadata["final_text"] = str(message.content)[:4000]
            audit["status"] = "model_finished"
            return Decision(finish_reason="model_finished")
        if choice.finish_reason != "tool_calls":
            return self._invalid("INCOMPLETE_TOOL_RESPONSE", audit)
        if len(calls) != 1:
            return self._invalid("MULTIPLE_TOOL_CALLS", audit)
        try:
            raw = calls[0].function.arguments
            if not isinstance(raw, str) or len(raw) > 4096:
                return self._invalid("ARGUMENT_SIZE_OR_TYPE", audit)
            arguments = json.loads(
                raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
            )
            if not isinstance(arguments, dict):
                return self._invalid("ARGUMENTS_NOT_OBJECT", audit)
            canonical(arguments)
            audit.update(status="tool_request", effective_call_count=1)
            return Decision(OperationRequest(calls[0].function.name, arguments))
        except (ValueError, TypeError, AttributeError):
            return self._invalid("INVALID_ARGUMENT_JSON", audit)

    def _invalid(self, reason: str, audit: JsonObject) -> Decision:
        """将不完整/非结构化模型输出记录为协议错误，不创建物理事件。"""
        audit.update(status="protocol_error", protocol_error=reason, effective_call_count=0)
        self.metadata["protocol_errors"] += 1
        return Decision(protocol_error=reason)
