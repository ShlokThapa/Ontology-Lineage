"""The mapping job as plain functions, shared by the command line and the Jobs page.

Nothing here prints or touches the web layer. Reads go through the read-only Neo4j client; the only writes are
the ones ``execute`` makes through ``MappingWriter`` when ``write=True``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..config import REPO_DIR, Settings
from ..neo4j_client import Neo4jClient
from ..repository import LiveRepository
from .catalogue import build_catalogue
from .engine import ACTIVE, ChunkResult, Link, Models, Plan, RunConfig, run_mapping
from .models import load_models
from .store import MappingWriter, Neo4jReader

REPORT_DIR = REPO_DIR / "reports"
CACHE_DIR = REPO_DIR / ".mapping_cache"

LINKS_OF_RUN = """MATCH (c:PolicyChunk)-[g:GOVERNS {run_id: $run_id}]->(a:Asset)
RETURN c.chunk_id AS chunk_id, c.section_path AS section, a.id AS asset_id,
       coalesce(a.full_name, a.table + '.' + a.name, a.name) AS asset, g.status AS status,
       g.confidence AS confidence, g.relation AS relation, g.evidence AS evidence, g.reason AS reason,
       g.category AS category, g.note AS note
ORDER BY section, asset"""

RECENT_RUNS = """MATCH (m:MappingRun)
OPTIONAL MATCH ()-[g:GOVERNS {run_id: m.run_id}]->()
RETURN m.run_id AS run_id, m.mode AS mode, m.started_at AS started_at, m.models AS models,
       m.created AS created, m.superseded AS superseded, collect(g.status) AS statuses
