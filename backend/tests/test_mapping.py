"""LLM policy mapping: catalogue, code-level checks, verification votes, append vs overwrite, the writer, and
the status-aware reads. No test calls a real model or database: models are scripted fakes and Neo4j is faked."""

import json
from pathlib import Path
from typing import Any, Callable

import pytest

from app import cypher
from app.chat import tools as chat_tools
from app.chat.llm import AzureChatLLM, LLMError, LLMNotConfigured, LLMReply
from app.config import Settings
from app.mapping import prompts, store
from app.mapping.__main__ import main as cli_main
from app.mapping.catalogue import build_catalogue
from app.mapping.engine import (
    Chunk, ChunkResult, Link, Mapper, Models, RunConfig, evidence_in_text, is_boilerplate, parse_links, plan_changes, run_mapping,
)
from app.mapping.models import load_models
from app.mapping.store import MappingWriter, chunks_from_rows
from app.snapshot import SnapshotRepository

DOB = "The national identity number and date of birth must additionally be protected by field-level encryption or tokenisation in the customer master."
PURPOSE = "This policy sets out how the bank protects the data it holds and who is responsible for doing so."
PII_TXT = "PII-Restricted data must be encrypted in transit and at rest, and masked in analytics and reports."

C_ENC = Chunk("c-enc", "pol-dp", "Customer Data Protection Policy", "4. Protection Controls > 4.3 Encryption", "4.3 Encryption", DOB)
C_SCOPE = Chunk("c-scope", "pol-dp", "Customer Data Protection Policy", "1. Purpose and Scope > 1.1 Purpose", "1.1 Purpose", PURPOSE)
C_PII = Chunk("c-pii", "pol-dp", "Customer Data Protection Policy", "3. Handling > 3.3 Handling by level", "3.3 Handling", PII_TXT)

DOB_COL = "banking.customers.date_of_birth"
NID_COL = "banking.customers.national_id"
EMAIL_COL = "banking.customers.email"
BIRTH_YEAR = "dim.customer.birth_year"


@pytest.fixture(scope="module")
def cat(demo_data):
    return build_catalogue(SnapshotRepository(*demo_data).catalogue())


# --- scripted models --------------------------------------------------------------------------------


def L(asset: str, evidence: str, confidence: str = "high", relation: str = "requires_control") -> dict[str, str]:
    return {"asset": asset, "relation": relation, "evidence": evidence, "reason": "because", "confidence": confidence}


class FakeLLM:
    """Answers the mapper schema with ``mapper(text)`` and the verifier schema with ``verdicts(text, assets)``."""

    def __init__(self, model: str, mapper: Callable[[str], list[dict]] | None = None, verdicts: Callable[[str, list[str]], dict[str, str]] | None = None):
        self.model, self._mapper, self._verdicts, self.calls = model, mapper, verdicts, 0

    def complete(self, messages, tools, json_schema=None):  # type: ignore[no-untyped-def]
        self.calls += 1
        user = messages[-1]["content"]
        if json_schema is prompts.MAP_SCHEMA:
            data: dict[str, Any] = {"links": self._mapper(user) if self._mapper else []}
        else:
            assets = [ln.split("asset:", 1)[1].strip() for ln in user.splitlines() if ln.startswith("- asset:")]
            data = {"verdicts": [{"asset": a, "verdict": v, "reason": "r"} for a, v in (self._verdicts(user, assets) if self._verdicts else {}).items()]}
        return LLMReply(content=json.dumps(data), total_tokens=10)


def mapper_script(table: dict[str, list[dict]]) -> Callable[[str], list[dict]]:
    return lambda user: next((links for needle, links in table.items() if needle in user), [])


ENC_LINKS = [
    L(DOB_COL, "date of birth must additionally be protected by field-level encryption"),
    L(NID_COL, "national identity number and date of birth", "medium"),
    L(BIRTH_YEAR, "protected by field-level encryption or tokenisation"),
    L(EMAIL_COL, "date of birth"),
    L("banking.nonexistent.col", "date of birth"),
    L("banking.customers.phone", "the customer's phone number"),
]


# --- catalogue --------------------------------------------------------------------------------------


def test_catalogue_is_metadata_only_and_copy_friendly(cat) -> None:
    assert cat.tables == 12 and cat.columns == 93
    assert cat.refs[DOB_COL].startswith("pg:bankdb") and cat.kinds["banking.customers"] == "Table" and cat.kinds[DOB_COL] == "Column"
    assert "represents business term: Customer" in cat.lines["banking.customers"]
    assert "flags: PII" in cat.lines[DOB_COL] and "PII: date of birth" in cat.lines[DOB_COL]
    assert sorted(cat.categories) == ["@PII", "@PII_MASKED"] and DOB_COL in cat.categories["@PII"] and BIRTH_YEAR in cat.categories["@PII_MASKED"]
    assert "embedding" not in cat.text.lower() and len(cat.hash) == 16


