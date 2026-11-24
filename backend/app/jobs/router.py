"""/api/jobs: the endpoints behind the Jobs page.

Fabric triggers start a notebook or pipeline (or a chain of them) and report on it. The mapping endpoints run the
policy-mapping job here. Everything that can change Neo4j (a mapping run with ``write``, accepting / rejecting
links, undoing a run) checks ``MAPPING_UI_WRITE`` first, so those buttons can be switched off in ``.env``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..chat.llm import LLMNotConfigured
from ..config import Settings
from ..mapping import service
from ..mapping.models import load_models
from ..mapping.store import MappingWriter
from ..neo4j_client import DatabaseUnavailable, Neo4jClient
from .fabric import FabricClient, FabricNotConfigured
from .registry import CHAINS, CHAINS_BY_KEY, GROUP_NOTES, GROUPS, JOBS, JOBS_BY_KEY, Job, coerce_params
from .runs import ItemResolver, JobStore, execute_chain, new_chain_run

RUN_ID = r"^[0-9A-Za-z-]{6,64}$"


class FabricHub:
    """The Fabric client and item resolver, built the first time a Fabric button is used."""

    def __init__(self, settings: Settings, client: FabricClient | None = None):
        self.settings, self._client, self._resolver = settings, client, None

    @property
    def client(self) -> FabricClient:
        if self._client is None:
            self._client = FabricClient(self.settings)
        return self._client

    @property
    def resolver(self) -> ItemResolver:
        if self._resolver is None:
            self._resolver = ItemResolver(self.client)
        return self._resolver


@dataclass
class JobsDeps:
    hub: FabricHub | None = None
    store: JobStore = field(default_factory=JobStore)
    mapping: Any = service  # check / execute / recent_runs / links_of_run (replaced in tests)
    writer_factory: Callable[[Settings], MappingWriter] = MappingWriter
    models_factory: Callable[[Settings], Any] = load_models
    sleep: Callable[[float], None] | None = None


class FabricRunBody(BaseModel):
    job: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    confirm: bool = False


class ChainBody(BaseModel):
    chain: str
    overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)  # job key -> parameters for that step
    confirm: bool = False


class MappingRunBody(BaseModel):
    mode: Literal["append", "overwrite"] = "append"
    write: bool = False
    confirm_overwrite: bool = False
    only_unlinked: bool = False
    no_auto_accept: bool = False
    accept_self_verified: bool = False
    docs: list[str] = Field(default_factory=list, max_length=20)
    limit: int | None = Field(default=None, ge=1, le=10_000)
    workers: int = Field(default=4, ge=1, le=16)
    use_cache: bool = True


class Decision(BaseModel):
    chunk_id: str = Field(min_length=1, max_length=300)
    asset_id: str = Field(min_length=1, max_length=500)


class DecideBody(BaseModel):
    run_id: str = Field(pattern=RUN_ID)
    status: Literal["accepted", "rejected"]
    decisions: list[Decision] = Field(min_length=1, max_length=500)


class ApplyBody(BaseModel):
    """Which links of a saved dry run to write: `accept` as accepted, `reject` as rejected (remembered)."""

    run_id: str = Field(pattern=RUN_ID)
    accept: list[Decision] = Field(default_factory=list, max_length=2000)
    reject: list[Decision] = Field(default_factory=list, max_length=2000)


class ReviewBody(BaseModel):
    run_id: str = Field(pattern=RUN_ID)
    status: Literal["accepted", "rejected"]
    min_confidence: Literal["high", "medium"] = "medium"


class RestoreBody(BaseModel):
    run_id: str = Field(pattern=RUN_ID)


def _job_info(j: Job) -> dict[str, Any]:
    return {
        "key": j.key, "title": j.title, "description": j.description, "item_type": j.item_type, "group": j.group,
        "confirm": j.confirm, "legacy": j.legacy, "names": list(j.names),
        "params": [{"name": p.name, "label": p.label, "type": p.type, "default": p.default, "options": list(p.options)} for p in j.params],
    }


def create_jobs_router(
    *,
    settings: Settings,
    deps: JobsDeps,
    get_neo4j: Callable[[], Neo4jClient | None],
    require_user: Callable[..., None],
) -> APIRouter:
    router = APIRouter(prefix="/api/jobs", dependencies=[Depends(require_user)])
    store = deps.store
    hub = deps.hub or FabricHub(settings)

    # -- helpers ----------------------------------------------------------------------------------

    def live() -> Neo4jClient:
        client = get_neo4j()
        if client is None:
            raise HTTPException(503, "The mapping job needs the live Neo4j database (snapshot mode has none).")
        return client

    def require_write() -> None:
        if not settings.mapping_ui_write:
            raise HTTPException(403, "Writing from the app is switched off (MAPPING_UI_WRITE=false in .env).")

    def ensure_fabric_ready() -> None:
        client = hub.client
        if not (settings.fabric_workspace_id.strip() or settings.fabric_workspace_name.strip()):
            raise FabricNotConfigured("Set FABRIC_WORKSPACE_ID or FABRIC_WORKSPACE_NAME in .env.")
        if client.tokens.mode == "none":
            client.tokens.token()  # raises FabricNotConfigured with the instructions

    def check_not_busy(keys: list[str]) -> None:
        busy = store.busy_keys() & set(keys)
        if busy:
            raise HTTPException(409, f"Already running: {', '.join(sorted(busy))}. Wait for it to finish.")

    def start_chain(kind: str, key: str, title: str, jobs: list[tuple[Job, dict[str, Any]]]) -> dict[str, Any]:
        ensure_fabric_ready()
        check_not_busy([j.key for j, _ in jobs] + [key])
        run = new_chain_run(store, kind, key, title, jobs)
        kw: dict[str, Any] = {"sleep": deps.sleep} if deps.sleep else {}
        store.start(run, lambda: execute_chain(store, run, hub.client, hub.resolver, jobs, settings, **kw))
        return store.to_dict(run)

    def typed(job: Job, params: dict[str, Any]) -> dict[str, Any]:
        try:
            out = coerce_params(job, params)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if out.get("WRITE"):
            require_write()
            if out.get("MODE") == "overwrite" and not out.get("CONFIRM_OVERWRITE"):
                raise HTTPException(400, "Overwrite replaces live links: tick 'Confirm overwrite' to write in overwrite mode.")
        return out

    # -- overview ---------------------------------------------------------------------------------

    @router.get("/config", summary="What the Jobs page can do, and what is switched on")
    def config() -> dict[str, Any]:
        client = hub.client
        return {
            "groups": GROUPS,
            "group_notes": GROUP_NOTES,
            "jobs": [_job_info(j) for j in JOBS],
            "chains": [{"key": c.key, "title": c.title, "description": c.description, "group": c.group, "steps": list(c.steps)} for c in CHAINS],
            "fabric": {
                "configured": client.configured, "auth_mode": client.tokens.mode,
                "workspace": settings.fabric_workspace_id or settings.fabric_workspace_name or None,
                "param_style": settings.fabric_param_style,
            },
            "mapping": {"write_enabled": settings.mapping_ui_write, "live": get_neo4j() is not None},
        }

    @router.get("/fabric/status", summary="Is Fabric configured, and which jobs exist in the workspace?")
    def fabric_status(refresh: bool = False) -> dict[str, Any]:
        client = hub.client
        info: dict[str, Any] = {
            "configured": client.configured, "auth_mode": client.tokens.mode, "token_expires_in": client.tokens.expires_in(),
            "workspace": settings.fabric_workspace_id or settings.fabric_workspace_name or None, "items": {}, "error": None, "problems": [],
        }
        if client.tokens.mode == "none":
            info["problems"].append("No way to sign in: set FABRIC_TENANT_ID, FABRIC_CLIENT_ID and FABRIC_CLIENT_SECRET (a service principal), or FABRIC_ACCESS_TOKEN.")
        if not (settings.fabric_workspace_id.strip() or settings.fabric_workspace_name.strip()):
            info["problems"].append("No workspace: set FABRIC_WORKSPACE_ID or FABRIC_WORKSPACE_NAME.")
        if client.configured:
            try:
                info["items"] = hub.resolver.overview(JOBS, refresh)
            except Exception as exc:  # noqa: BLE001 - shown on the page instead of failing it
                info["error"] = str(exc) if hasattr(exc, "status_code") else f"Could not list the workspace items ({type(exc).__name__})."
        return info

    # -- Fabric triggers --------------------------------------------------------------------------

    @router.post("/fabric/run", summary="Start one notebook or pipeline")
    def run_job(body: FabricRunBody) -> dict[str, Any]:
        job = JOBS_BY_KEY.get(body.job)
        if job is None:
            raise HTTPException(404, f"No job '{body.job}'.")
        if job.confirm and not body.confirm:
            raise HTTPException(400, f"Confirmation required. {job.confirm}")
        return start_chain("fabric", job.key, job.title, [(job, typed(job, body.parameters))])

    @router.post("/fabric/chain", summary="Start several jobs one after another")
    def run_chain(body: ChainBody) -> dict[str, Any]:
        chain = CHAINS_BY_KEY.get(body.chain)
        if chain is None:
            raise HTTPException(404, f"No chain '{body.chain}'.")
        unknown = set(body.overrides) - set(chain.steps)
        if unknown:
            raise HTTPException(400, f"Overrides for jobs not in this chain: {', '.join(sorted(unknown))}.")
        jobs = [(JOBS_BY_KEY[k], typed(JOBS_BY_KEY[k], body.overrides.get(k, {}))) for k in chain.steps]
        warnings = [j.confirm for j, _ in jobs if j.confirm]
        if warnings and not body.confirm:
            raise HTTPException(400, "Confirmation required. " + " ".join(dict.fromkeys(warnings)))
        return start_chain("chain", chain.key, chain.title, jobs)

    # -- runs -------------------------------------------------------------------------------------

    @router.get("/runs", summary="Recent runs started from this page")
    def runs() -> list[dict[str, Any]]:
        return [store.to_dict(r) for r in store.recent()]

    @router.get("/runs/{run_id}", summary="One run, for polling")
    def one_run(run_id: str) -> dict[str, Any]:
        run = store.get(run_id)
        if run is None:
            raise HTTPException(404, "No such run (the list is cleared when the API restarts).")
        return store.to_dict(run)

    @router.post("/runs/{run_id}/cancel", summary="Ask a running Fabric run to stop")
    def cancel(run_id: str) -> dict[str, Any]:
        run = store.get(run_id)
        if run is None:
            raise HTTPException(404, "No such run.")
        if run.kind == "mapping":
            raise HTTPException(400, "A mapping run cannot be cancelled; it finishes on its own (a dry run changes nothing).")
        if run.status not in ("queued", "running"):
            raise HTTPException(409, f"The run already ended ({run.status}).")
        with store.lock:
            run.cancel_requested = True
        return store.to_dict(run)

    # -- the mapping job, run here ------------------------------------------------------------------

    @router.post("/mapping/check", summary="Models, catalogue size, chunks and existing links (no model call)")
    def mapping_check() -> dict[str, Any]:
        return deps.mapping.check(settings, live())

    @router.post("/mapping/run", summary="Run the policy mapping (a dry run unless write is true)")
    def mapping_run(body: MappingRunBody) -> dict[str, Any]:
        opts = service.RunOptions(**body.model_dump())
        try:
            opts.validate()
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if opts.write:
            require_write()
        client = live()
        check_not_busy(["mapping"])
        run = store.new("mapping", "mapping", f"Policy mapping: {opts.mode}, {'write' if opts.write else 'dry run'}")

        def progress(done: int, total: int, res: Any) -> None:
            with store.lock:
                run.progress = {"done": done, "total": total}
                run.log.append(f"[{done}/{total}] {res.chunk.section_path[:70]}: " + (res.error or f"{len([x for x in res.links if x.status != 'dropped'])} links"))

        def work() -> None:
            try:
                out = deps.mapping.execute(settings, opts, client, progress, models=deps.models_factory(settings), writer_factory=deps.writer_factory)
                with store.lock:
                    run.result, run.status = out, "succeeded"
            except (LLMNotConfigured, ValueError, DatabaseUnavailable) as exc:
                with store.lock:
                    run.status, run.error = "failed", str(exc)[:400]

        store.start(run, work)
        return store.to_dict(run)

    @router.get("/mapping/runs", summary="Past mapping runs recorded in Neo4j")
    def mapping_history() -> list[dict[str, Any]]:
        return deps.mapping.recent_runs(live())

    @router.get("/mapping/links", summary="The links one run created")
    def mapping_links(run_id: str, status: str | None = None) -> list[dict[str, Any]]:
        if not _valid_run_id(run_id):
            raise HTTPException(400, "Invalid run id.")
        rows = deps.mapping.links_of_run(live(), run_id)
        return [r for r in rows if status is None or r.get("status") == status]

    @router.get("/mapping/reports", summary="Saved run reports, newest first (a dry run is not written yet)")
    def mapping_reports() -> list[dict[str, Any]]:
        return deps.mapping.list_reports(live())

    @router.get("/mapping/report", summary="The links of one saved run, to review before anything is written")
    def mapping_report(run_id: str) -> dict[str, Any]:
        if not _valid_run_id(run_id):
            raise HTTPException(400, "Invalid run id.")
        try:
            return deps.mapping.report_for_review(live(), run_id)
        except service.ReportNotFound as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.post("/mapping/apply", summary="Write only the links picked from a saved dry run")
    def mapping_apply(body: ApplyBody) -> dict[str, Any]:
        require_write()
        client = live()
        try:
            return deps.mapping.apply_report(
                settings, client, body.run_id, [(d.chunk_id, d.asset_id) for d in body.accept],
                [(d.chunk_id, d.asset_id) for d in body.reject], writer_factory=deps.writer_factory,
            )
        except service.ReportNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except service.AlreadyWritten as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/mapping/decide", summary="Accept or reject specific links of a run")
    def mapping_decide(body: DecideBody) -> dict[str, int]:
        require_write()
        return {"changed": _with_writer(deps, settings, lambda w: w.decide(body.run_id, [(d.chunk_id, d.asset_id) for d in body.decisions], body.status))}

    @router.post("/mapping/review", summary="Accept or reject all proposed links of a run")
    def mapping_review(body: ReviewBody) -> dict[str, int]:
        require_write()
        return {"changed": _with_writer(deps, settings, lambda w: w.review(body.run_id, body.status, body.min_confidence))}

    @router.post("/mapping/restore", summary="Undo a run: retract its links and bring back what it superseded")
    def mapping_restore(body: RestoreBody) -> dict[str, int]:
        require_write()
        return _with_writer(deps, settings, lambda w: w.restore(body.run_id))

    return router


def _valid_run_id(run_id: str) -> bool:
    import re

    return bool(re.match(RUN_ID, run_id))


def _with_writer(deps: JobsDeps, settings: Settings, action: Callable[[MappingWriter], Any]) -> Any:
    writer = deps.writer_factory(settings)
    try:
        return action(writer)
    finally:
        writer.close()
