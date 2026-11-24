"""Reading chunks and existing links, and writing the plan to Neo4j.

``Neo4jReader`` is read-only and safe anywhere. ``MappingWriter`` is the ONLY code in this project that writes
to the graph. It is used by the mapping job (script or Fabric notebook) and by the few Jobs-page endpoints that
accept / reject / undo links or run the mapping with ``write``. Those endpoints check ``MAPPING_UI_WRITE`` first;
every other API route, the Cypher workbench included, stays read-only.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from neo4j import WRITE_ACCESS, GraphDatabase

from ..config import Settings
from ..neo4j_client import Neo4jClient
from .engine import SCORE, Chunk, Plan
from .prompts import PROMPT_VERSION

_BATCH = 200

CHUNKS = """MATCH (d:PolicyDocument)-[:HAS_CHUNK]->(c:PolicyChunk)
RETURN d.doc_id AS doc_id, d.title AS doc_title, c.chunk_id AS chunk_id, c.section_path AS section_path,
       c.heading AS heading, c.text AS text
ORDER BY doc_title, coalesce(c.section_no, ''), chunk_id"""

EXISTING = """MATCH (c:PolicyChunk)-[g:GOVERNS]->(a:Asset)
RETURN c.chunk_id AS chunk_id, a.id AS asset_id, coalesce(g.status, 'accepted') AS status"""

CREATE_RUN = """CREATE (r:MappingRun {run_id: $run_id, mode: $mode, started_at: toString(datetime()), models: $models,
  prompt_version: $prompt_version, catalogue_hash: $catalogue_hash, created: $created, superseded: $superseded})"""

# Retire live links without deleting them; `prior_status` lets `restore` put them back exactly as they were.
SUPERSEDE = """UNWIND $pairs AS p
MATCH (:PolicyChunk {chunk_id: p.chunk_id})-[g:GOVERNS]->(:Asset {id: p.asset_id})
WHERE coalesce(g.status, 'accepted') IN ['accepted', 'proposed']
SET g.prior_status = coalesce(g.status, 'accepted'), g.status = 'superseded',
    g.superseded_by = $run_id, g.superseded_at = toString(datetime())