def test_catalogue_shows_lineage_once_per_transformation(cat) -> None:
    line = cat.lines["fact.loan_portfolio.risk_bucket"]
    assert line.count("CASE WHEN") == 1  # three sources share one expression
    assert "banking.loans.loan_status, banking.loans.outstanding_amount, banking.loans.principal_amount [DERIVED" in line
    assert "from banking.customers.date_of_birth [MASKED: EXTRACT(YEAR FROM date_of_birth)]" in cat.lines[BIRTH_YEAR]


def test_catalogue_hash_changes_with_the_metadata(demo_data) -> None:
    nodes, edges = demo_data
    a = build_catalogue(SnapshotRepository(nodes, edges).catalogue())
    changed = [{**n, "comment": "edited"} if n["id"].endswith("loans.loan_id") else n for n in nodes]
    b = build_catalogue(SnapshotRepository(changed, edges).catalogue())
    assert a.hash == build_catalogue(SnapshotRepository(nodes, edges).catalogue()).hash != b.hash


# --- checks done in code ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "evidence,ok",
    [
        ("date of birth must additionally be protected", True),
        ("  DATE of   birth must additionally be protected ", True),  # case and spacing
        ("national identity number … field-level encryption", True),  # an ellipsis splits it into pieces
        ("the customer's phone number", False),  # not in the text
        ("date of birth", True),
        ("PII", False),  # too short to prove anything
        ("", False),
        ("protected by encryption or tokenisation", False),  # paraphrase
    ],
)
def test_evidence_must_really_be_in_the_chunk(evidence, ok) -> None:
    assert evidence_in_text(evidence, DOB) is ok


def test_parse_links_rejects_invented_assets_and_quotes(cat) -> None:
    raw = {"links": ENC_LINKS + [L("@NOPE", "date of birth"), {**L(DOB_COL, "date of birth"), "relation": "bogus"}]}
    links, rejected = parse_links(raw, C_ENC, cat, max_links=15)
    assert [ln.ref for ln in links] == [DOB_COL, EMAIL_COL, BIRTH_YEAR, NID_COL]  # high first (by name), then medium
    assert {(r["ref"], r["reason"]) for r in rejected} == {
        ("banking.nonexistent.col", "unknown_asset"), ("banking.customers.phone", "evidence_not_in_text"),
        ("@NOPE", "unknown_category"), (DOB_COL, "invalid_relation"),
    }


def test_parse_links_dedupes_keeping_the_stronger_and_caps(cat) -> None:
    two = {"links": [L(DOB_COL, "date of birth", "medium"), L(DOB_COL, "date of birth", "high")]}
    links, _ = parse_links(two, C_ENC, cat, 15)
    assert len(links) == 1 and links[0].confidence == "high"
    many = {"links": [L(c, "date of birth", "high") for c in list(cat.categories["@PII"])[:5]]}
    kept, rejected = parse_links(many, C_ENC, cat, max_links=2)
    assert len(kept) == 2 and [r["reason"] for r in rejected] == ["over_link_cap"] * 3


# --- votes decide the status ------------------------------------------------------------------------


def verdicts(supported: set[str]) -> Callable[[str, list[str]], dict[str, str]]:
    return lambda user, assets: {a: ("supported" if a in supported else "unsupported") for a in assets}


def mapper_only(cat, **kw):  # noqa: ANN001
    return Mapper(cat, Models(FakeLLM("m1", mapper_script({"date of birth": ENC_LINKS}))), RunConfig(**kw))


def three_models(cat, verifier_supports, tiebreak_supports, **kw):  # noqa: ANN001
    models = Models(
        FakeLLM("m1", mapper_script({"date of birth": ENC_LINKS})),
        FakeLLM("m2", verdicts=verdicts(verifier_supports)),
        FakeLLM("m3", verdicts=verdicts(tiebreak_supports)),
    )
    return Mapper(cat, models, RunConfig(**kw)), models


def by_ref(res: ChunkResult) -> dict[str, Link]:
    return {ln.ref: ln for ln in res.links}


def test_three_models_vote_and_tiers(cat) -> None:
    mapper, models = three_models(cat, {DOB_COL, NID_COL}, {BIRTH_YEAR})
    res = mapper.map_chunk(C_ENC)
    got = by_ref(res)
    assert got[DOB_COL].status == "accepted" and got[DOB_COL].votes == {"mapper": "supported", "verifier": "supported"}
    assert got[NID_COL].status == "proposed" and "medium confidence" in got[NID_COL].note  # never auto-accepted
    assert got[BIRTH_YEAR].status == "proposed" and got[BIRTH_YEAR].votes["adjudicator"] == "supported" and "contested" in got[BIRTH_YEAR].note
    assert got[EMAIL_COL].status == "dropped" and got[EMAIL_COL].votes == {"mapper": "supported", "verifier": "unsupported", "adjudicator": "unsupported"}
    assert {r["reason"] for r in res.rejected} == {"unknown_asset", "evidence_not_in_text"}
    assert res.tokens["mapper"] == 10 and res.tokens["verifier"] == 10 and res.tokens["adjudicator"] == 10
    assert models.adjudicator.calls == 1  # asked once, only about the disputed links


