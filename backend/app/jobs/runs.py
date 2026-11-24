"""Run tracking and execution for the Jobs page.

A *run* is one button press: a single Fabric job, a chain of them (one after another, stopping at the first
failure), or a local mapping run. Runs execute in background threads and are kept in memory (the last few
dozen), so the page can poll them. A restart of the API forgets finished runs; Fabric itself keeps its own
history, and mapping runs are also recorded in Neo4j as ``MappingRun`` nodes.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import Settings
from .fabric import TERMINAL, FabricClient, FabricError
from .registry import Job

log = logging.getLogger(__name__)

ACTIVE = ("queued", "running")


@dataclass
class StepState:
    key: str
    title: str
    status: str = "pending"  # pending | resolving | NotStarted | InProgress | Completed | Failed | Cancelled | Deduped | TimedOut
    item_id: str | None = None
    instance_id: str | None = None
    message: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    started_at: float | None = None
    finished_at: float | None = None


@dataclass
class RunState:
    id: str
    kind: str  # fabric | chain | mapping
    key: str
    title: str
    started_at: float
    status: str = "queued"  # queued | running | succeeded | failed | cancelled
    steps: list[StepState] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    progress: dict[str, int] | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    finished_at: float | None = None
    cancel_requested: bool = False


class JobStore:
    def __init__(self, synchronous: bool = False, keep: int = 50):
        self.runs: dict[str, RunState] = {}
        self.lock = threading.RLock()
        self.synchronous = synchronous  # tests run the work inline
        self.keep = keep

    def new(self, kind: str, key: str, title: str, steps: list[StepState] | None = None) -> RunState:
        run = RunState(id=uuid.uuid4().hex[:12], kind=kind, key=key, title=title, started_at=time.time(), steps=steps or [])
        with self.lock:
            self.runs[run.id] = run
            for old in sorted(self.runs.values(), key=lambda r: r.started_at)[: max(0, len(self.runs) - self.keep)]:
                if old.status not in ACTIVE:
                    self.runs.pop(old.id, None)
        return run

    def get(self, run_id: str) -> RunState | None:
        return self.runs.get(run_id)

    def recent(self, limit: int = 30) -> list[RunState]:
        with self.lock:
            return sorted(self.runs.values(), key=lambda r: r.started_at, reverse=True)[:limit]

    def busy_keys(self) -> set[str]:
        """Keys of jobs that a queued or running run still has to execute or is executing."""
        with self.lock:
            out: set[str] = set()
            for r in self.runs.values():
                if r.status in ACTIVE:
                    out.add(r.key)
                    out.update(s.key for s in r.steps if s.status in ("pending", "resolving", "NotStarted", "InProgress"))
            return out

    def start(self, run: RunState, target: Callable[[], None]) -> None:
        def body() -> None:
            with self.lock:
                run.status = "running"
            try:
                target()
            except Exception as exc:  # noqa: BLE001 - a run must always end in a final state
                log.exception("Run %s crashed", run.id)
                with self.lock:
                    run.status, run.error = "failed", f"Unexpected error ({type(exc).__name__}). See the server log."
            finally:
                with self.lock:
                    run.finished_at = time.time()

        if self.synchronous:
            body()
        else:
            threading.Thread(target=body, name=f"job-{run.id}", daemon=True).start()

    def to_dict(self, run: RunState) -> dict[str, Any]:
        with self.lock:
            return {
                "id": run.id, "kind": run.kind, "key": run.key, "title": run.title, "status": run.status,
                "started_at": run.started_at, "finished_at": run.finished_at, "error": run.error,
                "progress": dict(run.progress) if run.progress else None, "result": run.result,
                "log": run.log[-40:], "cancel_requested": run.cancel_requested,
                "steps": [
                    {"key": s.key, "title": s.title, "status": s.status, "message": s.message, "item_id": s.item_id,
                     "instance_id": s.instance_id, "started_at": s.started_at, "finished_at": s.finished_at}
                    for s in run.steps
                ],
            }


class ItemResolver:
    """Finds each job's Fabric item by display name, with a short cache so the page can poll cheaply."""

    def __init__(self, client: FabricClient, ttl: float = 60.0, clock: Callable[[], float] = time.time):
        self.client, self.ttl, self.clock = client, ttl, clock
        self._cache: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}
        self._lock = threading.Lock()

    def _items(self, item_type: str, refresh: bool) -> dict[str, dict[str, Any]]:
        with self._lock:
            hit = self._cache.get(item_type)
            if hit and not refresh and self.clock() - hit[0] < self.ttl:
                return hit[1]
            items = {str(i["displayName"]).lower(): i for i in self.client.list_items(item_type)}
            self._cache[item_type] = (self.clock(), items)
            return items

    def resolve(self, job: Job, refresh: bool = False) -> dict[str, Any] | None:
        items = self._items(job.item_type, refresh)
        for name in job.names:
            if name.lower() in items:
                return items[name.lower()]
        return None

    def overview(self, jobs: tuple[Job, ...], refresh: bool = False) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for job in jobs:
            item = self.resolve(job, refresh)
            refresh = False  # one listing per item type is enough
            out[job.key] = {"found": item is not None, "item_id": item["id"] if item else None,
                            "display_name": item["displayName"] if item else None, "looked_for": list(job.names)}
        return out


