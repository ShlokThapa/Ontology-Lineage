"""Azure OpenAI chat wrapper, kept behind a tiny interface so the agent loop can be tested offline."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..config import Settings


class LLMNotConfigured(RuntimeError):
    """Azure OpenAI settings are missing from the environment."""


class LLMError(RuntimeError):
    """The model call failed. The message never contains credentials.

    ``status_code`` and ``retry_after`` (seconds, when the service says) let callers wait out a rate limit.
    """

    def __init__(self, message: str, status_code: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


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
    """One Azure OpenAI deployment. ``slot`` picks model 1 (chat and mapper), 2 (verifier) or 3 (tie-breaker)."""

    def __init__(self, settings: Settings, slot: int = 1):
        suffix = "" if slot == 1 else f"_{slot}"
        cfg = settings.llm_slot(slot)
        missing = [
            name
            for name, value in (
                ("AZURE_OPEN_AI_KEY", cfg["key"]),
                ("AZURE_OPEN_AI_ENDPOINT", cfg["endpoint"]),
                (f"AZURE_OPEN_AI_MODEL_NAME{suffix}", cfg["model"]),
            )
            if not value
        ]
        if missing:
            raise LLMNotConfigured("Missing in .env: " + ", ".join(missing))
        from openai import AzureOpenAI

        self.model = cfg["model"]
        self.slot = slot
        # Two slots pointing at the same endpoint and deployment are the same model for every practical purpose.
        self.identity = f"{cfg['endpoint']}|{cfg['model']}"
        self._client = AzureOpenAI(
            azure_endpoint=cfg["endpoint"],
            api_key=cfg["key"],
            api_version=cfg["version"] or "2024-12-01-preview",
            timeout=settings.chat_timeout_seconds,
            max_retries=1,
        )

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        json_schema: dict[str, Any] | None = None,
    ) -> LLMReply:
        """``json_schema`` asks for a structured reply that must match it (used by the mapping job)."""
        from openai import APIStatusError, OpenAIError

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            # Reasoning models spend part of this budget thinking, so keep it generous.
            "max_completion_tokens": 4000,
        }
        if tools:
            kwargs["tools"] = tools
        if json_schema:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "result", "strict": True, "schema": json_schema},
            }
        try:
            response = self._client.chat.completions.create(**kwargs)
        except APIStatusError as exc:
            if getattr(exc, "code", None) == "content_filter":
                raise LLMError(
                    "The request was blocked by the model's content filter. Please rephrase the question.",
                    status_code=400,
                ) from exc
            retry_after: float | None = None
            try:
                retry_after = float(exc.response.headers.get("retry-after"))
            except (AttributeError, TypeError, ValueError):
                pass
            raise LLMError(
                f"The language model returned HTTP {exc.status_code}.", status_code=exc.status_code, retry_after=retry_after
            ) from exc
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