def test_tie_breaker_is_not_called_when_nobody_disagrees(cat) -> None:
    mapper, models = three_models(cat, {DOB_COL, NID_COL, BIRTH_YEAR, EMAIL_COL}, set())
    mapper.map_chunk(C_ENC)
    assert models.verifier.calls == 1 and models.adjudicator.calls == 0


def test_without_a_verifier_nothing_is_auto_accepted(cat) -> None:
    res = mapper_only(cat).map_chunk(C_ENC)
    assert {ln.status for ln in res.links} == {"proposed"} and all("unverified" in ln.note for ln in res.links)


def test_disputed_links_are_dropped_when_there_is_no_tie_breaker(cat) -> None:
    models = Models(FakeLLM("m1", mapper_script({"date of birth": ENC_LINKS})), FakeLLM("m2", verdicts=verdicts({DOB_COL})))
    got = by_ref(Mapper(cat, models, RunConfig()).map_chunk(C_ENC))
    assert got[DOB_COL].status == "accepted" and got[BIRTH_YEAR].status == "dropped" and "no tie-breaker" in got[BIRTH_YEAR].note


def test_chunks_that_govern_nothing_return_nothing(cat) -> None:
    res = Mapper(cat, Models(FakeLLM("m1", mapper_script({}))), RunConfig()).map_chunk(C_SCOPE)
    assert res.links == [] and res.error is None


def test_category_link_expands_to_flagged_columns_and_direct_links_win(cat) -> None:
    script = {"encrypted in transit": [L("@pii", "must be encrypted in transit and at rest"), L(EMAIL_COL, "encrypted in transit and at rest", "medium")]}
    models = Models(FakeLLM("m1", mapper_script(script)), FakeLLM("m2", verdicts=lambda u, a: {x: "supported" for x in a}))
    got = by_ref(Mapper(cat, models, RunConfig()).map_chunk(C_PII))
    pii = set(cat.categories["@PII"])
    assert set(got) == pii  # the 7 flagged columns, no duplicate for email
    assert got[EMAIL_COL].basis == "direct" and got[EMAIL_COL].status == "proposed"  # the direct (medium) link kept
    child = got[DOB_COL]
    assert child.basis == "category" and child.category == "@PII" and child.status == "accepted" and child.asset_id == cat.refs[DOB_COL]


@pytest.mark.parametrize(
    "section,expected",
    [
        ("1. Purpose and Scope > 1.2 Scope", True),
        ("1. Purpose and Scope > 1.3 In-scope data assets", True),
        ("5. Roles and responsibilities > 5.1 Data owners", True),
        ("6. Review > 6.1 Review cycle", True),
        ("(Preamble)", True),
        ("4. Protection Controls > 4.3 Encryption", False),
        ("3. Data Classification and Handling > 3.3 Handling requirements by level", False),
        ("2. Protection Principles > 2.2 Accuracy and storage limitation", False),
    ],
)
def test_boilerplate_sections_are_recognised(section, expected) -> None:
    assert is_boilerplate(Chunk("c", "d", "T", section, section.split(">")[-1].strip(), "x")) is expected


def test_category_link_from_a_scope_section_is_held_for_review_even_when_verified(cat) -> None:
    scope = Chunk("c-bp", "pol-dp", "Customer Data Protection Policy", "1. Purpose and Scope > 1.2 Scope", "1.2 Scope",
                  "This policy applies to personal data of individual customers and must be followed everywhere.")
    script = {"applies to personal data": [L("@PII", "applies to personal data of individual customers", "high", "defines")]}
    models = Models(FakeLLM("m1", mapper_script(script)), FakeLLM("m2", verdicts=lambda u, a: {x: "supported" for x in a}))
    res = Mapper(cat, models, RunConfig()).map_chunk(scope)
    assert len(res.links) == 7 and {ln.status for ln in res.links} == {"proposed"}
    assert all("policy-wide statement" in ln.note for ln in res.links)
    # The same wording in a rule section is accepted.
    rule = Chunk("c-pii", "pol-dp", "Customer Data Protection Policy", "2. Protection Principles > 2.2 Accuracy", "2.2 Accuracy",
                 "Personal data must be kept accurate and up to date.")
    script = {"must be kept accurate": [L("@PII", "must be kept accurate and up to date", "high", "quality_rule")]}
    models = Models(FakeLLM("m1", mapper_script(script)), FakeLLM("m2", verdicts=lambda u, a: {x: "supported" for x in a}))
    assert {ln.status for ln in Mapper(cat, models, RunConfig()).map_chunk(rule).links} == {"accepted"}


def test_default_cap_is_high_enough_for_a_classification_table() -> None:
    assert RunConfig().max_links == 40


def test_dropped_category_is_not_expanded(cat) -> None:
    models = Models(
        FakeLLM("m1", mapper_script({"encrypted in transit": [L("@PII", "must be encrypted in transit and at rest")]})),
        FakeLLM("m2", verdicts=verdicts(set())),
    )
    res = Mapper(cat, models, RunConfig()).map_chunk(C_PII)
    assert [(ln.ref, ln.status) for ln in res.links] == [("@PII", "dropped")]


# --- append vs overwrite ----------------------------------------------------------------------------