def execute_chain(
    store: JobStore,
    run: RunState,
    client: FabricClient,
    resolver: ItemResolver,
    jobs: list[tuple[Job, dict[str, Any]]],
    settings: Settings,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> None:
    """Run the jobs one after another. Stops at the first one that does not complete."""
    timeout = settings.fabric_step_timeout_minutes * 60

    def note(msg: str) -> None:
        with store.lock:
            run.log.append(time.strftime("%H:%M:%S ") + msg)

    for step, (job, params) in zip(run.steps, jobs):
        if run.cancel_requested:
            with store.lock:
                for rest in run.steps:
                    if rest.status == "pending":
                        rest.status = "Cancelled"
                run.status = "cancelled"
            return
        try:
            with store.lock:
                step.status, step.started_at, step.parameters = "resolving", clock(), params
            item = resolver.resolve(job, refresh=True)
            if item is None:
                raise FabricError(f"'{job.title}' was not found in the workspace. Looked for: {', '.join(job.names)}.")
            handle = client.run(str(item["id"]), job.fabric_job_type, params)
            with store.lock:
                step.item_id, step.instance_id, step.status = str(item["id"]), handle.instance_id, "NotStarted"
            note(f"started {job.title} ({handle.instance_id})")
            cancel_sent = False
            deadline = clock() + timeout
            while True:
                st = client.status(str(item["id"]), handle.instance_id)
                with store.lock:
                    step.status, step.message = st["status"], st["failure"] or ""
                if st["status"] in TERMINAL:
                    break
                if run.cancel_requested and not cancel_sent:
                    client.cancel(str(item["id"]), handle.instance_id)
                    cancel_sent = True
                    note(f"cancel requested for {job.title}")
                if clock() > deadline:
                    with store.lock:
                        step.status, step.message = "TimedOut", f"Still running after {settings.fabric_step_timeout_minutes} minutes; stopped waiting (the run may still be going in Fabric)."
                        run.status, run.error = "failed", step.message
                    return
                sleep(max(settings.fabric_poll_seconds, 0))
            with store.lock:
                step.finished_at = clock()
            note(f"{job.title}: {st['status']}")
            if st["status"] == "Cancelled":
                with store.lock:
                    run.status = "cancelled"
                    for rest in run.steps:
                        if rest.status == "pending":
                            rest.status = "Cancelled"
                return
            if st["status"] != "Completed":
                reason = st["failure"] or (
                    "Fabric skipped it because an instance of this job is already running." if st["status"] == "Deduped" else f"Ended as {st['status']}."
                )
                with store.lock:
                    step.message = reason
                    run.status, run.error = "failed", f"{job.title}: {reason}"
                return
        except FabricError as exc:
            with store.lock:
                step.status, step.message, step.finished_at = "Failed", str(exc), clock()
                run.status, run.error = "failed", f"{job.title}: {exc}"
            return
    with store.lock:
        run.status = "succeeded"


def new_chain_run(store: JobStore, kind: str, key: str, title: str, jobs: list[tuple[Job, dict[str, Any]]]) -> RunState:
    return store.new(kind, key, title, [StepState(key=j.key, title=j.title, parameters=p) for j, p in jobs])
