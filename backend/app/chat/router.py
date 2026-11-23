"""POST /api/chat: stateless chat endpoint (the browser sends the conversation, the server keeps none)."""

from __future__ import annotations

from typing import Any, Callable, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, model_validator

from ..config import Settings
from ..neo4j_client import Neo4jClient
from ..repository import Repository
from .agent import run_chat
from .embeddings import QueryEmbedder
from .graph import wants_graph
from .llm import ChatLLM
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
) -> APIRouter:
    router = APIRouter(prefix="/api/chat", dependencies=[Depends(require_user)])

    def tools_for(repo: Repository, allow_graph: bool = False) -> GraphTools:
        return GraphTools(repo, get_client(), embedder, allow_graph)

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
        }

    @router.post("", summary="Ask a question about lineage, PII, pipelines or policies")
    def chat(body: ChatRequest, repo: Repository = Depends(get_repo)) -> dict[str, Any]:
        llm = get_llm()  # raises LLMNotConfigured -> 503
        history = [{"role": m.role, "content": m.content} for m in body.messages]
        # A graph is drawn only when the latest message directly asks for one: show_graph is not even
        # offered to the model otherwise.
        tools = tools_for(repo, allow_graph=wants_graph(history[-1]["content"]))
        result = run_chat(history, llm, tools, settings.chat_max_tool_rounds)
        return {
            "answer": result.answer,
            "graph": result.graph,
            "citations": result.citations,
            "steps": [
                {"tool": s.tool, "arguments": s.arguments, "summary": s.summary, "error": s.error} for s in result.steps
            ],
            "tokens": result.tokens,
            "model": llm.model,
        }

    return router