def link(chunk_id: str, ref: str, status: str = "accepted", **kw: Any) -> Link:
    return Link(chunk_id, ref, f"id:{ref}", "defines", "date of birth", "r", "high", status=status, **kw)


def result(chunk: Chunk, *links: Link, error: str | None = None) -> ChunkResult:
    return ChunkResult(chunk, list(links), error=error)


def test_append_only_adds_pairs_that_have_no_live_link() -> None:
    existing = {("c-enc", "id:old"): {"accepted"}, ("c-enc", "id:pending"): {"proposed"}, ("c-enc", "id:no"): {"rejected"}, ("c-enc", "id:gone"): {"superseded"}}
    res = result(C_ENC, link("c-enc", "old"), link("c-enc", "pending"), link("c-enc", "no"), link("c-enc", "gone"), link("c-enc", "new"), link("c-enc", "x", "dropped"))
    plan = plan_changes([res], existing, RunConfig(mode="append"), "r1")
    assert [ln.ref for ln in plan.create] == ["gone", "new"]  # a superseded pair may come back; a dropped link never
    assert plan.supersede == []  # append never retires anything
    assert {(s["ref"], s["reason"]) for s in plan.skipped} == {("old", "already_mapped"), ("pending", "already_mapped"), ("no", "previously_rejected")}


def test_overwrite_supersedes_live_links_of_processed_chunks_only() -> None:
    existing = {
        ("c-enc", "id:old"): {"accepted"}, ("c-enc", "id:pending"): {"proposed"}, ("c-enc", "id:no"): {"rejected"},
        ("c-scope", "id:keep"): {"accepted"},  # this chunk's model call failed
        ("c-pii", "id:also"): {"accepted"},  # this chunk is not part of the run at all
    }
    results = [result(C_ENC, link("c-enc", "old"), link("c-enc", "no"), link("c-enc", "new")), result(C_SCOPE, error="boom")]
    plan = plan_changes(results, existing, RunConfig(mode="overwrite"), "r1")
    assert sorted(ln.ref for ln in plan.create) == ["new", "old"]  # replaced even though it already existed
    assert plan.supersede == [("c-enc", "id:old"), ("c-enc", "id:pending")]  # the rejected one stays rejected
    assert [s["reason"] for s in plan.skipped] == ["previously_rejected"]


def test_overwrite_reports_chunks_it_would_leave_empty() -> None:
    existing = {("c-enc", "id:old"): {"accepted"}}
    plan = plan_changes([result(C_ENC), result(C_SCOPE)], existing, RunConfig(mode="overwrite"), "r1")
    assert plan.supersede == [("c-enc", "id:old")] and plan.orphaned_chunks == ["c-enc"]


def test_auto_accept_off_makes_everything_proposed() -> None:
    plan = plan_changes([result(C_ENC, link("c-enc", "a"), link("c-enc", "b", "proposed"))], {}, RunConfig(auto_accept=False), "r1")
    assert [ln.status for ln in plan.create] == ["proposed", "proposed"] and "auto-accept off" in plan.create[0].note


# --- running it -------------------------------------------------------------------------------------


def full_models() -> Models:
    return Models(
        FakeLLM("m1", mapper_script({"date of birth": ENC_LINKS[:3]})),
        FakeLLM("m2", verdicts=lambda u, a: {x: "supported" for x in a}),
        FakeLLM("m3", verdicts=verdicts(set())),
    )


def test_run_mapping_end_to_end_append(cat) -> None:
    existing = {("c-enc", cat.refs[DOB_COL]): {"accepted"}}  # already done by the older mapping
    seen = []
    report = run_mapping([C_ENC, C_SCOPE, C_PII], cat, full_models(), existing, RunConfig(workers=3), lambda d, t, r: seen.append((d, t)))
    s = report.summary()
    assert [r.chunk.chunk_id for r in report.results] == ["c-enc", "c-scope", "c-pii"]  # input order, not completion order
    assert sorted(seen) == [(1, 3), (2, 3), (3, 3)]
    assert s["chunks_processed"] == 3 and s["chunks_with_no_links"] == 2 and s["skipped_existing"] == 1
    assert [ln.ref for ln in report.plan.create] == [BIRTH_YEAR, NID_COL] and s["to_supersede"] == 0
    assert s["to_create_accepted"] == 1 and s["to_create_proposed"] == 1  # birth_year high+verified; national_id medium
    assert {"mapper", "verifier"} <= set(s["tokens"]) and s["models"] == {"mapper": "m1", "verifier": "m2", "adjudicator": "m3"}
    assert all(r["will_write"] == ((r["chunk_id"], r["asset"]) in {(ln.chunk_id, ln.ref) for ln in report.plan.create}) for r in report.rows())


def test_only_unlinked_skips_chunks_that_already_have_a_link(cat) -> None:
    models = full_models()
    existing = {("c-enc", cat.refs[DOB_COL]): {"accepted"}}
    report = run_mapping([C_ENC, C_SCOPE], cat, models, existing, RunConfig(only_unlinked=True))
    assert report.chunks_skipped == 1 and [r.chunk.chunk_id for r in report.results] == ["c-scope"] and models.mapper.calls == 1


