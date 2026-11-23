"""Azure OpenAI chat wrapper, kept behind a tiny interface so the agent loop can be tested offline."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..config import Settings


class LLMNotConfigured(RuntimeError):
    """Azure OpenAI settings are missing from the environment."""


class LLMError(RuntimeError):
    """The model call failed. The message never contains credentials."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""


@dataclass
class LLMReply:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    total_tokens: int = 0


class ChatLLM(Protocol):
    model: str

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> LLMReply: ...


class AzureChatLLM:
    def __init__(self, settings: Settings):
        missing = [
            name
            for name, value in (
                ("AZURE_OPEN_AI_KEY", settings.azure_openai_key),
                ("AZURE_OPEN_AI_ENDPOINT", settings.azure_openai_endpoint),
                ("AZURE_OPEN_AI_MODEL_NAME", settings.azure_openai_model),
            )
            if not value.strip()
        ]
        if missing:
            raise LLMNotConfigured("Missing in .env: " + ", ".join(missing))
        from openai import AzureOpenAI

        self.model = settings.azure_openai_model.strip()
        self._client = AzureOpenAI(
            azure_endpoint=settings.azure_openai_endpoint.strip(),
            api_key=settings.azure_openai_key.strip(),
            api_version=settings.azure_openai_version.strip(),
            timeout=settings.chat_timeout_seconds,
            max_retries=1,
        )

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> LLMReply:
        from openai import APIStatusError, OpenAIError

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            # Reasoning models spend part of this budget thinking, so keep it generous.
            "max_completion_tokens": 4000,
        }
        if tools:
            kwargs["tools"] = tools
        try:
            response = self._client.chat.completions.create(**kwargs)
        except APIStatusError as exc:
            if getattr(exc, "code", None) == "content_filter":
                raise LLMError(
                    "The request was blocked by the model's content filter. Please rephrase the question."
                ) from exc
            raise LLMError(f"The language model returned HTTP {exc.status_code}.") from exc
        except OpenAIError as exc:
            raise LLMError(f"The language model call failed ({type(exc).__name__}).") from exc

        message = response.choices[0].message
        calls: list[ToolCall] = []
        for tc in message.tool_calls or []:
            raw = tc.function.arguments or "{}"
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args, raw_arguments=raw))
        used = getattr(response.usage, "total_tokens", 0) or 0
        return LLMReply(content=message.content or "", tool_calls=calls, total_tokens=used)
