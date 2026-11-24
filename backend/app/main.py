"""Lineage Studio API."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from neo4j.exceptions import Neo4jError
from pydantic import BaseModel, Field, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import cypher, lineage
from .config import Settings, get_settings
from .cypher import cypher_literal, inline_params  # noqa: F401 - re-exported: tests import them from here
from .graphmodel import GNode, SubGraph, node_view, to_neo4j_format, to_view
from .guard import ReadOnlyViolation, ensure_read_only
from .neo4j_client import DatabaseUnavailable, Neo4jClient
from .repository import LiveRepository, Repository
from .chat.embeddings import LocalEmbedder, QueryEmbedder
from .chat.lakehouse import LakehouseData
from .chat.llm import AzureChatLLM, ChatLLM, LLMError, LLMNotConfigured
from .chat.router import create_chat_router
from .jobs.fabric import FabricError, FabricNotConfigured
from .jobs.router import JobsDeps, create_jobs_router
from .explore.phrases import PhraseStore
from .explore.router import create_explore_router
from .snapshot import SnapshotRepository

log = logging.getLogger("lineage_studio")


# ---------------------------------------------------------------------------
# Errors: always {"error": {"code", "message"}}; never leak stack traces.
# ---------------------------------------------------------------------------


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return _error(exc.status, exc.code, exc.message)

    @app.exception_handler(ReadOnlyViolation)
    async def _read_only(_: Request, exc: ReadOnlyViolation) -> JSONResponse:
        return _error(400, "LineageStudio.ReadOnly", str(exc))

    @app.exception_handler(DatabaseUnavailable)
    async def _unavailable(_: Request, exc: DatabaseUnavailable) -> JSONResponse:
        log.warning("Database unavailable: %s", exc)
        return _error(503, "LineageStudio.DatabaseUnavailable", "The Neo4j database is not reachable. Try Reconnect.")

    @app.exception_handler(LLMNotConfigured)
    async def _llm_not_configured(_: Request, exc: LLMNotConfigured) -> JSONResponse:
        return _error(503, "LineageStudio.ChatNotConfigured", f"The chatbot is not configured. {exc}")

    @app.exception_handler(LLMError)
    async def _llm_error(_: Request, exc: LLMError) -> JSONResponse:
        log.warning("Language model call failed: %s", exc)
        return _error(502, "LineageStudio.LlmError", str(exc))

    @app.exception_handler(FabricNotConfigured)
    async def _fabric_not_configured(_: Request, exc: FabricNotConfigured) -> JSONResponse:
        return _error(503, "LineageStudio.FabricNotConfigured", str(exc))

    @app.exception_handler(FabricError)
    async def _fabric_error(_: Request, exc: FabricError) -> JSONResponse:
        log.warning("Fabric call failed: %s", exc)
        return _error(502, "LineageStudio.FabricError", str(exc))  # the message never contains a token or secret

    @app.exception_handler(Neo4jError)
    async def _neo4j(_: Request, exc: Neo4jError) -> JSONResponse:
        code = exc.code or "Neo.DatabaseError"
        status = 408 if "TransactionTimedOut" in code or "Timeout" in code else 400
        if code.startswith("Neo.ClientError.Statement.AccessMode"):
            message = "This app is read-only: the database rejected a write operation."
        else:
            message = exc.message or "Query failed."
        return _error(status, code, message)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        parts = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err.get("loc", []) if p not in ("body", "query"))
            parts.append(f"{loc}: {err.get('msg')}" if loc else str(err.get("msg")))
        return _error(422, "LineageStudio.InvalidInput", "; ".join(parts) or "Invalid input")

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _error(exc.status_code, "LineageStudio.Http", str(exc.detail))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("Unhandled error")
        return _error(500, "LineageStudio.InternalError", "Internal server error.")


# ---------------------------------------------------------------------------
# Auth hook
# ---------------------------------------------------------------------------


def require_user(request: Request) -> None:
    """Authentication hook.

    Microsoft Entra ID login is out of scope. To add it, validate the bearer
    token here (e.g. with `fastapi-azure-auth` or by verifying the JWT against
    the tenant's JWKS) and raise HTTPException(401) when it is missing/invalid.
    Every /api route depends on this function.
    """
    return None


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class QueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=100_000)
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("query")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("query must not be empty")
        return v

    @field_validator("params")
    @classmethod
    def _small_params(cls, v: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(v, default=str)) > 1_000_000:
            raise ValueError("params are too large")
        return v


DirectionParam = Literal["upstream", "downstream", "both"]
LevelParam = Literal["column", "table"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def add_policies(repo: "Repository", sub: SubGraph, view_sub: SubGraph, start: GNode, level: str) -> int:
    """Overlay the policy documents governing the assets of a lineage view.

    Table level: policies on every table / view shown (column governance rolls up to the table).
    Column level: policies on the start node, its own columns and the tables shown, so the
    graph stays readable. Starting from a policy document shows only that document.
    """
    owners = sub.owner_map()
    if level == "table" or start.label == "PolicyDocument":
        ids = {n.id for n in sub.nodes.values() if n.label in ("Table", "View", "Column")}
    else:
        seeds = lineage.seed_columns(sub, start)
        ids = {start.id, *seeds} | {owners[s] for s in seeds if s in owners}
        ids |= {n.id for n in view_sub.nodes.values() if n.label in ("Table", "View")}
        ids |= {c for c, tbl in owners.items() if tbl in view_sub.nodes}
    try:
        policies = repo.governing_policies(sorted(ids))
    except DatabaseUnavailable:
        raise
    except Exception:  # noqa: BLE001 - policies are an optional overlay; never fail the lineage
        log.exception("Could not load governing policies")
        return 0
    only = start.id if start.label == "PolicyDocument" else None
    return lineage.attach_policies(view_sub, policies, owners, only_doc=only)


def search_row(n: GNode) -> dict[str, Any]:
    v = node_view(n)
    return {
        "id": n.id,
        "label": n.label,
        "name": n.name,
        "caption": v["caption"],
        "subtitle": v["subtitle"],
        "platform": n.props.get("platform"),
    }


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    settings: Settings | None = None,
    repo: Repository | None = None,
    client: Neo4jClient | None = None,
    phrase_store: PhraseStore | None = None,
    llm: ChatLLM | None = None,
    embedder: QueryEmbedder | None = None,
    jobs: JobsDeps | None = None,
    lakehouse: LakehouseData | None = None,
) -> FastAPI:
    """Build the app. `repo` / `client` / `llm` / `embedder` / `jobs` / `lakehouse` can be injected (tests)."""
    settings = settings or get_settings()
    if client is not None and repo is None:
        repo = LiveRepository(client)
    state: dict[str, Any] = {"repo": repo, "client": client, "llm": llm}
    if embedder is None:
        embedder = LocalEmbedder(settings.embedding_model, settings.model_cache_dir)

    @asynccontextmanager
    async def lifespan(_: FastAPI):  # type: ignore[no-untyped-def]
        if state["repo"] is None:
            if settings.mode == "snapshot":
                state["repo"] = SnapshotRepository.from_dir(settings.snapshot_dir)
                log.info("Snapshot mode: loaded %s", settings.snapshot_dir)
            else:
                client = Neo4jClient(settings)
                state["client"] = client
                state["repo"] = LiveRepository(client)
        try:
            yield
        finally:
            if state["client"] is not None:
                state["client"].close()

    app = FastAPI(
        title="Lineage Studio API",
        version="1.0.0",
        description="Read-only exploration of the data-lineage ontology stored in Neo4j.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Authorization"],
    )
    _install_error_handlers(app)

    def get_repo() -> Repository:
        if state["repo"] is None:  # pragma: no cover - only before startup
            raise ApiError(503, "LineageStudio.NotReady", "Server is starting up.")
        return state["repo"]

    def live_client() -> Neo4jClient:
        client = state["client"]
        if settings.mode == "snapshot" or client is None:
            raise ApiError(
                503, "LineageStudio.SnapshotMode", "Live Cypher is unavailable in snapshot mode."
            )
        return client

    def resolve(repo: Repository, ref: str) -> GNode:
        node = repo.resolve(ref.strip())
        if node is None:
            raise ApiError(404, "LineageStudio.NotFound", f"No asset found for '{ref}'.")
        return node

    def chat_llm() -> ChatLLM:
        if state["llm"] is None:  # built on first use so a missing key never blocks startup
            state["llm"] = AzureChatLLM(settings)
        return state["llm"]

    def graph_client() -> Neo4jClient | None:
        return None if settings.mode == "snapshot" else state["client"]

    api = APIRouter(prefix="/api", dependencies=[Depends(require_user)])

    # -- status ----------------------------------------------------------------

    @api.get("/health", summary="Connection status")
    def health() -> dict[str, Any]:
        base = {
            "mode": settings.mode,
            "max_rows": settings.max_rows,
            "query_timeout_seconds": settings.query_timeout_seconds,
        }
        if settings.mode == "snapshot":
            return {
                **base,
                "connected": True,
                "host": "snapshot",
                "database": "snapshot",
                "user": None,
                "server_version": None,
                "server_edition": None,
                "error": None,
            }
        client = state["client"]
        return {**base, **(client.health() if client else {"connected": False, "error": "Not started"})}

    @api.post("/reconnect", summary="Recreate the Neo4j driver and re-check connectivity")
    def reconnect() -> dict[str, Any]:
        if settings.mode == "snapshot":
            return health()
        return {
            "mode": settings.mode,
            "max_rows": settings.max_rows,
            "query_timeout_seconds": settings.query_timeout_seconds,
            **live_client().reconnect(),
        }

    @api.get("/schema", summary="Labels, relationship types and property keys with counts")
    def schema(repo: Repository = Depends(get_repo)) -> dict[str, Any]:
        return repo.schema_info()

    @api.get("/schema/graph", summary="Schema (meta-graph) as nodes/edges")
    def schema_graph(repo: Repository = Depends(get_repo)) -> dict[str, Any]:
        sub = repo.schema_graph()
        sub.nodes.pop("Asset", None)
        return {**to_view(sub, reverse_reads=False), "cypher": cypher.SCHEMA_VISUALIZATION}

    # -- query -----------------------------------------------------------------

    @api.post("/query", summary="Run a read-only Cypher query")
    def run_query(body: QueryRequest) -> dict[str, Any]:
        ensure_read_only(body.query)  # layer 2: static guard, before anything else
        client = live_client()
        # layer 1: the client runs everything in a READ access-mode transaction
        return client.run_read(body.query, body.params).to_dict()

    @api.get("/neighbors", summary="1-hop neighbourhood of a node")
    def neighbors(
        id: str = Query(min_length=1, max_length=1000),
        format: Literal["graph", "lineage"] = "graph",
        limit: int = Query(200, ge=1, le=2000),
        repo: Repository = Depends(get_repo),
    ) -> dict[str, Any]:
        if format == "graph":
            # `id` is a Neo4j elementId from a query result.
            if isinstance(repo, LiveRepository):
                return repo.element_neighbors(id, limit)
            return to_neo4j_format(repo.asset_neighbors(id, limit))
        node = resolve(repo, id)
        sub = repo.asset_neighbors(node.id, limit)
        return {
            **to_view(sub, start_ids={node.id}),
            "cypher": inline_params(cypher.NEIGHBORS_BY_ASSET_ID, {"id": node.id}).replace("$limit", str(limit)),
        }

    # -- lineage pages ---------------------------------------------------------

    @api.get("/search", summary="Search tables, views, columns, pipelines and concepts")
    def search(
        q: str = Query(min_length=1, max_length=200),
        limit: int = Query(25, ge=1, le=100),
        repo: Repository = Depends(get_repo),
    ) -> dict[str, Any]:
        rows = [search_row(n) for n in repo.search(q.strip(), limit)]
        return {"rows": rows, "nodes": [], "edges": [], "cypher": cypher.SEARCH}

    @api.get("/lineage", summary="Upstream / downstream lineage")
    def get_lineage(
        id: str = Query(min_length=1, max_length=1000),
        direction: DirectionParam = "both",
        depth: int = Query(3, ge=1, le=6),
        level: LevelParam = "column",
        policies: bool = Query(True, description="Add the policy documents that govern the assets shown"),
        repo: Repository = Depends(get_repo),
    ) -> dict[str, Any]:
        start = resolve(repo, id)
        sub = repo.lineage(start, direction, depth)
        if level == "table":
            view_sub, start_ids = lineage.table_level(sub, start)
        else:
            view_sub, start_ids = lineage.column_level(sub, start), {start.id}
        policy_count = add_policies(repo, sub, view_sub, start, level) if policies else 0
        view = to_view(view_sub, start_ids=start_ids)
        return {
            **view,
            "start": node_view(start, {start.id}),
            "summary": {
                "nodes": len(view["nodes"]),
                "edges": len(view["edges"]),
                "direction": direction,
                "depth": depth,
                "level": level,
                "policies": policy_count,
            },
            "cypher": inline_params(cypher.lineage(start.label, direction, depth), {"id": start.id}),
        }

    @api.get("/impact", summary="Downstream impact of changing a table or column")
    def get_impact(
        id: str = Query(min_length=1, max_length=1000),
        depth: int = Query(6, ge=1, le=6),
        level: LevelParam = "column",
        policies: bool = Query(True, description="Add the policy documents that govern the assets shown"),
        repo: Repository = Depends(get_repo),
    ) -> dict[str, Any]:
        start = resolve(repo, id)
        sub = repo.lineage(start, "downstream", depth)
        rows, summary = lineage.impact(sub, start)
        if level == "table":
            view_sub, start_ids = lineage.table_level(sub, start)
        else:
            view_sub, start_ids = lineage.column_level(sub, start), {start.id}
        if policies:
            summary = {**summary, "policies": add_policies(repo, sub, view_sub, start, level)}
        return {
            **to_view(view_sub, start_ids=start_ids),
            "start": node_view(start, {start.id}),
            "rows": rows,
            "summary": summary,
            "cypher": inline_params(cypher.impact(start.label, depth), {"id": start.id}),
        }

    @api.get("/pii", summary="PII columns and how they are treated downstream")
    def get_pii(repo: Repository = Depends(get_repo)) -> dict[str, Any]:
        sub = repo.pii()
        rows, summary = lineage.pii(sub)
        return {**to_view(sub), "rows": rows, "summary": summary, "cypher": cypher.PII}

    @api.get("/pipelines", summary="Pipelines, activities and the tables they read / write")
    def get_pipelines(repo: Repository = Depends(get_repo)) -> dict[str, Any]:
        sub = repo.pipelines()
        rows, summary = lineage.pipelines(sub)
        return {**to_view(sub), "rows": rows, "summary": summary, "cypher": cypher.PIPELINES}

    @api.get("/concepts", summary="Business ontology (concepts and their relations)")
    def get_concepts(include_tables: bool = False, repo: Repository = Depends(get_repo)) -> dict[str, Any]:
        sub: SubGraph = repo.concepts(include_tables)
        q = cypher.CONCEPTS_WITH_TABLES if include_tables else cypher.CONCEPTS
        return {**to_view(sub), "summary": {"concepts": sum(1 for n in sub.nodes.values() if n.label == "BusinessConcept")}, "cypher": q}

    app.include_router(api)
    app.include_router(
        create_chat_router(
            settings=settings,
            get_repo=get_repo,
            get_client=graph_client,
            get_llm=chat_llm,
            embedder=embedder,
            require_user=require_user,
            data=lakehouse or LakehouseData(settings),
        )
    )
    app.include_router(
        create_jobs_router(settings=settings, deps=jobs or JobsDeps(), get_neo4j=graph_client, require_user=require_user)
    )
    # Bloom explorer (live mode only: live_client() returns 503 in snapshot mode)
    app.include_router(
        create_explore_router(
            get_client=live_client,
            phrase_store=phrase_store or PhraseStore(settings.phrases_file),
            require_user=require_user,
        )
    )
    _mount_frontend(app, settings)
    return app


def _mount_frontend(app: FastAPI, settings: Settings) -> None:
    """Serve the built React app (single-container deployment)."""
    static_dir = settings.static_dir.resolve()
    index = static_dir / "index.html"
    if not index.is_file():
        log.info("No frontend build at %s; API only.", static_dir)
        return

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str) -> FileResponse:
        if full_path.startswith("api/") or full_path == "api":
            raise HTTPException(404, "Not found")
        candidate = (static_dir / full_path).resolve()
        # Prevent path traversal outside the static directory.
        if full_path and candidate.is_file() and static_dir in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(index)


app = create_app()
