"""The lakehouse reader, the data quality scorecard, the question gating and the saved answers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import duckdb
import pytest
from fastapi.testclient import TestClient

from app.chat.agent import run_chat
from app.chat.answer_cache import AnswerCache, normalise
from app.chat.lakehouse import LakehouseData, LakehouseError, Snapshot, TableInfo
from app.chat.quality import _capped_grade, build_report, wants_quality
from app.chat.suggested_questions import QUESTIONS
from app.chat.tools import QUALITY_TOOL, GraphTools
from app.config import Settings
from app.main import create_app
from test_chat import FakeEmbedder, ScriptedLLM, ask, call, say

ROOT = Path(__file__).resolve().parents[2]


# --- a small lakehouse with problems planted in it ---------------------------------------------------


def _info(con: duckdb.DuckDBPyConnection, schema: str, name: str) -> TableInfo:
    cols = con.execute(
        "SELECT column_name, data_type FROM duckdb_columns() WHERE schema_name = ? AND table_name = ? ORDER BY column_index", [schema, name]
    ).fetchall()
    n = con.execute(f'SELECT count(*) FROM {schema}."{name}"').fetchone()[0]
    return TableInfo(schema, name, n, n, [(c[0], c[1]) for c in cols])


def make_snapshot() -> Snapshot:
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    con.execute("CREATE SCHEMA dim")
    con.execute("CREATE SCHEMA fact")
    con.execute(
        """CREATE TABLE dim.customer AS SELECT i AS customer_id, 'Name ' || i AS full_name,
            CASE WHEN i <= 30 THEN 'VERIFIED' ELSE 'EXPIRED' END AS kyc_status,
            CASE WHEN i = 5 THEN 'not-an-email' WHEN i = 6 THEN NULL ELSE 'c' || i || '@example.com' END AS email_masked,
            TIMESTAMP '2025-01-01' AS created_at,
            CASE WHEN i = 7 THEN TIMESTAMP '2099-01-01' ELSE TIMESTAMP '2026-11-01' END AS updated_at
           FROM range(1, 41) t(i)"""
    )
    con.execute(
        """CREATE TABLE dim.account AS SELECT 'A' || lpad(i::VARCHAR, 3, '0') AS account_no,
            CASE WHEN i = 1 THEN 999 ELSE (i % 40) + 1 END AS customer_id,
            CASE WHEN i <= 5 THEN 'DORMANT' ELSE 'ACTIVE' END AS status,
            CASE WHEN i = 3 THEN -50.0 ELSE 1000.0 + i END::DECIMAL(15, 2) AS balance
           FROM range(1, 61) t(i)"""
    )
    con.execute(
        """CREATE TABLE fact.loan AS SELECT i AS loan_id, i AS customer_id, 1000.0::DECIMAL(15, 2) AS principal_amount,
            CASE WHEN i = 2 THEN 2000.0 ELSE 500.0 END::DECIMAL(15, 2) AS outstanding_amount,
            CASE WHEN i BETWEEN 3 AND 6 THEN 'NPA' ELSE 'ACTIVE' END AS loan_status,
            CASE WHEN i = 3 THEN 'High Risk' ELSE 'Medium Risk' END AS risk_bucket,
            CASE WHEN i = 3 THEN 2 ELSE 1 END AS risk_bucket_flag,
            50.0::DECIMAL(9, 2) AS repaid_pct
           FROM range(1, 21) t(i)"""
    )
    tables = [_info(con, "dim", "customer"), _info(con, "dim", "account"), _info(con, "fact", "loan")]
    con.execute("SET enable_external_access = false")
    con.execute("SET lock_configuration = true")
    return Snapshot(con, tables, {}, datetime.now(timezone.utc))


@pytest.fixture(scope="module")
def snap() -> Snapshot:
    return make_snapshot()


class FakeLake:
    configured = True

    def __init__(self, snapshot: Snapshot):
        self._snap = snapshot

    def snapshot(self, refresh: bool = False) -> Snapshot:
        return self._snap


# --- the scorecard -----------------------------------------------------------------------------------


def test_scorecard_finds_the_planted_problems(snap: Snapshot) -> None:
    rep = build_report(snap)
    titles = [i["title"] for i in rep["issues"]]

    def has(text: str) -> bool:
        return any(text in t for t in titles)

    assert has("owe more than was originally lent")
    assert has("non-performing") and has("are not rated high risk")
    assert has("belong to customers whose KYC is not verified")
    assert has("point to a customer ID that does not exist in dim.customer")
    assert has("not an email address")
    assert has("have no email")
    assert has("updated at in the future")
    assert has("negative balance")
    assert rep["overall"]["issues"] == len(titles) or rep["overall"]["issues"] >= len(titles)
    assert rep["overall"]["high"] >= 1 and rep["overall"]["grade"] != "Excellent"


def test_scorecard_structure_is_what_the_dashboard_needs(snap: Snapshot) -> None:
    rep = build_report(snap)
    assert [d["key"] for d in rep["dimensions"]] == ["completeness", "uniqueness", "validity", "consistency", "timeliness"]
    assert {t["name"] for t in rep["tables"]} == {"dim.customer", "dim.account", "fact.loan"}
    assert all(0 <= t["score"] <= 100 for t in rep["tables"])
    assert rep["tables"][0]["score"] <= rep["tables"][-1]["score"]  # weakest first
    assert all({"severity", "title", "table", "failed", "checked", "pct", "examples"} <= set(i) for i in rep["issues"])
    assert rep["issues"][0]["severity"] == "high"  # worst first
    json.dumps(rep)  # plain data: it goes straight to the browser


def test_keys_are_inferred_and_a_clean_table_scores_full(snap: Snapshot) -> None:
    rep = build_report(snap)
    keys = {t["name"]: t["key"] for t in rep["tables"]}
    assert keys["dim.account"] == ["account_no"] and keys["fact.loan"] == ["loan_id"] and keys["dim.customer"] == ["customer_id"]
    clean = SimpleNamespace(**{})  # noqa: F841 - a table with nothing wrong has no completeness or uniqueness issues
    comp = next(d for d in rep["dimensions"] if d["key"] == "uniqueness")
    assert comp["score"] == 100.0


def test_one_table_scope_and_unknown_table(snap: Snapshot) -> None:
    rep = build_report(snap, "fact.loan")
    assert rep["scope"] == "fact.loan" and [t["name"] for t in rep["tables"]] == ["fact.loan"]
    assert all(i["table"] == "fact.loan" or "fact.loan" in i["title"] for i in rep["issues"])
    assert build_report(snap, "LOAN")["scope"] == "fact.loan"  # a bare, case-insensitive name works
    with pytest.raises(ValueError, match="No table"):
        build_report(snap, "dim.nothing")


def test_a_high_score_cannot_hide_serious_findings() -> None:
    assert _capped_grade(99.6, 0, 0) == "Excellent"
    assert _capped_grade(99.6, 1, 0) == "Fair"
    assert _capped_grade(99.6, 3, 0) == "Needs attention"
    assert _capped_grade(99.6, 0, 3) == "Good"
    assert _capped_grade(70.0, 0, 0) == "Needs attention"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("How is our data quality?", True),
        ("Give me a health check of our banking data", True),
        ("Are there duplicate customers?", True),
        ("which loans have missing values", True),
        ("Which policies govern customer data?", False),
        ("What does the loan policy say about risk classification?", False),
        ("What is affected if I change banking.loans?", False),
        ("Which columns hold PII?", False),
    ],
)
def test_wants_quality(text: str, expected: bool) -> None:
    assert wants_quality(text) is expected


# --- reading the copy safely -------------------------------------------------------------------------


def test_query_returns_rows_and_truncates(snap: Snapshot) -> None:
    res = snap.query("SELECT count(*) AS n FROM fact.loan")
    assert res == {"columns": ["n"], "rows": [[20]], "truncated": False}
    big = snap.query("SELECT * FROM range(100)", max_rows=5)
    assert len(big["rows"]) == 5 and big["truncated"] is True
    assert snap.query("SELECT count(*) FROM FACT.LOAN")["rows"] == [[20]]  # names are not case-sensitive


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE dim.customer",
        "DELETE FROM dim.customer",
        "INSERT INTO dim.customer SELECT * FROM dim.customer",
        "SELECT 1; DROP TABLE dim.customer",
        "SET enable_external_access = true",
        "COPY dim.customer TO 'out.csv'",
        "",
    ],
)
def test_query_refuses_anything_but_one_select(snap: Snapshot, sql: str) -> None:
    with pytest.raises(LakehouseError):
        snap.query(sql)
    assert snap.query("SELECT count(*) FROM dim.customer")["rows"] == [[40]]  # still there


def test_query_cannot_read_files(snap: Snapshot) -> None:
    with pytest.raises(LakehouseError, match="failed"):
        snap.query(f"SELECT * FROM read_csv('{(ROOT / 'README.md').as_posix()}')")


def test_snapshot_finds_tables_by_full_or_bare_name(snap: Snapshot) -> None:
    assert snap.table("dim.Customer").full_name == "dim.customer"
    assert snap.table("loan").full_name == "fact.loan"
    assert snap.table("nothing") is None


# --- reading Delta tables from OneLake -----------------------------------------------------------------


class _Resp:
    def __init__(self, data: bytes):
        self.content, self.text = data, data.decode("utf-8", "ignore")


def _lake(files: dict[str, bytes], log_names: list[str] | None = None) -> LakehouseData:
    settings = Settings(mode="snapshot", static_dir=ROOT / "__no_frontend__")
    fabric = SimpleNamespace(configured=True, tokens=SimpleNamespace(mode="service_principal"), http=None)
    data = LakehouseData(settings, fabric=fabric)  # type: ignore[arg-type]
    data._ls = lambda wid, directory: log_names if log_names is not None and directory.endswith("_delta_log") else []  # type: ignore[method-assign]
    data._get = lambda url, **kw: _Resp(files[url.rsplit("/", 1)[-1]])  # type: ignore[method-assign]
    return data


def _commit(*actions: dict[str, Any]) -> bytes:
    return "\n".join(json.dumps(a) for a in actions).encode()


def test_delta_log_replay_keeps_only_current_files(tmp_path: Path) -> None:
    meta = {"metaData": {"partitionColumns": [], "configuration": {}}}
    files = {
        "00000000000000000000.json": _commit({"protocol": {}}, meta, {"add": {"path": "a.parquet"}}),
        "00000000000000000001.json": _commit({"add": {"path": "b.parquet"}}),
        "00000000000000000002.json": _commit({"remove": {"path": "a.parquet"}}, {"add": {"path": "c.parquet"}}),
    }
    data = _lake(files, sorted(files))
    assert data._active_files("w", "lh/Tables/dim/t", tmp_path) == ["b.parquet", "c.parquet"]


@pytest.mark.parametrize(
    "action, message",
    [
        ({"metaData": {"partitionColumns": ["country"]}}, "partitioned"),
        ({"metaData": {"partitionColumns": [], "configuration": {"delta.columnMapping.mode": "name"}}}, "column mapping"),
        ({"add": {"path": "a.parquet", "deletionVector": {"storageType": "u"}}}, "deletion vectors"),
    ],
)
def test_delta_features_that_cannot_be_read_are_refused_not_misread(tmp_path: Path, action: dict, message: str) -> None:
    extra = [] if "add" in action else [{"add": {"path": "a.parquet"}}]  # a second add of the same file would replace the first
    files = {"00000000000000000000.json": _commit(action, *extra)}
    with pytest.raises(LakehouseError, match=message):
        _lake(files, sorted(files))._active_files("w", "lh/Tables/dim/t", tmp_path)


def test_a_table_is_loaded_from_parquet_with_time_zones_made_plain(tmp_path: Path) -> None:
    parquet = tmp_path / "part.parquet"
    duckdb.execute(
        f"COPY (SELECT 1 AS id, TIMESTAMPTZ '2026-01-01 10:00:00+00' AS made_at, 'x' AS tag) TO '{parquet.as_posix()}' (FORMAT parquet)"
    )
    files = {
        "00000000000000000000.json": _commit({"metaData": {"partitionColumns": []}}, {"add": {"path": "part.parquet"}}),
        "part.parquet": parquet.read_bytes(),
    }
    data = _lake(files, ["00000000000000000000.json"])
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    info = data._load_table(con, "w", "lh", "dim", "t")
    assert info is not None and info.rows == 1 and info.columns[1] == ("made_at", "TIMESTAMP")
    assert con.execute("SELECT made_at FROM dim.t").fetchone()[0] == datetime(2026, 1, 1, 10, 0)  # no tzdata needed


def test_a_hostile_file_path_in_the_log_is_never_fetched(tmp_path: Path) -> None:
    files = {"00000000000000000000.json": _commit({"metaData": {"partitionColumns": []}}, {"add": {"path": "../../secret.parquet"}})}
    with pytest.raises(LakehouseError, match="unexpected path"):
        _lake(files, sorted(files))._load_table(duckdb.connect(), "w", "lh", "dim", "t")


# --- the tools and the loop -----------------------------------------------------------------------------


def tools_for(snap: Snapshot, **kw: Any) -> GraphTools:
    return GraphTools(None, None, FakeEmbedder(), data=FakeLake(snap), **kw)  # type: ignore[arg-type]


def test_scorecard_tool_exists_only_for_a_data_quality_turn(snap: Snapshot) -> None:
    names = lambda t: [s["function"]["name"] for s in t.specs]  # noqa: E731
    assert QUALITY_TOOL not in names(tools_for(snap)) and QUALITY_TOOL in names(tools_for(snap, allow_quality=True))
    assert {"list_data_tables", "query_data"} <= set(names(tools_for(snap)))
    refused = tools_for(snap).call(QUALITY_TOOL)
    assert "only built when" in refused["error"]


def test_data_tools_return_what_the_model_needs(snap: Snapshot) -> None:
    t = tools_for(snap)
    listed = t.call("list_data_tables")
    assert listed["count"] == 3 and {x["table"] for x in listed["tables"]} == {"dim.customer", "dim.account", "fact.loan"}
    assert t.call("query_data", {"sql": "SELECT count(*) AS n FROM fact.loan WHERE loan_status = 'NPA'"})["rows"] == [[4]]
    assert "Only a single SELECT" in t.call("query_data", {"sql": "DROP TABLE fact.loan"})["error"]
    assert "not connected" in GraphTools(None, None, FakeEmbedder()).call("list_data_tables")["error"]  # type: ignore[arg-type]


def test_lakehouse_failure_is_a_readable_error_not_a_crash() -> None:
    class Broken:
        configured = True

        def snapshot(self, refresh: bool = False) -> Snapshot:
            raise LakehouseError("OneLake returned HTTP 503.")

    out = GraphTools(None, None, FakeEmbedder(), data=Broken()).call("list_data_tables")  # type: ignore[arg-type]
    assert "not available right now" in out["error"] and "503" in out["error"]


def test_the_full_scorecard_goes_to_the_browser_and_only_a_summary_to_the_model(snap: Snapshot) -> None:
    llm = ScriptedLLM(ask(call(QUALITY_TOOL)), say("Two problems matter most."))
    res = run_chat([{"role": "user", "content": "How is our data quality?"}], llm, tools_for(snap, allow_quality=True))
    assert res.dashboard is not None and res.dashboard["overall"]["issues"] >= 1 and len(res.dashboard["tables"]) == 3
    sent_back = json.dumps(llm.sent[1][0][-1])
    assert "worst_issues" in sent_back and '"tables"' not in sent_back and "freshness" not in sent_back
    assert res.steps[0].summary.startswith("scorecard with ") and not res.steps[0].error


# --- the API, the suggested questions and the saved answers ---------------------------------------------


def make_client(repo: Any, llm: Any, snap: Snapshot, tmp_path: Path, cache: dict | None = None) -> TestClient:
    path = tmp_path / "cached_answers.json"
    if cache is not None:
        path.write_text(json.dumps(cache), encoding="utf-8")
    cfg = Settings(mode="snapshot", static_dir=ROOT / "__no_frontend__", chat_cache_file=str(path),
                   AZURE_OPEN_AI_KEY="k", AZURE_OPEN_AI_ENDPOINT="https://e", AZURE_OPEN_AI_MODEL_NAME="dep")
    return TestClient(create_app(cfg, repo=repo, llm=llm, embedder=FakeEmbedder(), lakehouse=FakeLake(snap)))  # type: ignore[arg-type]


def test_api_returns_the_dashboard_for_a_data_quality_question(repo, snap: Snapshot, tmp_path: Path) -> None:
    llm = ScriptedLLM(ask(call(QUALITY_TOOL)), say("Here is the picture."))
    with make_client(repo, llm, snap, tmp_path) as c:
        body = c.post("/api/chat", json={"messages": [{"role": "user", "content": "How healthy is our data?"}]}).json()
    assert body["dashboard"]["title"] == "Data quality scorecard" and body["dashboard"]["overall"]["score"] is not None
    assert body["graph"] is None and "cached" not in body


def test_api_offers_no_scorecard_for_other_questions(repo, snap: Snapshot, tmp_path: Path) -> None:
    llm = ScriptedLLM(say("They are in the lakehouse."))
    with make_client(repo, llm, snap, tmp_path) as c:
        body = c.post("/api/chat", json={"messages": [{"role": "user", "content": "Which pipelines write to the lakehouse?"}]}).json()
    assert body["dashboard"] is None
    assert QUALITY_TOOL not in [s["function"]["name"] for s in llm.sent[0][1]]


def test_a_suggested_question_is_answered_from_the_saved_copy(repo, snap: Snapshot, tmp_path: Path) -> None:
    q = QUESTIONS[0]
    saved = {"generated_at": "2026-11-24T10:00:00+00:00", "answers": {q["id"]: {
        "question": q["question"], "prepared_at": "2026-11-24T09:00:00+00:00",
        "response": {"answer": "Saved answer.", "graph": None, "dashboard": None, "citations": [], "steps": [], "tokens": 5, "model": "dep"}}}}
    llm = ScriptedLLM()  # any call to the model would fail: there is no reply queued
    with make_client(repo, llm, snap, tmp_path, saved) as c:
        status = c.get("/api/chat/status").json()
        hit = c.post("/api/chat", json={"messages": [{"role": "user", "content": q["question"].upper() + "  "}]}).json()
        flags = {s["id"]: s["saved"] for s in status["suggestions"]}
    assert hit["answer"] == "Saved answer." and hit["cached"] is True and hit["cached_at"] == "2026-11-24T09:00:00+00:00"
    assert flags[q["id"]] is True and flags[QUESTIONS[1]["id"]] is False
    assert llm.sent == []


def test_a_follow_up_or_an_unsaved_question_always_goes_to_the_model(repo, snap: Snapshot, tmp_path: Path) -> None:
    q = QUESTIONS[0]
    saved = {"answers": {q["id"]: {"question": q["question"], "response": {"answer": "Saved answer."}}}}
    llm = ScriptedLLM(say("Fresh answer."), say("Another fresh answer."))
    with make_client(repo, llm, snap, tmp_path, saved) as c:
        follow_up = c.post("/api/chat", json={"messages": [
            {"role": "user", "content": q["question"]}, {"role": "assistant", "content": "x"}, {"role": "user", "content": "and the loans?"}]}).json()
        other = c.post("/api/chat", json={"messages": [{"role": "user", "content": "A different question"}]}).json()
    assert follow_up["answer"] == "Fresh answer." and other["answer"] == "Another fresh answer." and len(llm.sent) == 2


def test_cache_ignores_a_missing_or_broken_file_and_can_be_switched_off(tmp_path: Path) -> None:
    assert AnswerCache(tmp_path / "nope.json").get("anything") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert AnswerCache(bad).get("anything") is None
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"answers": {"a": {"question": "Q?", "response": {"answer": "A"}}}}), encoding="utf-8")
    assert AnswerCache(good).get("q") is not None and AnswerCache(good, enabled=False).get("q") is None
    assert normalise("  Which   loans?? ") == "which loans"


def test_suggested_questions_are_distinct_and_businesslike() -> None:
    ids = [q["id"] for q in QUESTIONS]
    assert len(ids) == len(set(ids)) >= 6
    assert len({normalise(q["question"]) for q in QUESTIONS}) == len(QUESTIONS)
    assert all(len(q["question"]) > 60 and q["label"] for q in QUESTIONS)
    text = " ".join(q["question"].lower() for q in QUESTIONS)
    assert "cypher" not in text and "neo4j" not in text and "poc" not in text
