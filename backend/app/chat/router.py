"""POST /api/chat: stateless chat endpoint (the browser sends the conversation, the server keeps none)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, model_validator

from ..config import Settings
from ..neo4j_client import Neo4jClient
from ..repository import Repository
from .agent import run_chat
from .answer_cache import AnswerCache
from .embeddings import QueryEmbedder
from .graph import wants_graph
from .lakehouse import LakehouseData
from .llm import ChatLLM
from .quality import wants_quality
from .suggested_questions import QUESTIONS
from .tools import GraphTools


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def _ends_with_user(self) -> "ChatRequest":
        if self.messages[-1].role != "user":
            raise ValueError("the last message must be from the user")
        return self


def create_chat_router(
    *,
    settings: Settings,
    get_repo: Callable[[], Repository],
    get_client: Callable[[], Neo4jClient | None],
    get_llm: Callable[[], ChatLLM],
    embedder: QueryEmbedder | None,
    require_user: Callable[..., None],
    data: LakehouseData | None = None,
    cache: AnswerCache | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/chat", dependencies=[Depends(require_user)])
    cache = cache or AnswerCache(Path(settings.chat_cache_file) if settings.chat_cache_file else None, settings.chat_use_cache)

    def tools_for(repo: Repository, allow_graph: bool = False, allow_quality: bool = False) -> GraphTools:
        return GraphTools(repo, get_client(), embedder, allow_graph, data, allow_quality)

    @router.get("/status", summary="Is the chatbot set up?")
    def status(repo: Repository = Depends(get_repo)) -> dict[str, Any]:
        configured = all(
            v.strip()
            for v in (settings.azure_openai_key, settings.azure_openai_endpoint, settings.azure_openai_model)
        )
        tools = tools_for(repo)
        return {
            "configured": configured,
            "model": settings.azure_openai_model or None,
            "policy_search": tools.vector_search_ready,
            "mode": settings.mode,
            "suggestions": [{**q, "saved": cache.has(q["id"])} for q in QUESTIONS],
        }

    @router.post("", summary="Ask a question about lineage, PII, pipelines or policies")
    def chat(body: ChatRequest, repo: Repository = Depends(get_repo)) -> dict[str, Any]:
        history = [{"role": m.role, "content": m.content} for m in body.messages]
        # A suggested question that opens a conversation is answered from the saved copy.
        if len(history) == 1 and (saved := cache.get(history[0]["content"])) is not None:
            return saved
        llm = get_llm()  # raises LLMNotConfigured -> 503
        # A graph is drawn only when the latest message directly asks for one, and the data quality scorecard is
        # built only when it is about data quality: those tools are not even offered to the model otherwise.
        latest = history[-1]["content"]
        tools = tools_for(repo, allow_graph=wants_graph(latest), allow_quality=wants_quality(latest))
        result = run_chat(history, llm, tools, settings.chat_max_tool_rounds)
        return {
            "answer": result.answer,
            "graph": result.graph,
            "dashboard": result.dashboard,
            "citations": result.citations,
            "steps": [
                {"tool": s.tool, "arguments": s.arguments, "summary": s.summary, "error": s.error} for s in result.steps
            ],
            "tokens": result.tokens,
            "model": llm.model,
        }

    return router