def test_cache_makes_a_rerun_free_and_is_invalidated_by_new_inputs(cat, tmp_path: Path) -> None:
    cfg = RunConfig(cache_dir=tmp_path)
    models = full_models()
    first = run_mapping([C_ENC], cat, models, {}, cfg)
    calls = models.mapper.calls
    again = run_mapping([C_ENC], cat, models, {}, cfg)
    assert models.mapper.calls == calls and again.results[0].cached and not first.results[0].cached
    assert [ln.ref for ln in again.results[0].links] == [ln.ref for ln in first.results[0].links]
    other = Chunk("c-enc", "pol-dp", "Customer Data Protection Policy", "4.3", "4.3", DOB + " Keys are rotated yearly.")
    run_mapping([other], cat, models, {}, cfg)
    assert models.mapper.calls == calls + 1  # changed text -> asked again


def test_a_failed_chunk_is_reported_not_cached_and_does_not_stop_the_run(cat, tmp_path: Path) -> None:
    class Flaky(FakeLLM):
        def complete(self, messages, tools, json_schema=None):  # type: ignore[no-untyped-def]
            if "Purpose" in messages[-1]["content"]:
                raise LLMError("The language model returned HTTP 429.")
            return super().complete(messages, tools, json_schema)

    models = Models(Flaky("m1", mapper_script({"date of birth": ENC_LINKS[:1]})))
    report = run_mapping([C_SCOPE, C_ENC], cat, models, {}, RunConfig(cache_dir=tmp_path))
    bad, good = report.results
    assert bad.error and "429" in bad.error and not good.error and len(good.links) == 1
    assert report.summary()["chunks_failed"] == 1 and report.to_dict()["errors"][0]["chunk_id"] == "c-scope"
    assert len(list(tmp_path.glob("*.json"))) == 1  # only the good chunk was cached


def test_unusable_json_is_retried_once_then_reported(cat) -> None:
    class Junk:
        model, calls = "m1", 0

        def complete(self, messages, tools, json_schema=None):  # type: ignore[no-untyped-def]
            self.calls += 1
            return LLMReply(content="not json at all", total_tokens=1)

    junk = Junk()
    res = Mapper(cat, Models(junk), RunConfig()).map_chunk(C_ENC)  # type: ignore[arg-type]
    assert junk.calls == 2 and res.error and "unusable JSON" in res.error


def test_report_dict_is_json_serialisable(cat) -> None:
    report = run_mapping([C_ENC], cat, full_models(), {}, RunConfig())
    data = json.loads(json.dumps(report.to_dict()))
    assert {"summary", "links", "rejected_by_code", "skipped", "errors", "supersede"} <= set(data)


# --- chunks from a Fabric table ---------------------------------------------------------------------


def test_chunks_from_rows_maps_columns_and_skips_empty_text() -> None:
    rows = [{"id": "a", "body": "Some text.", "title": "T", "doc": "d", "path": "1 > 2", "head": "2"}, {"id": "b", "body": "  "}]
    got = chunks_from_rows(rows, {"chunk_id": "id", "text": "body", "doc_title": "title", "doc_id": "doc", "section_path": "path", "heading": "head"})
    assert got == [Chunk("a", "d", "T", "1 > 2", "2", "Some text.")]
    with pytest.raises(KeyError, match="no column"):
        chunks_from_rows([{"chunk_id": "a"}])
    assert chunks_from_rows([{"chunk_id": "a", "text": "x y z", "doc_id": None}])[0].doc_id == ""


# --- the writer (fake driver) -----------------------------------------------------------------------


class FakeTx:
    def __init__(self) -> None:
        self.runs: list[tuple[str, dict[str, Any]]] = []

    def run(self, query: str, **params: Any):  # type: ignore[no-untyped-def]
        self.runs.append((query, params))
        n = len(params.get("pairs", params.get("rows", [None] * 3)))
        return type("R", (), {"single": lambda self: {"n": n}})()


class FakeDriver:
    def __init__(self) -> None:
        self.tx, self.modes, self.closed = FakeTx(), [], False

    def session(self, database: str, default_access_mode: Any):  # type: ignore[no-untyped-def]
        self.modes.append((database, default_access_mode))
        driver = self

        class S:
            def __enter__(s): return s  # noqa: N805
            def __exit__(s, *a): return False  # noqa: N805
            def execute_write(s, fn): return fn(driver.tx)  # noqa: N805

        return S()

    def close(self) -> None:
        self.closed = True


@pytest.fixture()
def writer(monkeypatch):  # type: ignore[no-untyped-def]
    driver = FakeDriver()
    monkeypatch.setattr(store.GraphDatabase, "driver", lambda *a, **k: driver)
    w = MappingWriter(Settings(mode="snapshot", neo4j_uri="neo4j://x", neo4j_database="db"))
    return w, driver


