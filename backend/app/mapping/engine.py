"""The mapping engine: propose, verify, check, plan.

Per policy chunk: the mapper model proposes links; every link is checked in code (known asset, a quoted
evidence that really appears in the chunk); a second model verifies the survivors and a third settles
disagreements; the votes decide a status. A separate planning step turns the results into creates / supersedes
for ``append`` or ``overwrite`` without ever touching the database.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from ..chat.llm import LLMError, LLMReply
from . import prompts
from .catalogue import Catalogue

log = logging.getLogger(__name__)

ACTIVE = {"accepted", "proposed"}  # statuses that count as a live link
SCORE = {"high": 0.9, "medium": 0.7}  # numeric `score` kept for compatibility with the older links


class StructuredLLM(Protocol):
    model: str

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, json_schema: dict[str, Any] | None = None
    ) -> LLMReply: ...


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    doc_id: str
    doc_title: str
    section_path: str
    heading: str
    text: str


@dataclass
class Link:
    chunk_id: str
    ref: str  # catalogue reference, or a category like "@PII" before expansion
    asset_id: str
    relation: str
    evidence: str
    reason: str
    confidence: str  # high | medium
    basis: str = "direct"  # direct | category
    category: str | None = None
    status: str = "proposed"  # accepted | proposed | dropped
    votes: dict[str, str] = field(default_factory=dict)  # role -> supported | unsupported
    note: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Link":
        return cls(**d)


@dataclass
class ChunkResult:
    chunk: Chunk
    links: list[Link] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)  # links the code threw out, with the reason
    tokens: dict[str, int] = field(default_factory=dict)
    cached: bool = False
    error: str | None = None


@dataclass
class Models:
    mapper: StructuredLLM
    verifier: StructuredLLM | None = None
    adjudicator: StructuredLLM | None = None

    def names(self) -> dict[str, str | None]:
        return {
            "mapper": self.mapper.model,
            "verifier": self.verifier.model if self.verifier else None,
            "adjudicator": self.adjudicator.model if self.adjudicator else None,
        }

    @staticmethod
    def _identity(llm: StructuredLLM) -> str:
        return getattr(llm, "identity", llm.model)

    @property
    def independent_verifier(self) -> bool:
        """True when the verifier is a different model from the mapper. A model agreeing with its own answer is
        not independent evidence, so its verdicts alone never auto-accept a link (see ``RunConfig``)."""
        return self.verifier is not None and self._identity(self.verifier) != self._identity(self.mapper)


@dataclass
class RunConfig:
    mode: str = "append"  # append | overwrite
    only_unlinked: bool = False  # skip chunks that already have a live link (saves calls)
    auto_accept: bool = True  # verified high-confidence links become `accepted`; otherwise all are `proposed`
    # When one model plays mapper and verifier, its verdict is not independent. By default such links stay
    # `proposed` for a person; set this to let the high-confidence ones through as `accepted` anyway.
    accept_self_verified: bool = False
    max_links: int = 40  # per chunk; a classification table can legitimately name dozens of columns
    workers: int = 4
    cache_dir: Path | None = None


# --- checks done in code ----------------------------------------------------------------------------


def _norm(s: str) -> str:
    s = s.lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", s).strip()


def evidence_in_text(evidence: str, text: str) -> bool:
    """The quote must really be in the chunk. "…" splits it into pieces that must each appear."""
    haystack = _norm(text)
    pieces = [_norm(p).strip(" \"'") for p in re.split(r"\s*(?:…|\.\.\.)\s*", evidence or "")]
    pieces = [p for p in pieces if p]
    return bool(pieces) and all(len(p) >= 8 and p in haystack for p in pieces)


def parse_links(raw: dict[str, Any], chunk: Chunk, cat: Catalogue, max_links: int) -> tuple[list[Link], list[dict[str, str]]]:
    links: dict[str, Link] = {}
    rejected: list[dict[str, str]] = []
    categories = {k.upper(): k for k in cat.categories}
    for item in raw.get("links") or []:
        ref = str(item.get("asset", "")).strip()
        why = ""
        if ref.startswith("@"):
            canon = categories.get(ref.upper())
            if canon is None:
                why = "unknown_category"
            ref = canon or ref
        elif ref not in cat.refs:
            why = "unknown_asset"
        if not why and item.get("relation") not in prompts.RELATIONS:
            why = "invalid_relation"
        if not why and item.get("confidence") not in SCORE:
            why = "invalid_confidence"
        if not why and not evidence_in_text(str(item.get("evidence", "")), chunk.text):
            why = "evidence_not_in_text"
        if why:
            rejected.append({"ref": ref, "reason": why, "evidence": str(item.get("evidence", ""))[:200]})
            continue
        link = Link(
            chunk_id=chunk.chunk_id,
            ref=ref,
            asset_id="" if ref.startswith("@") else cat.refs[ref],
            relation=item["relation"],
            evidence=str(item["evidence"]).strip()[:300],
            reason=str(item.get("reason", "")).strip()[:300],
            confidence=item["confidence"],
        )
        keep = links.get(ref)
        if keep is None or (keep.confidence == "medium" and link.confidence == "high"):
            links[ref] = link
    ordered = sorted(links.values(), key=lambda ln: (ln.confidence != "high", ln.ref))
    for extra in ordered[max_links:]:
        rejected.append({"ref": extra.ref, "reason": "over_link_cap", "evidence": extra.evidence[:200]})
    return ordered[:max_links], rejected


# Sections that describe the policy itself rather than impose a rule. A category-wide link from one of these
# ("this policy applies to personal data") is real but policy-wide, so it is held for review, never auto-accepted.
_BOILERPLATE = re.compile(
    r"\b(purpose|scope|introduction|overview|background|definitions?|glossary|roles?|responsibilit\w*|review|"
    r"version|revision|preamble|document control)\b",
    re.IGNORECASE,
)


def is_boilerplate(chunk: Chunk) -> bool:
    return bool(_BOILERPLATE.search(f"{chunk.section_path} {chunk.heading}"))


def _status(link: Link, has_verifier: bool, has_adjudicator: bool, trusted: bool = True) -> tuple[str, str]:
    """``trusted`` is False when the verifier is the same model as the mapper: it may still drop links it finds
    unsupported, but its approval alone does not accept one."""
    if not has_verifier:
        return "proposed", "unverified: no verifier model configured"
    verifier, tiebreak = link.votes.get("verifier"), link.votes.get("adjudicator")
    if verifier == "supported":
        if link.confidence != "high":
            return "proposed", "medium confidence"
        if not trusted:
            return "proposed", "verified only by the same model that proposed it; a person should confirm"
        return "accepted", ""
    if tiebreak == "supported":
        return "proposed", "contested: the verifier disagreed, the tie-breaker supported it"
    return "dropped", "rejected by the verifier" + (" and the tie-breaker" if tiebreak else " (no tie-breaker configured)")


# --- model calls ------------------------------------------------------------------------------------


# One model playing every role means about three times the calls, which is where a token-per-minute limit
# shows up. A rate-limited call (HTTP 429) waits and tries again (the service's Retry-After if it sends one,
# else these seconds) before the chunk is given up on. Lowering --workers is the other lever.
RATE_LIMIT_WAITS = (5.0, 15.0, 45.0)
_sleep = time.sleep  # tests replace this so they never really wait


def _call(llm: StructuredLLM, messages: list[dict[str, str]], schema: dict[str, Any]) -> LLMReply:
    for attempt in range(len(RATE_LIMIT_WAITS) + 1):
        try:
            return llm.complete(messages, None, json_schema=schema)
        except LLMError as exc:
            if exc.status_code != 429 or attempt == len(RATE_LIMIT_WAITS):
                raise
            wait = min(exc.retry_after or RATE_LIMIT_WAITS[attempt], 120.0)
            log.warning("Rate limited by %s; waiting %.0fs (retry %d of %d)", llm.model, wait, attempt + 1, len(RATE_LIMIT_WAITS))
            _sleep(wait)
    raise AssertionError("unreachable")  # pragma: no cover


def _ask(llm: StructuredLLM, messages: list[dict[str, str]], schema: dict[str, Any], tokens: dict[str, int], role: str) -> dict[str, Any]:
    """One structured call, retried once if the reply is not valid JSON."""
    last: Exception | None = None
    for _ in range(2):
        reply = _call(llm, messages, schema)
        tokens[role] = tokens.get(role, 0) + reply.total_tokens
        try:
            data = json.loads(reply.content)
            if isinstance(data, dict):
                return data
            last = ValueError("reply was not a JSON object")
        except json.JSONDecodeError as exc:
            last = exc
    raise ValueError(f"{role} model returned unusable JSON ({last})")


class FileCache:
    """Per-chunk results on disk, so re-running costs nothing for chunks whose inputs did not change."""

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.dir / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        p = self._path(key)
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
        except (OSError, json.JSONDecodeError):
            return None

    def put(self, key: str, value: dict[str, Any]) -> None:
        try:
            self._path(key).write_text(json.dumps(value), encoding="utf-8")
        except OSError as exc:
            log.warning("Could not write mapping cache: %s", exc)


class Mapper:
    def __init__(self, catalogue: Catalogue, models: Models, cfg: RunConfig):
        self.cat, self.models, self.cfg = catalogue, models, cfg
        self.cache = FileCache(cfg.cache_dir) if cfg.cache_dir else None
        self.trusted = models.independent_verifier or cfg.accept_self_verified

    def _key(self, chunk: Chunk) -> str:
        blob = json.dumps(
            [prompts.PROMPT_VERSION, self.cat.hash, self.models.names(), self.trusted, self.cfg.max_links,
             chunk.doc_title, chunk.section_path, chunk.text],
            sort_keys=True,
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]

    def _verify(self, llm: StructuredLLM, chunk: Chunk, links: list[Link], tokens: dict[str, int], role: str) -> dict[str, tuple[str, str]]:
        data = _ask(llm, prompts.verifier_messages(self.cat, chunk, links), prompts.VERIFY_SCHEMA, tokens, role)
        return {
            str(v.get("asset", "")).strip(): (str(v.get("verdict")), str(v.get("reason", "")))
            for v in data.get("verdicts") or []
        }

    def map_chunk(self, chunk: Chunk) -> ChunkResult:
        key = self._key(chunk) if self.cache else ""
        if self.cache and (hit := self.cache.get(key)):
            return ChunkResult(
                chunk,
                [Link.from_dict(d) for d in hit["links"]],
                hit["rejected"],
                hit["tokens"],
                cached=True,
            )
        tokens: dict[str, int] = {}
        result = ChunkResult(chunk, tokens=tokens)
        try:
            raw = _ask(self.models.mapper, prompts.mapper_messages(self.cat, chunk), prompts.MAP_SCHEMA, tokens, "mapper")
            links, result.rejected = parse_links(raw, chunk, self.cat, self.cfg.max_links)
            for ln in links:
                ln.votes = {"mapper": "supported"}
            if links and self.models.verifier:
                verdicts = self._verify(self.models.verifier, chunk, links, tokens, "verifier")
                for ln in links:
                    verdict, reason = verdicts.get(ln.ref, ("unsupported", "no verdict returned"))
                    ln.votes["verifier"] = verdict
                    if verdict != "supported":
                        ln.note = f"verifier: {reason}"[:300]
                disputed = [ln for ln in links if ln.votes["verifier"] != "supported"]
                if disputed and self.models.adjudicator:
                    ruling = self._verify(self.models.adjudicator, chunk, disputed, tokens, "adjudicator")
                    for ln in disputed:
                        verdict, reason = ruling.get(ln.ref, ("unsupported", "no verdict returned"))
                        ln.votes["adjudicator"] = verdict
                        ln.note += f" | tie-breaker: {reason}"[:300]
            for ln in links:
                ln.status, why = _status(ln, self.models.verifier is not None, self.models.adjudicator is not None, self.trusted)
                if why:
                    ln.note = (why + (" | " + ln.note if ln.note else ""))[:400]
            result.links = self._expand_categories(chunk, links)
        except (LLMError, ValueError) as exc:
            result.error = str(exc)[:300]
            return result  # failures are never cached
        if self.cache:
            self.cache.put(key, {"links": [asdict(ln) for ln in result.links], "rejected": result.rejected, "tokens": tokens})
        return result

    def _expand_categories(self, chunk: Chunk, links: list[Link]) -> list[Link]:
        """A category link (@PII) becomes one link per column in that category; direct links win on overlap."""
        direct = {ln.ref for ln in links if not ln.ref.startswith("@")}
        out = [ln for ln in links if not ln.ref.startswith("@")]
        for ln in links:
            if not ln.ref.startswith("@"):
                continue
            if ln.status == "dropped":
                out.append(ln)  # kept so the report shows it was considered
                continue
            status, note = ln.status, ln.note
            if status == "accepted" and is_boilerplate(chunk):
                status, note = "proposed", ("policy-wide statement in a purpose/scope-type section | " + note).strip(" |")
            for col in self.cat.categories.get(ln.ref, []):
                if col in direct:
                    continue
                out.append(
                    Link(
                        chunk_id=ln.chunk_id, ref=col, asset_id=self.cat.refs[col], relation=ln.relation,
                        evidence=ln.evidence, reason=ln.reason, confidence=ln.confidence, basis="category",
                        category=ln.ref, status=status, votes=dict(ln.votes), note=note,
                    )
                )
        return out


# --- planning: append vs overwrite ------------------------------------------------------------------


@dataclass
class Plan:
    mode: str
    run_id: str
    create: list[Link] = field(default_factory=list)
    supersede: list[tuple[str, str]] = field(default_factory=list)  # (chunk_id, asset_id) of live links to retire
    skipped: list[dict[str, str]] = field(default_factory=list)
    orphaned_chunks: list[str] = field(default_factory=list)  # overwrite: chunks left with no live link at all


def plan_changes(
    results: list[ChunkResult],
    existing: dict[tuple[str, str], set[str]],
    cfg: RunConfig,
    run_id: str,
) -> Plan:
    """What would change in the graph. ``existing`` maps (chunk_id, asset_id) to the statuses already stored.

    append    never touches an existing link: only (chunk, asset) pairs that have no live link yet are created.
    overwrite replaces the live links of every successfully processed chunk: they are marked `superseded`
              (not deleted) and the new ones created. Chunks whose model call failed are left alone.
    Either way, a pair a person rejected is never created again.
    """
    plan = Plan(cfg.mode, run_id)
    live_by_chunk: dict[str, set[str]] = defaultdict(set)
    for (chunk_id, asset_id), statuses in existing.items():
        if statuses & ACTIVE:
            live_by_chunk[chunk_id].add(asset_id)

    for res in results:
        if res.error:
            continue
        kept = 0
        for ln in res.links:
            if ln.status == "dropped":
                continue
            statuses = existing.get((ln.chunk_id, ln.asset_id), set())
            if "rejected" in statuses:
                plan.skipped.append({"chunk_id": ln.chunk_id, "ref": ln.ref, "reason": "previously_rejected"})
                continue
            if cfg.mode == "append" and statuses & ACTIVE:
                plan.skipped.append({"chunk_id": ln.chunk_id, "ref": ln.ref, "reason": "already_mapped"})
                continue
            if ln.status == "accepted" and not cfg.auto_accept:
                ln.status = "proposed"
                ln.note = ("auto-accept off | " + ln.note).strip(" |")
            plan.create.append(ln)
            kept += 1
        if cfg.mode == "overwrite":
            plan.supersede.extend((res.chunk.chunk_id, a) for a in sorted(live_by_chunk.get(res.chunk.chunk_id, ())))
            if live_by_chunk.get(res.chunk.chunk_id) and not kept:
                plan.orphaned_chunks.append(res.chunk.chunk_id)
    return plan


@dataclass
class RunReport:
    run_id: str
    config: RunConfig
    models: dict[str, str | None]
    catalogue: dict[str, Any]
    chunks_total: int
    chunks_skipped: int
    results: list[ChunkResult]
    plan: Plan
    seconds: float
    independent_verification: bool = False  # False: the verifier is the same model as the mapper

    def tokens(self) -> dict[str, int]:
        total: dict[str, int] = defaultdict(int)
        for r in self.results:
            for role, n in r.tokens.items():
                total[role] += n
        return dict(total)

    def summary(self) -> dict[str, Any]:
        statuses: dict[str, int] = defaultdict(int)
        for r in self.results:
            for ln in r.links:
                statuses[ln.status] += 1
        return {
            "run_id": self.run_id,
            "mode": self.config.mode,
            "models": self.models,
            "independent_verification": self.independent_verification,
            "chunks_total": self.chunks_total,
            "chunks_skipped_already_linked": self.chunks_skipped,
            "chunks_processed": len(self.results),
            "chunks_failed": sum(1 for r in self.results if r.error),
            "chunks_from_cache": sum(1 for r in self.results if r.cached),
            "chunks_with_no_links": sum(1 for r in self.results if not r.error and not [x for x in r.links if x.status != "dropped"]),
            "links_by_status": dict(statuses),
            "links_rejected_by_code": sum(len(r.rejected) for r in self.results),
            "to_create": len(self.plan.create),
            "to_create_accepted": sum(1 for ln in self.plan.create if ln.status == "accepted"),
            "to_create_proposed": sum(1 for ln in self.plan.create if ln.status == "proposed"),
            "to_supersede": len(self.plan.supersede),
            "skipped_existing": sum(1 for s in self.plan.skipped if s["reason"] == "already_mapped"),
            "skipped_previously_rejected": sum(1 for s in self.plan.skipped if s["reason"] == "previously_rejected"),
            "chunks_left_without_links_by_overwrite": len(self.plan.orphaned_chunks),
            "tokens": self.tokens(),
            "seconds": round(self.seconds, 1),
            "catalogue": self.catalogue,
        }

    def rows(self) -> list[dict[str, Any]]:
        """One flat row per link considered (for a Delta table or a CSV)."""
        planned = {(ln.chunk_id, ln.ref) for ln in self.plan.create}
        out = []
        for r in self.results:
            for ln in r.links:
                out.append(
                    {
                        "run_id": self.run_id, "chunk_id": ln.chunk_id, "doc_id": r.chunk.doc_id, "doc_title": r.chunk.doc_title,
                        "section_path": r.chunk.section_path, "asset": ln.ref, "asset_id": ln.asset_id, "relation": ln.relation,
                        "confidence": ln.confidence, "status": ln.status, "basis": ln.basis, "category": ln.category,
                        "evidence": ln.evidence, "reason": ln.reason, "votes": json.dumps(ln.votes), "note": ln.note,
                        "will_write": (ln.chunk_id, ln.ref) in planned,
                    }
                )
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary(),
            "links": self.rows(),
            "rejected_by_code": [
                {"chunk_id": r.chunk.chunk_id, **x} for r in self.results for x in r.rejected
            ],
            "skipped": self.plan.skipped,
            "errors": [{"chunk_id": r.chunk.chunk_id, "error": r.error} for r in self.results if r.error],
            "supersede": [{"chunk_id": c, "asset_id": a} for c, a in self.plan.supersede],
        }


def run_mapping(
    chunks: list[Chunk],
    catalogue: Catalogue,
    models: Models,
    existing: dict[tuple[str, str], set[str]],
    cfg: RunConfig,
    progress: Callable[[int, int, ChunkResult], None] | None = None,
) -> RunReport:
    """Map every chunk (in parallel) and plan the changes. Touches nothing outside memory and the cache."""
    started = time.time()
    run_id = time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    live: dict[str, bool] = defaultdict(bool)
    for (chunk_id, _asset), statuses in existing.items():
        if statuses & ACTIVE:
            live[chunk_id] = True
    todo = [c for c in chunks if not (cfg.only_unlinked and live[c.chunk_id])]

    mapper = Mapper(catalogue, models, cfg)
    order = {c.chunk_id: i for i, c in enumerate(todo)}
    results: list[ChunkResult] = []
    with ThreadPoolExecutor(max_workers=max(1, cfg.workers)) as pool:
        futures = [pool.submit(mapper.map_chunk, c) for c in todo]
        for done, fut in enumerate(as_completed(futures), 1):
            res = fut.result()
            results.append(res)
            if progress:
                progress(done, len(todo), res)
    results.sort(key=lambda r: order[r.chunk.chunk_id])

    plan = plan_changes(results, existing, cfg, run_id)
    return RunReport(
        run_id=run_id,
        config=cfg,
        models=models.names(),
        catalogue={"tables": catalogue.tables, "columns": catalogue.columns, "hash": catalogue.hash, "prompt_version": prompts.PROMPT_VERSION},
        chunks_total=len(chunks),
        chunks_skipped=len(chunks) - len(todo),
        results=results,
        plan=plan,
        seconds=time.time() - started,
        independent_verification=models.independent_verifier,
    )