RETURN count(g) AS n"""

# CREATE, not MERGE: a superseded link and its replacement can both exist for the same pair, and MERGE would
# overwrite the old link's properties.
CREATE_LINKS = """UNWIND $rows AS r
MATCH (c:PolicyChunk {chunk_id: r.chunk_id})
MATCH (a:Asset {id: r.asset_id})
CREATE (c)-[g:GOVERNS]->(a)
SET g += r.props, g.created_at = toString(datetime())
RETURN count(g) AS n"""

RETRACT_RUN = """MATCH ()-[g:GOVERNS {run_id: $run_id}]->()
WHERE g.status IN ['accepted', 'proposed']
SET g.status = 'retracted', g.retracted_at = toString(datetime())
RETURN count(g) AS n"""

RESTORE_SUPERSEDED = """MATCH ()-[g:GOVERNS {superseded_by: $run_id}]->()
SET g.status = coalesce(g.prior_status, 'accepted'), g.restored_at = toString(datetime())
REMOVE g.superseded_by, g.superseded_at, g.prior_status
RETURN count(g) AS n"""

DECIDE = """UNWIND $pairs AS p
MATCH (:PolicyChunk {chunk_id: p.chunk_id})-[g:GOVERNS {run_id: $run_id}]->(:Asset {id: p.asset_id})
WHERE g.status IN ['proposed', 'accepted', 'rejected']
SET g.status = $status, g.reviewed_at = toString(datetime())
RETURN count(g) AS n"""

REVIEW = """MATCH ()-[g:GOVERNS {run_id: $run_id, status: 'proposed'}]->()
WHERE g.confidence IN $levels
SET g.status = $status, g.reviewed_at = toString(datetime())
RETURN count(g) AS n"""


class Neo4jReader:
    """Read-only access to chunks and the links already in the graph."""

    def __init__(self, client: Neo4jClient):
        self.client = client

    def chunks(self) -> list[Chunk]:
        rows = self.client.run_read(CHUNKS, max_rows=100_000).records
        return [
            Chunk(
                chunk_id=str(r["chunk_id"]), doc_id=str(r["doc_id"] or ""), doc_title=str(r["doc_title"] or ""),
                section_path=str(r["section_path"] or ""), heading=str(r["heading"] or ""), text=str(r["text"] or ""),
            )
            for r in rows
            if r.get("text")
        ]

    def written_run_ids(self) -> set[str]:
        """Runs that have been written to the graph (each one leaves a MappingRun node)."""
        rows = self.client.run_read("MATCH (m:MappingRun) RETURN m.run_id AS run_id", max_rows=100_000).records
        return {str(r["run_id"]) for r in rows}

    def existing_links(self) -> dict[tuple[str, str], set[str]]:
        out: dict[tuple[str, str], set[str]] = {}
        for r in self.client.run_read(EXISTING, max_rows=1_000_000).records:
            out.setdefault((str(r["chunk_id"]), str(r["asset_id"])), set()).add(str(r["status"]))
        return out


def chunks_from_rows(rows: Iterable[dict[str, Any]], columns: dict[str, str] | None = None) -> list[Chunk]:
    """Build chunks from table rows (e.g. the Fabric ``policy_chunks`` table as dicts).

    ``columns`` maps our field names to the table's column names, for tables that name them differently.
    Only ``chunk_id`` and ``text`` are required.
    """
    cols = {"chunk_id": "chunk_id", "doc_id": "doc_id", "doc_title": "doc_title",
            "section_path": "section_path", "heading": "heading", "text": "text", **(columns or {})}
    out: list[Chunk] = []
    for i, row in enumerate(rows):
        missing = [c for c in ("chunk_id", "text") if cols[c] not in row]
        if missing:
            raise KeyError(f"Row {i} has no column(s) {[cols[m] for m in missing]}; pass `columns` to map them. Row has: {sorted(row)}")

        def get(field: str) -> str:
            v = row.get(cols[field])
            return "" if v is None else str(v)

        if get("text").strip():
            out.append(Chunk(get("chunk_id"), get("doc_id"), get("doc_title"), get("section_path"), get("heading"), get("text")))
    return out


class MappingWriter:
    """Writes mapping plans to Neo4j. Opens its own write-capable session, separate from the read-only client."""

    def __init__(self, settings: Settings):
        self._driver = GraphDatabase.driver(
            settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password), connection_timeout=15
        )
        self._database = settings.neo4j_database

    def close(self) -> None:
        self._driver.close()

    def _write(self, work):  # type: ignore[no-untyped-def]
        with self._driver.session(database=self._database, default_access_mode=WRITE_ACCESS) as session:
            return session.execute_write(work)

    @staticmethod
    def _props(ln, doc_id: str, run_id: str, models: dict[str, str | None]) -> dict[str, Any]:  # noqa: ANN001
        return {
            "doc_id": doc_id,
            "method": "llm-category" if ln.basis == "category" else "llm-map",
            "score": SCORE[ln.confidence],
            "term": ln.evidence[:120],
            "source": "llm_mapping",
            "status": ln.status,
            "relation": ln.relation,
            "confidence": ln.confidence,
            "evidence": ln.evidence,
            "reason": ln.reason,
            "category": ln.category or "",
            "votes": json.dumps(ln.votes),
            "note": ln.note,
            "run_id": run_id,
            "prompt_version": PROMPT_VERSION,
            "models": json.dumps(models),
        }

    def apply(self, plan: Plan, models: dict[str, str | None], doc_ids: dict[str, str], catalogue_hash: str = "") -> dict[str, int]:
        """Supersede (overwrite mode only) and create, in one transaction: either all of it lands or none."""
        rows = [
            {"chunk_id": ln.chunk_id, "asset_id": ln.asset_id, "props": self._props(ln, doc_ids.get(ln.chunk_id, ""), plan.run_id, models)}
            for ln in plan.create
        ]
        pairs = [{"chunk_id": c, "asset_id": a} for c, a in plan.supersede]

        def work(tx):  # type: ignore[no-untyped-def]
            superseded = 0
            for i in range(0, len(pairs), _BATCH):
                superseded += tx.run(SUPERSEDE, pairs=pairs[i : i + _BATCH], run_id=plan.run_id).single()["n"]
            created = 0
            for i in range(0, len(rows), _BATCH):
                created += tx.run(CREATE_LINKS, rows=rows[i : i + _BATCH]).single()["n"]
            tx.run(
                CREATE_RUN, run_id=plan.run_id, mode=plan.mode, models=json.dumps(models), prompt_version=PROMPT_VERSION,
                catalogue_hash=catalogue_hash, created=created, superseded=superseded,
            )
            return {"created": created, "superseded": superseded}

        return self._write(work)

    def restore(self, run_id: str) -> dict[str, int]:
        """Undo a run: its links become `retracted` and whatever it superseded is live again."""

        def work(tx):  # type: ignore[no-untyped-def]
            return {
                "retracted": tx.run(RETRACT_RUN, run_id=run_id).single()["n"],
                "restored": tx.run(RESTORE_SUPERSEDED, run_id=run_id).single()["n"],
            }

        return self._write(work)

    def decide(self, run_id: str, pairs: list[tuple[str, str]], status: str) -> int:
        """Accept or reject specific links of a run, one decision per (chunk_id, asset_id). A rejection is kept,
        so a later run never creates that link again; a mistaken one can be flipped back to accepted."""
        if status not in ("accepted", "rejected"):
            raise ValueError("status must be 'accepted' or 'rejected'")
        rows = [{"chunk_id": c, "asset_id": a} for c, a in pairs]
        return self._write(lambda tx: tx.run(DECIDE, run_id=run_id, pairs=rows, status=status).single()["n"]) if rows else 0

    def review(self, run_id: str, status: str, min_confidence: str = "medium") -> int:
        """Accept or reject a run's `proposed` links in bulk (decisions are kept: rejected pairs never return)."""
        if status not in ("accepted", "rejected"):
            raise ValueError("status must be 'accepted' or 'rejected'")
        levels = ["high"] if min_confidence == "high" else ["high", "medium"]
        return self._write(lambda tx: tx.run(REVIEW, run_id=run_id, status=status, levels=levels).single()["n"])