def test_writer_creates_links_with_full_provenance_in_one_transaction(writer) -> None:
    w, driver = writer
    plan = plan_changes([result(C_ENC, link("c-enc", "a", votes={"mapper": "supported", "verifier": "supported"}), link("c-enc", "b", "proposed", basis="category", category="@PII"))], {}, RunConfig(), "run-1")
    out = w.apply(plan, {"mapper": "m1", "verifier": "m2", "adjudicator": None}, {"c-enc": "pol-dp"}, "hash1")
    assert out == {"created": 2, "superseded": 0}
    queries = [q for q, _ in driver.tx.runs]
    assert queries == [store.CREATE_LINKS, store.CREATE_RUN]  # append never supersedes
    row_a, row_b = driver.tx.runs[0][1]["rows"]
    assert row_a["asset_id"] == "id:a" and row_a["props"]["status"] == "accepted" and row_a["props"]["doc_id"] == "pol-dp"
    assert row_a["props"]["source"] == "llm_mapping" and row_a["props"]["method"] == "llm-map" and row_a["props"]["run_id"] == "run-1"
    assert json.loads(row_a["props"]["votes"]) == {"mapper": "supported", "verifier": "supported"} and row_a["props"]["prompt_version"] == prompts.PROMPT_VERSION
    assert row_b["props"]["method"] == "llm-category" and row_b["props"]["category"] == "@PII" and row_b["props"]["status"] == "proposed"
    assert all(v is not None and not isinstance(v, (dict, list)) for v in row_a["props"].values())  # Neo4j properties are primitives
    assert driver.modes[0][0] == "db" and "WRITE" in str(driver.modes[0][1]).upper()


def test_writer_overwrite_supersedes_before_creating_and_never_deletes(writer) -> None:
    w, driver = writer
    existing = {("c-enc", "id:old"): {"accepted"}}
    plan = plan_changes([result(C_ENC, link("c-enc", "new"))], existing, RunConfig(mode="overwrite"), "run-2")
    assert w.apply(plan, {"mapper": "m1"}, {"c-enc": "pol-dp"}) == {"created": 1, "superseded": 1}
    assert [q for q, _ in driver.tx.runs] == [store.SUPERSEDE, store.CREATE_LINKS, store.CREATE_RUN]
    assert driver.tx.runs[0][1]["pairs"] == [{"chunk_id": "c-enc", "asset_id": "id:old"}] and driver.tx.runs[0][1]["run_id"] == "run-2"
    for q, _ in driver.tx.runs:
        assert "DELETE" not in q.upper()  # superseding and undoing only ever change a status


def test_writer_restore_and_review(writer) -> None:
    w, driver = writer
    assert w.restore("run-3") == {"retracted": 3, "restored": 3}
    assert [q for q, _ in driver.tx.runs] == [store.RETRACT_RUN, store.RESTORE_SUPERSEDED]
    driver.tx.runs.clear()
    assert w.review("run-3", "accepted", "high") == 3
    assert driver.tx.runs[0][1] == {"run_id": "run-3", "status": "accepted", "levels": ["high"]}
    w.review("run-3", "rejected")
    assert driver.tx.runs[1][1]["levels"] == ["high", "medium"]
    with pytest.raises(ValueError):
        w.review("run-3", "deleted")


def test_supersede_and_restore_cypher_keep_the_old_status() -> None:
    assert "g.prior_status = coalesce(g.status, 'accepted')" in store.SUPERSEDE
    assert "g.status = coalesce(g.prior_status, 'accepted')" in store.RESTORE_SUPERSEDED
    assert "CREATE (c)-[g:GOVERNS]->(a)" in store.CREATE_LINKS and "MERGE" not in store.CREATE_LINKS


# --- the rest of the app only sees accepted links ---------------------------------------------------


def test_only_accepted_links_are_read_by_the_app(demo_data) -> None:
    nodes, edges = demo_data
    nodes = [*nodes, {"id": "doc", "label": "PolicyDocument", "doc_id": "doc", "title": "P"}] + [
        {"id": f"ch{i}", "label": "PolicyChunk", "chunk_id": f"ch{i}", "heading": f"s{i}"} for i in range(5)
    ]
    loans = "pg:bankdb.banking.loans"
    statuses = [None, "accepted", "proposed", "rejected", "superseded"]  # None = a link from before statuses existed
    edges = [*edges, *({"from": "doc", "rel": "HAS_CHUNK", "to": f"ch{i}"} for i in range(5))] + [
        {"from": f"ch{i}", "rel": "GOVERNS", "to": loans, **({"status": s} if s else {})} for i, s in enumerate(statuses)
    ]
    repo = SnapshotRepository(nodes, edges)
    sub = repo.governing_policies([loans])
    assert sorted(e.source for e in sub.edges_of("GOVERNS")) == ["ch0", "ch1"]  # no status, or accepted


def test_every_policy_query_filters_on_status() -> None:
    active = "coalesce(g.status, 'accepted') = 'accepted'"
    assert active in cypher.GOVERNING_POLICIES and active in cypher.GOVERNS_ACTIVE
    assert active in cypher.lineage("PolicyDocument", "both", 2)
    assert active in chat_tools.POLICIES_FOR_ASSET and active in chat_tools.POLICY_SECTION


# --- models and config ------------------------------------------------------------------------------