ORDER BY started_at DESC LIMIT $limit"""


@dataclass
class RunOptions:
    mode: str = "append"
    write: bool = False
    confirm_overwrite: bool = False
    only_unlinked: bool = False
    no_auto_accept: bool = False
    accept_self_verified: bool = False
    docs: list[str] = field(default_factory=list)
    chunks: list[str] = field(default_factory=list)
    limit: int | None = None
    workers: int = 4
    use_cache: bool = True

    def validate(self) -> None:
        if self.mode not in ("append", "overwrite"):
            raise ValueError("mode must be 'append' or 'overwrite'.")
        if self.write and self.mode == "overwrite" and not self.confirm_overwrite:
            raise ValueError(
                "Overwrite replaces the live links of the chosen chunks. Confirm it to proceed "
                "(the old links are kept as 'superseded' and Undo restores them)."
            )
        if not 1 <= self.workers <= 16:
            raise ValueError("workers must be between 1 and 16.")
        if self.limit is not None and self.limit < 1:
            raise ValueError("limit must be at least 1.")


def select_chunks(chunks, docs: list[str], ids: list[str], limit: int | None):  # type: ignore[no-untyped-def]
    if docs:
        needles = [d.lower() for d in docs if d.strip()]
        chunks = [c for c in chunks if any(n in c.doc_title.lower() or n in c.doc_id.lower() for n in needles)]
    if ids:
        wanted = set(ids)
        chunks = [c for c in chunks if c.chunk_id in wanted]
    return chunks[:limit] if limit else chunks


def check(settings: Settings, client: Neo4jClient) -> dict[str, Any]:
    """What a run would use. Reads Neo4j only; calls no model."""
    reader = Neo4jReader(client)
    cat = build_catalogue(LiveRepository(client).catalogue())
    chunks, existing = reader.chunks(), reader.existing_links()
    live = {c for (c, _), st in existing.items() if st & {"accepted", "proposed"}}
    slots = [{"slot": n, "role": role, "model": settings.llm_slot(n)["model"] or None}
             for n, role in ((1, "mapper"), (2, "verifier"), (3, "tie-breaker"))]
    return {
        "catalogue": {"tables": cat.tables, "columns": cat.columns, "categories": sorted(cat.categories), "hash": cat.hash},
        "chunks": {"total": len(chunks), "documents": len({c.doc_id for c in chunks}), "with_live_link": len(live), "without_link": len(chunks) - len(live)},
        "existing_links": len(existing),
        "models": slots,
        "independent_verifier": bool(settings.llm_slot(2)["model"]),
    }


def execute(
    settings: Settings,
    opts: RunOptions,
    client: Neo4jClient,
    progress: Callable[[int, int, ChunkResult], None] | None = None,
    models: Models | None = None,
    writer_factory: Callable[[Settings], MappingWriter] = MappingWriter,
    report_dir: Path = REPORT_DIR,
    cache_dir: Path = CACHE_DIR,
) -> dict[str, Any]:
    """Map the chunks, save a report, and (only if ``opts.write``) apply the plan to Neo4j."""
    opts.validate()
    models = models or load_models(settings)
    reader = Neo4jReader(client)
    cat = build_catalogue(LiveRepository(client).catalogue())
    chunks = select_chunks(reader.chunks(), opts.docs, opts.chunks, opts.limit)
    cfg = RunConfig(
        mode=opts.mode, only_unlinked=opts.only_unlinked, auto_accept=not opts.no_auto_accept,
        accept_self_verified=opts.accept_self_verified, workers=opts.workers, cache_dir=cache_dir if opts.use_cache else None,
    )
    report = run_mapping(chunks, cat, models, reader.existing_links(), cfg, progress)

    report_dir.mkdir(exist_ok=True)
    path = report_dir / f"mapping-{report.run_id}.json"
    path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")

    written = None
    if opts.write:
        writer = writer_factory(settings)
        try:
            written = writer.apply(report.plan, models.names(), {c.chunk_id: c.doc_id for c in chunks}, cat.hash)
        finally:
            writer.close()
    return {
        "run_id": report.run_id,
        "summary": report.summary(),
        "report_path": str(path),
        "written": written,
        "failed_chunks": [r.chunk.chunk_id for r in report.results if r.error],
        "dry_run": not opts.write,
    }


class ReportNotFound(ValueError):
    """There is no saved report for that run."""


class AlreadyWritten(ValueError):
    """The run was already written to Neo4j; use the review of written runs instead."""


_RUN_ID = re.compile(r"^[0-9A-Za-z-]{6,64}$")
_REVIEW_FIELDS = ("chunk_id", "doc_title", "section_path", "asset", "asset_id", "relation", "confidence", "status",
                  "basis", "category", "evidence", "reason", "note")


def _report_path(run_id: str, report_dir: Path) -> Path:
    if not _RUN_ID.match(run_id or ""):
        raise ValueError("Invalid run id.")
    path = (report_dir / f"mapping-{run_id}.json").resolve()
    if report_dir.resolve() not in path.parents:
        raise ValueError("Invalid run id.")
    return path


def load_report(run_id: str, report_dir: Path = REPORT_DIR) -> dict[str, Any]:
    path = _report_path(run_id, report_dir)
    if not path.is_file():
        raise ReportNotFound(f"No saved report for run {run_id}. Reports are kept on the server in {report_dir}.")
    return json.loads(path.read_text(encoding="utf-8"))


def reviewable(report: dict[str, Any]) -> list[dict[str, Any]]:
    """The links a person can still decide on: planned for writing and not dropped by the models."""
    return [r for r in report.get("links", []) if r.get("will_write") and r.get("status") != "dropped"]


def list_reports(client: Neo4jClient, report_dir: Path = REPORT_DIR, limit: int = 30) -> list[dict[str, Any]]:
    """Saved run reports, newest first, each marked as written to Neo4j or not (a dry run is not)."""
    written = Neo4jReader(client).written_run_ids()
    out: list[dict[str, Any]] = []
    for path in sorted(report_dir.glob("mapping-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        s = data.get("summary", {})
        run_id = str(s.get("run_id") or path.stem.removeprefix("mapping-"))
        out.append({
            "run_id": run_id, "created": path.stat().st_mtime, "mode": s.get("mode"), "chunks": s.get("chunks_processed", 0),
            "links": len(reviewable(data)), "written": run_id in written,
        })
    return out


def report_for_review(client: Neo4jClient, run_id: str, report_dir: Path = REPORT_DIR) -> dict[str, Any]:
    report = load_report(run_id, report_dir)
    s = report.get("summary", {})
    return {
        "run_id": run_id,
        "written": run_id in Neo4jReader(client).written_run_ids(),
        "summary": {k: s.get(k) for k in ("mode", "chunks_processed", "to_create", "skipped_existing", "links_rejected_by_code", "independent_verification")},
        "links": [{k: r.get(k) for k in _REVIEW_FIELDS} for r in reviewable(report)],
    }


def apply_report(
    settings: Settings,
    client: Neo4jClient,
    run_id: str,
    accept: list[tuple[str, str]],
    reject: list[tuple[str, str]],
    writer_factory: Callable[[Settings], MappingWriter] = MappingWriter,
    report_dir: Path = REPORT_DIR,
) -> dict[str, Any]:
    """Write only the links a person picked from a saved dry run.

    ``accept`` links are written as `accepted` (a person has now looked at them); ``reject`` links are written as
    `rejected`, so no later run proposes them again. Everything not listed is not written at all. The links come
    from the saved report, never from the caller, and the graph is re-checked first: a pair that is already live,
    or that someone rejected before, is skipped.
    """
    both = set(accept) & set(reject)
    if both:
        raise ValueError("A link cannot be both accepted and rejected.")
    wanted = {**{p: "accepted" for p in accept}, **{p: "rejected" for p in reject}}
    if not wanted:
        raise ValueError("Pick at least one link to write.")
    report = load_report(run_id, report_dir)
    rows = {(r["chunk_id"], r["asset_id"]): r for r in reviewable(report)}
    unknown = [p for p in wanted if p not in rows]
    if unknown:
        raise ValueError(f"{len(unknown)} of the picked links are not in that report.")

    reader = Neo4jReader(client)
    if run_id in reader.written_run_ids():
        raise AlreadyWritten("That run was already written to Neo4j. Review it under the written runs instead.")
    existing = reader.existing_links()
    create: list[Link] = []
    skipped: list[dict[str, str]] = []
    for pair, status in wanted.items():
        row, statuses = rows[pair], existing.get(pair, set())
        if "rejected" in statuses:
            skipped.append({"chunk_id": pair[0], "ref": row["asset"], "reason": "previously_rejected"})
        elif statuses & ACTIVE:
            skipped.append({"chunk_id": pair[0], "ref": row["asset"], "reason": "already_mapped"})
        else:
            create.append(Link(
                chunk_id=pair[0], ref=row["asset"], asset_id=pair[1], relation=row["relation"], evidence=row["evidence"],
                reason=row.get("reason") or "", confidence=row["confidence"], basis=row.get("basis") or "direct",
                category=row.get("category") or None, status=status, votes=json.loads(row.get("votes") or "{}"),
                note=" | ".join(x for x in (row.get("note") or "", "reviewed before writing") if x)[:400],
            ))
    models = report.get("summary", {}).get("models") or {}
    plan = Plan("append", run_id, create=create, skipped=skipped)
    written = None
    if create:
        writer = writer_factory(settings)
        try:
            written = writer.apply(plan, models, {r["chunk_id"]: r.get("doc_id") or "" for r in rows.values()},
                                   str(report.get("summary", {}).get("catalogue", {}).get("hash", "")))
        finally:
            writer.close()
    return {
        "run_id": run_id, "created": written["created"] if written else 0,
        "accepted": sum(1 for ln in create if ln.status == "accepted"), "rejected": sum(1 for ln in create if ln.status == "rejected"),
        "skipped": skipped,
    }


def links_of_run(client: Neo4jClient, run_id: str) -> list[dict[str, Any]]:
    return client.run_read(LINKS_OF_RUN, {"run_id": run_id}, max_rows=5000).records


def recent_runs(client: Neo4jClient, limit: int = 20) -> list[dict[str, Any]]:
    rows = client.run_read(RECENT_RUNS, {"limit": limit}, max_rows=limit).records
    out = []
    for r in rows:
        counts: dict[str, int] = {}
        for s in r.get("statuses") or []:
            counts[str(s)] = counts.get(str(s), 0) + 1
        try:
            models = json.loads(r.get("models") or "{}")
        except (TypeError, json.JSONDecodeError):
            models = {}
        out.append({**{k: r[k] for k in ("run_id", "mode", "started_at", "created", "superseded")}, "models": models, "links_by_status": counts})
    return out
