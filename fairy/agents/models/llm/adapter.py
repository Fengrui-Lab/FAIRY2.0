"""One OpenAI-compatible chat tool-call adapter; optional SDK, bounded calls."""
from __future__ import annotations
import json
import os
from fairy.acps.ontology.contracts import OperationRequest
from fairy.agents.models.decision import Decision
from fairy.events.records import canonical


class LLMAdapter:
    def __init__(self, model=None, base_url=None, api_key=None, client=None, *, max_output_tokens=512, thinking=None, timeout=30.0, temperature=0.1):
        if type(max_output_tokens) is not int or max_output_tokens<1:
            raise ValueError("Output token budget must be positive")
        from fairy.acps.ontology.farm.spec import finite
        finite(timeout, "timeout", 0.001)
        finite(temperature, "temperature", 0, 2)
        if thinking is not None and type(thinking) is not bool:
            raise ValueError("thinking must be a boolean or None")
        self.temperature=temperature
        self.max_output_tokens=max_output_tokens
        self.thinking=thinking
        self.model = model or os.getenv("FAIRY_LLM_MODEL")
        if not self.model:
            raise ValueError("Set FAIRY_LLM_MODEL or pass --model")
        if client is None:
            from openai import OpenAI
            key = api_key or os.getenv("FAIRY_LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
            if not key:
                raise ValueError("Set FAIRY_LLM_API_KEY (or OPENAI_API_KEY)")
            client = OpenAI(api_key=key, base_url=base_url or os.getenv("FAIRY_LLM_BASE_URL"),
                            timeout=timeout, max_retries=0)
        self.client = client
        from urllib.parse import urlsplit, urlunsplit
        endpoint = str(getattr(client, "base_url", base_url or os.getenv("FAIRY_LLM_BASE_URL") or ""))
        parts = urlsplit(endpoint)
        # Keep credentials and query parameters out of run artifacts.
        endpoint = urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))
        self.metadata = dict(kind="llm", privileged=False, model=self.model,
                             adapter="chat_tools_v4", base_url=endpoint or None, request_timeout_seconds=timeout, max_output_tokens=max_output_tokens, thinking=thinking, temperature=temperature, calls=0, input_tokens=0, output_tokens=0)

    def decide(self, context):
        # Rebuild the bounded conversation from public observations only.
        # Never include raw exceptions, audit data, credentials or restore packages.
        options={}
        if self.thinking is not None:
            options["extra_body"]={"chat_template_kwargs":{"enable_thinking":self.thinking}}
        messages=[{"role":"system","content":
            "Complete the declared task using the available tools and public results. "
            "Use one tool per response. A text-only response TERMINATES this run immediately. "
            "Do not use text-only replies for progress updates, plans, or announcements of a next action. "
            "Continue using tools until the entire declared task is complete, then finish with text. "
            "This is a simulation: read-only calls do not advance time or run processes in the background. "
            "The tool messages contain actual results; do not invent results."},
            {"role":"user","content":canonical({k:v for k,v in context.items() if k not in ("tools","history")})}]
        # Reconstruct only the bounded public request/result pairs, not chain of
        # thought. Valid role/tool_call_id linkage matters to tool-trained models.
        for index,turn in enumerate(context.get("history",())):
            call_id=f"history_{index}"
            request=turn["request"]
            messages.append({"role":"assistant","content":None,"tool_calls":[{
                "id":call_id,"type":"function","function":{
                    "name":request["tool"],"arguments":canonical(request["arguments"])}}]})
            messages.append({"role":"tool","tool_call_id":call_id,"content":canonical(turn["result"])})
        response = self.client.chat.completions.create(
            model=self.model,messages=messages,
            tools=context["tools"], temperature=self.temperature, parallel_tool_calls=False,
            max_completion_tokens=self.max_output_tokens, **options,
        )
        self.metadata["calls"] += 1
        usage = getattr(response, "usage", None)
        if usage:
            self.metadata["input_tokens"] += getattr(usage, "prompt_tokens", 0) or 0
            self.metadata["output_tokens"] += getattr(usage, "completion_tokens", 0) or 0
        if not response.choices:
            return self._invalid()
        choice = response.choices[0]
        message = choice.message
        calls = message.tool_calls or []
        if not calls:
            if choice.finish_reason != "stop" or not message.content:
                return self._invalid()
            self.metadata["final_text"] = str(message.content)[:4000]
            return Decision(finish_reason="model_finished")
        if choice.finish_reason != "tool_calls":
            return self._invalid()
        if len(calls) != 1:
            return self._invalid()
        try:
            raw = calls[0].function.arguments
            if len(raw) > 4096:
                return self._invalid()
            arguments = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(arguments, dict):
                return self._invalid()
            canonical(arguments)  # rejects overflow such as 1e999 -> infinity
            return Decision(OperationRequest(calls[0].function.name, arguments))
        except (ValueError, TypeError, AttributeError):
            return self._invalid()

    @staticmethod
    def _invalid():
        # A normal rejected request gives the model feedback and is replayable.
        return Decision(OperationRequest("invalid_model_response"))