def cfg(**kw: str) -> Settings:
    base = dict(mode="snapshot", AZURE_OPEN_AI_KEY="k1", AZURE_OPEN_AI_ENDPOINT="https://one.openai.azure.com", AZURE_OPEN_AI_MODEL_NAME="m1",
                AZURE_OPEN_AI_VERSION="v1", AZURE_OPEN_AI_MODEL_NAME_2="", AZURE_OPEN_AI_MODEL_NAME_3="", AZURE_OPEN_AI_KEY_2="", AZURE_OPEN_AI_KEY_3="",
                AZURE_OPEN_AI_ENDPOINT_2="", AZURE_OPEN_AI_ENDPOINT_3="", AZURE_OPEN_AI_VERSION_2="", AZURE_OPEN_AI_VERSION_3="")
    return Settings(**{**base, **kw})


def test_extra_models_fall_back_to_the_first_models_connection() -> None:
    s = cfg(AZURE_OPEN_AI_MODEL_NAME_2="m2", AZURE_OPEN_AI_MODEL_NAME_3="m3", AZURE_OPEN_AI_ENDPOINT_3="https://three.openai.azure.com", AZURE_OPEN_AI_KEY_3="k3")
    assert s.llm_slot(2) == {"endpoint": "https://one.openai.azure.com", "key": "k1", "model": "m2", "version": "v1"}
    assert s.llm_slot(3) == {"endpoint": "https://three.openai.azure.com", "key": "k3", "model": "m3", "version": "v1"}
    assert cfg().llm_slot(2)["model"] == ""  # not configured
    assert AzureChatLLM(s, 2).model == "m2" and AzureChatLLM(s, 3).slot == 3


def test_an_unconfigured_slot_names_what_is_missing() -> None:
    with pytest.raises(LLMNotConfigured, match="AZURE_OPEN_AI_MODEL_NAME_2"):
        AzureChatLLM(cfg(), 2)


def test_one_model_plays_every_role_and_extras_take_over_only_when_configured() -> None:
    single = load_models(cfg())
    assert single.names() == {"mapper": "m1", "verifier": "m1", "adjudicator": "m1"}
    assert single.verifier is single.mapper and single.adjudicator is single.mapper  # literally the same model
    assert single.independent_verifier is False
    v = load_models(cfg(AZURE_OPEN_AI_MODEL_NAME_2="m2"))  # e.g. added because model 1's token limit bites
    assert v.names() == {"mapper": "m1", "verifier": "m2", "adjudicator": "m1"} and v.independent_verifier is True
    t = load_models(cfg(AZURE_OPEN_AI_MODEL_NAME_3="m3"))
    assert t.names() == {"mapper": "m1", "verifier": "m1", "adjudicator": "m3"} and t.independent_verifier is False
    full = load_models(cfg(AZURE_OPEN_AI_MODEL_NAME_2="m2", AZURE_OPEN_AI_MODEL_NAME_3="m3"))
    assert full.names() == {"mapper": "m1", "verifier": "m2", "adjudicator": "m3"} and full.independent_verifier is True


def test_a_second_slot_pointing_at_the_same_deployment_is_not_independent() -> None:
    same = load_models(cfg(AZURE_OPEN_AI_MODEL_NAME_2="m1"))  # same endpoint (inherited) and deployment name
    assert same.verifier is not same.mapper and same.independent_verifier is False
    elsewhere = load_models(cfg(AZURE_OPEN_AI_MODEL_NAME_2="m1", AZURE_OPEN_AI_ENDPOINT_2="https://other.openai.azure.com"))
    assert elsewhere.independent_verifier is True  # same deployment name on a different resource is a different model


def one_model(script=None, supports=None):  # noqa: ANN001
    """One FakeLLM object as mapper, verifier and tie-breaker, like load_models gives when only model 1 is set."""
    llm = FakeLLM("m1", mapper_script({"date of birth": script or ENC_LINKS}), verdicts(supports) if supports is not None else (lambda u, a: {x: "supported" for x in a}))
    return llm, Models(llm, llm, llm)


def test_a_model_verifying_itself_never_auto_accepts(cat) -> None:
    _, models = one_model()
    got = by_ref(Mapper(cat, models, RunConfig()).map_chunk(C_ENC))
    assert got[DOB_COL].status == "proposed" and "same model that proposed it" in got[DOB_COL].note
    assert {ln.status for ln in got.values()} == {"proposed"}  # nothing accepted on its own say-so


def test_self_verification_still_filters_what_it_finds_unsupported(cat) -> None:
    llm, models = one_model(supports={DOB_COL})
    got = by_ref(Mapper(cat, models, RunConfig()).map_chunk(C_ENC))
    assert got[DOB_COL].status == "proposed" and got[BIRTH_YEAR].status == "dropped" and got[EMAIL_COL].status == "dropped"
    assert got[BIRTH_YEAR].votes == {"mapper": "supported", "verifier": "unsupported", "adjudicator": "unsupported"}


def test_self_verified_links_can_be_trusted_explicitly(cat) -> None:
    _, models = one_model()
    got = by_ref(Mapper(cat, models, RunConfig(accept_self_verified=True)).map_chunk(C_ENC))
    assert got[DOB_COL].status == "accepted" and got[BIRTH_YEAR].status == "accepted"
    assert got[NID_COL].status == "proposed" and "medium confidence" in got[NID_COL].note  # medium is never accepted


def test_trust_setting_is_part_of_the_cache_key(cat, tmp_path: Path) -> None:
    llm, models = one_model()
    run_mapping([C_ENC], cat, models, {}, RunConfig(cache_dir=tmp_path))
    calls = llm.calls
    again = run_mapping([C_ENC], cat, models, {}, RunConfig(cache_dir=tmp_path, accept_self_verified=True))
    assert llm.calls > calls and not again.results[0].cached  # a cached "proposed" must not stand in for "accepted"
    assert by_ref(again.results[0])[DOB_COL].status == "accepted"


def test_report_says_whether_verification_was_independent(cat) -> None:
    _, models = one_model()
    assert run_mapping([C_ENC], cat, models, {}, RunConfig()).summary()["independent_verification"] is False
    assert run_mapping([C_ENC], cat, full_models(), {}, RunConfig()).summary()["independent_verification"] is True


# --- rate limits (a single model doing every role makes about three times the calls) ----------------


class Limited(FakeLLM):
    """Raises HTTP 429 for the first ``failures`` calls, then behaves normally."""

    def __init__(self, failures: int, retry_after: float | None = None, status: int = 429, **kw: Any):
        super().__init__("m1", mapper_script({"date of birth": ENC_LINKS[:1]}), **kw)
        self.failures, self.retry_after, self.status, self.attempts = failures, retry_after, status, 0

    def complete(self, messages, tools, json_schema=None):  # type: ignore[no-untyped-def]
        self.attempts += 1
        if self.attempts <= self.failures:
            raise LLMError(f"The language model returned HTTP {self.status}.", status_code=self.status, retry_after=self.retry_after)
        return super().complete(messages, tools, json_schema)


@pytest.fixture()
def waits(monkeypatch):  # type: ignore[no-untyped-def]
    from app.mapping import engine

    recorded: list[float] = []
    monkeypatch.setattr(engine, "_sleep", recorded.append)
    return recorded


def test_rate_limit_waits_and_retries(cat, waits) -> None:
    llm = Limited(failures=2)
    res = Mapper(cat, Models(llm), RunConfig()).map_chunk(C_ENC)
    assert res.error is None and len(res.links) == 1 and llm.attempts == 3
    assert waits == [5.0, 15.0]


def test_rate_limit_honours_retry_after(cat, waits) -> None:
    Mapper(cat, Models(Limited(failures=1, retry_after=2.0)), RunConfig()).map_chunk(C_ENC)
    assert waits == [2.0]
    waits.clear()
    Mapper(cat, Models(Limited(failures=1, retry_after=9999.0)), RunConfig()).map_chunk(C_ENC)
    assert waits == [120.0]  # never an absurd wait


def test_rate_limit_gives_up_after_the_last_wait_and_reports_the_chunk(cat, waits, tmp_path: Path) -> None:
    llm = Limited(failures=99)
    res = Mapper(cat, Models(llm), RunConfig(cache_dir=tmp_path)).map_chunk(C_ENC)
    assert res.error and "429" in res.error and waits == [5.0, 15.0, 45.0] and llm.attempts == 4
    assert not list(tmp_path.glob("*.json"))  # a failure is never cached


def test_only_rate_limits_are_waited_out(cat, waits) -> None:
    for status in (400, 500, 503):
        llm = Limited(failures=99, status=status)
        res = Mapper(cat, Models(llm), RunConfig()).map_chunk(C_ENC)
        assert res.error and llm.attempts == 1
    assert waits == []


def test_llm_error_carries_the_status_and_retry_after(monkeypatch) -> None:
    import httpx
    from openai import APIStatusError

    llm = AzureChatLLM(cfg())
    resp = httpx.Response(429, headers={"retry-after": "7"}, request=httpx.Request("POST", "https://x/y"))
    monkeypatch.setattr(llm._client.chat.completions, "create", lambda **_: (_ for _ in ()).throw(APIStatusError("slow down", response=resp, body=None)))
    with pytest.raises(LLMError) as err:
        llm.complete([{"role": "user", "content": "x"}], None)
    assert err.value.status_code == 429 and err.value.retry_after == 7.0 and "HTTP 429" in str(err.value)
    plain = LLMError("boom")
    assert plain.status_code is None and plain.retry_after is None  # old call sites keep working


def test_the_chat_model_is_unchanged_by_the_extra_slots() -> None:
    assert AzureChatLLM(cfg(AZURE_OPEN_AI_MODEL_NAME_2="m2")).model == "m1"


# --- command line -----------------------------------------------------------------------------------


def test_cli_refuses_to_overwrite_without_confirmation(capsys) -> None:
    assert cli_main(["run", "--mode", "overwrite", "--write"]) == 2
    assert "--confirm-overwrite" in capsys.readouterr().err


def test_cli_dry_run_is_the_default_and_review_needs_a_decision() -> None:
    with pytest.raises(SystemExit):
        cli_main(["review", "--run", "x"])  # neither --accept nor --reject
    with pytest.raises(SystemExit):
        cli_main(["run", "--mode", "merge"])
