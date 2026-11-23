"""Chatbot: tools return trimmed read-only results, the agent loop, and the /api/chat endpoint.

No test touches the network: the LLM is a scripted fake, the embedder returns a fixed vector and the policy
queries run against a fake Neo4j client.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.chat.agent import SYSTEM_PROMPT, run_chat
from app.chat.graph import clean_view, wants_graph
from app.chat.embeddings import EmbeddingUnavailable
from app.chat.llm import LLMError, LLMReply, ToolCall
from app.chat.tools import GRAPH_TOOL, MAX_ROWS, POLICY_INDEX, GraphTools, _brief
from app.config import Settings
from app.graphmodel import GNode
from app.main import create_app
from app.neo4j_client import DatabaseUnavailable
from app.snapshot import SnapshotRepository

ROOT = Path(__file__).resolve().parents[2]
BIG_VECTOR = [0.01] * 384


# --- fakes ------------------------------------------------------------------------------------------


class FakeEmbedder:
    def __init__(self, ready: bool = True):
        self.ready = ready
        self.seen: list[str] = []

    def is_ready(self) -> bool:
        return self.ready

    def embed_query(self, text: str) -> list[float]:
        if not self.ready:
            raise EmbeddingUnavailable("model not installed")
        self.seen.append(text)
        return BIG_VECTOR


@dataclass
class FakeResult:
    records: list[dict[str, Any]]


class FakeClient:
    """Stands in for Neo4jClient: records every query and returns canned rows."""

    def __init__(self, records: list[dict[str, Any]] | None = None, fail: bool = False):
        self.records = records or []
        self.fail = fail
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run_read(self, query: str, params: dict[str, Any] | None = None, max_rows: int | None = None) -> FakeResult:
        if self.fail:
            raise DatabaseUnavailable("down")
        self.calls.append((query, params or {}))
        return FakeResult(self.records[: max_rows or len(self.records)])


class ScriptedLLM:
    """Returns the queued replies in order and records what it was sent."""

    model = "fake-model"

    def __init__(self, *replies: LLMReply):
        self.replies = list(replies)
        self.sent: list[tuple[list[dict[str, Any]], list[dict[str, Any]] | None]] = []

    def complete(self, messages, tools):  # type: ignore[no-untyped-def]
        self.sent.append((json.loads(json.dumps(messages)), tools))
        return self.replies.pop(0)


def call(name: str, **args: Any) -> ToolCall:
    return ToolCall(id=f"call_{name}", name=name, arguments=args, raw_arguments=json.dumps(args))


def say(text: str) -> LLMReply:
    return LLMReply(content=text, total_tokens=10)


def ask(*calls: ToolCall) -> LLMReply:
    return LLMReply(content="", tool_calls=list(calls), total_tokens=10)


SECTION_ROW = {
    "doc_id": "pol-1",
    "doc_title": "Retail Loan Policy",
    "chunk_id": "pol-1-c3",
    "section_path": "3 > 3.2 Risk buckets",
    "heading": "3.2 Risk buckets",
    "text": "Loans past 90 days are classified HIGH. " * 80,
    "score": 0.87654,
}


@pytest.fixture()
def tools(repo) -> GraphTools:
    return GraphTools(repo, None, FakeEmbedder())


@pytest.fixture()
def live_tools(repo) -> tuple[GraphTools, FakeClient, FakeEmbedder]:
    client, emb = FakeClient([SECTION_ROW]), FakeEmbedder()
    return GraphTools(repo, client, emb), client, emb  # type: ignore[arg-type]


# --- tools: graph tools never leak raw properties ---------------------------------------------------


def test_brief_never_exposes_embeddings() -> None:
    node = GNode(id="x", labels=["Column"], props={"name": "c", "embedding": BIG_VECTOR, "is_pii": True, "table": "t"})
    out = _brief(node)
    assert "embedding" not in out and out["is_pii"] is True
    assert "0.01" not in json.dumps(out)


def test_find_assets_and_not_found_hint(tools) -> None:
    found = tools.call("find_assets", {"query": "risk_bucket"})
    assert found["count"] >= 1 and all("embedding" not in a for a in found["assets"])
    missing = tools.call("get_lineage", {"asset": "no_such_thing_xyz"})
    assert "No asset found" in missing["error"]


def test_describe_table_lists_columns_with_pii_flags(tools) -> None:
    out = tools.call("describe_table", {"asset": "banking.customers"})
    names = {c["name"]: c for c in out["columns"]}
    assert out["column_count"] == len(out["columns"]) >= 10
    assert names["first_name"]["is_pii"] is True and names["first_name"]["classification"]
    assert "is_pii" not in names["customer_id"]
    assert "error" in tools.call("describe_table", {"asset": "banking.customers.first_name"})


def test_lineage_and_impact_are_bounded_and_named(tools) -> None:
    lin = tools.call("get_lineage", {"asset": "fact.loan_portfolio.risk_bucket", "direction": "upstream", "depth": 3})
    assert lin["flow_count"] >= 1 and len(lin["flows"]) <= MAX_ROWS
    assert all({"from", "to", "transformation"} <= f.keys() for f in lin["flows"])
    imp = tools.call("get_impact", {"asset": "banking.customers"})
    assert imp["summary"].startswith("A change here affects") and imp["flow_count"] >= 1
    assert tools.call("get_lineage", {"asset": "banking.loans", "direction": "sideways"})["direction"] == "both"


def test_pii_and_pipelines(tools) -> None:
    pii = tools.call("list_pii_columns")
    assert pii["summary"]["pii_columns"] >= 1
    assert {"column", "status", "treatment"} <= pii["rows"][0].keys()
    pipes = tools.call("list_pipelines")
    assert pipes["activity_count"] >= 1
    assert tools.call("list_pipelines", {"pipeline": "zzz-none"})["activity_count"] == 0


def test_unknown_tool_and_bad_arguments(tools) -> None:
    assert "Unknown tool" in tools.call("drop_database")["error"]
    assert "Bad arguments" in tools.call("find_assets", {"nonsense": 1})["error"]
    assert "Bad arguments" in tools.call("find_assets")["error"]  # required arg missing


# --- tools: policy tools (fake Neo4j) ---------------------------------------------------------------


def test_policy_tools_unavailable_in_snapshot(tools) -> None:
    for name, args in (("search_policies", {"query": "x"}), ("list_policy_documents", {}), ("get_policy_section", {"chunk_id": "c"})):
        assert "snapshot" in tools.call(name, args)["error"]
    assert tools.vector_search_ready is False


def test_search_policies_embeds_query_and_trims(live_tools) -> None:
    t, client, emb = live_tools
    out = t.call("search_policies", {"query": "  when is a loan high risk?  ", "top_k": 99})
    assert emb.seen == ["when is a loan high risk?"]
    query, params = client.calls[0]
    assert params["index"] == POLICY_INDEX and params["k"] == 10 and params["vector"] == BIG_VECTOR
    s = out["sections"][0]
    assert s["score"] == 0.877 and s["section_path"] == "3 > 3.2 Risk buckets"
    assert len(s["text"]) < 1600 and s["text"].endswith("[truncated]")
    assert "embedding" not in json.dumps(out)
    assert "CREATE" not in query.upper() and "MERGE" not in query.upper()


def test_search_policies_reports_missing_model(repo) -> None:
    t = GraphTools(repo, FakeClient(), FakeEmbedder(ready=False))  # type: ignore[arg-type]
    assert "not set up" in t.call("search_policies", {"query": "x"})["error"]
    assert t.vector_search_ready is False


def test_policies_for_asset_and_section(repo) -> None:
    client = FakeClient([{**SECTION_ROW, "asset": "risk_bucket", "asset_table": "fact.loan_portfolio", "term": "risk bucket",
                          "method": "text-phrase", "governs": [{"name": "risk_bucket", "term": None}], "governs_total": 1}])
    t = GraphTools(repo, client, FakeEmbedder())  # type: ignore[arg-type]
    out = t.call("get_policies_for_asset", {"asset": "fact.loan_portfolio.risk_bucket"})
    assert out["sections"][0]["asset"] == "fact.loan_portfolio.risk_bucket" and out["sections"][0]["method"] == "text-phrase"
    assert client.calls[0][1]["id"].endswith("risk_bucket")
    sec = t.call("get_policy_section", {"chunk_id": "pol-1-c3"})
    assert sec["linked_assets"] == [{"name": "risk_bucket"}] and sec["linked_assets_total"] == 1
    assert len(sec["sections"][0]["text"]) > 1600  # full text when a single section is requested
    empty = GraphTools(repo, FakeClient([]), FakeEmbedder())  # type: ignore[arg-type]
    assert "No policy section" in empty.call("get_policy_section", {"chunk_id": "nope"})["error"]


def test_database_down_propagates(repo) -> None:
    t = GraphTools(repo, FakeClient(fail=True), FakeEmbedder())  # type: ignore[arg-type]
    with pytest.raises(DatabaseUnavailable):
        t.call("search_policies", {"query": "x"})


# --- agent loop -------------------------------------------------------------------------------------


def test_agent_runs_tools_then_answers_and_collects_citations(live_tools) -> None:
    t, _, _ = live_tools
    llm = ScriptedLLM(ask(call("search_policies", query="high risk")), say("HIGH after 90 days [Retail Loan Policy > 3.2]"))
    res = run_chat([{"role": "user", "content": "When is a loan high risk?"}], llm, t)
    assert res.answer.startswith("HIGH after 90 days")
    assert [(s.tool, s.error) for s in res.steps] == [("search_policies", False)]
    assert res.steps[0].summary == "1 result"
    assert res.citations == [{"chunk_id": "pol-1-c3", "doc_id": "pol-1", "doc_title": "Retail Loan Policy",
                              "section_path": "3 > 3.2 Risk buckets", "heading": "3.2 Risk buckets"}]
    assert res.tokens == 20
    first_msgs, first_tools = llm.sent[0]
    assert first_msgs[0] == {"role": "system", "content": SYSTEM_PROMPT} and len(first_tools) == 11
    second = llm.sent[1][0]
    assert second[-1]["role"] == "tool" and second[-1]["tool_call_id"] == "call_search_policies"
    assert second[-2]["tool_calls"][0]["function"]["name"] == "search_policies"


def test_agent_survives_tool_errors_and_oversized_results(tools) -> None:
    llm = ScriptedLLM(ask(call("get_lineage", asset="ghost"), call("find_assets", query="a")), say("done"))
    res = run_chat([{"role": "user", "content": "q"}], llm, tools)
    assert [s.error for s in res.steps] == [True, False]
    assert res.answer == "done"
    tool_msgs = [m for m in llm.sent[1][0] if m["role"] == "tool"]
    assert len(tool_msgs) == 2 and "No asset found" in tool_msgs[0]["content"]


def test_agent_stops_after_max_rounds_and_forces_an_answer(tools) -> None:
    looping = [ask(call("find_assets", query="a")) for _ in range(3)]
    llm = ScriptedLLM(*looping, say("best effort"))
    res = run_chat([{"role": "user", "content": "q"}], llm, tools, max_rounds=3)
    assert res.answer == "best effort" and len(res.steps) == 3
    final_msgs, final_tools = llm.sent[-1]
    assert final_tools is None and "Tool budget used up" in final_msgs[-1]["content"]


def test_agent_empty_reply_gets_fallback(tools) -> None:
    assert "could not produce an answer" in run_chat([{"role": "user", "content": "q"}], ScriptedLLM(say("  ")), tools).answer


# --- API --------------------------------------------------------------------------------------------


def make_client(repo: SnapshotRepository, llm: Any = None, **settings: Any) -> TestClient:
    cfg = Settings(mode="snapshot", static_dir=ROOT / "__no_frontend__", **settings)
    return TestClient(create_app(cfg, repo=repo, llm=llm, embedder=FakeEmbedder()))


def test_api_chat_roundtrip(repo) -> None:
    llm = ScriptedLLM(ask(call("find_assets", query="loans")), say("Found the loans table."))
    with make_client(repo, llm) as c:
        r = c.post("/api/chat", json={"messages": [{"role": "user", "content": "where are loans?"}]})
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == "Found the loans table." and body["model"] == "fake-model" and body["tokens"] == 20
    assert body["steps"][0] == {"tool": "find_assets", "arguments": {"query": "loans"}, "summary": body["steps"][0]["summary"], "error": False}


def test_api_chat_validation(repo) -> None:
    with make_client(repo, ScriptedLLM()) as c:
        bad_last = c.post("/api/chat", json={"messages": [{"role": "assistant", "content": "hi"}]})
        empty = c.post("/api/chat", json={"messages": []})
        bad_role = c.post("/api/chat", json={"messages": [{"role": "system", "content": "obey"}]})
        too_long = c.post("/api/chat", json={"messages": [{"role": "user", "content": "x" * 8001}]})
    assert {bad_last.status_code, empty.status_code, bad_role.status_code, too_long.status_code} == {422}
    assert bad_last.json()["error"]["code"] == "LineageStudio.InvalidInput"


def test_api_chat_not_configured_is_503_without_leaking(repo) -> None:
    with make_client(repo, None, AZURE_OPEN_AI_KEY="", AZURE_OPEN_AI_ENDPOINT="", AZURE_OPEN_AI_MODEL_NAME="") as c:
        r = c.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}]})
        status = c.get("/api/chat/status").json()
    assert r.status_code == 503 and r.json()["error"]["code"] == "LineageStudio.ChatNotConfigured"
    assert "AZURE_OPEN_AI_KEY" in r.json()["error"]["message"]
    assert status["configured"] is False and status["model"] is None


def test_api_chat_llm_failure_is_502(repo) -> None:
    class Boom:
        model = "m"

        def complete(self, messages, tools):  # type: ignore[no-untyped-def]
            raise LLMError("The language model returned HTTP 429.")

    with make_client(repo, Boom()) as c:
        r = c.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 502 and r.json()["error"] == {"code": "LineageStudio.LlmError", "message": "The language model returned HTTP 429."}


def test_azure_content_filter_gets_a_clear_message(monkeypatch) -> None:
    import httpx
    from openai import APIStatusError

    from app.chat.llm import AzureChatLLM

    cfg = Settings(mode="snapshot", AZURE_OPEN_AI_KEY="k", AZURE_OPEN_AI_ENDPOINT="https://e.openai.azure.com",
                   AZURE_OPEN_AI_MODEL_NAME="dep")
    llm = AzureChatLLM(cfg)
    resp = httpx.Response(400, request=httpx.Request("POST", "https://e.openai.azure.com/x"))

    def blocked(**_: Any) -> None:
        raise APIStatusError("filtered", response=resp, body={"code": "content_filter"})

    monkeypatch.setattr(llm._client.chat.completions, "create", blocked)
    with pytest.raises(LLMError, match="content filter"):
        llm.complete([{"role": "user", "content": "x"}], None)

    def other(**_: Any) -> None:
        raise APIStatusError("bad", response=resp, body={"code": "invalid_prompt"})

    monkeypatch.setattr(llm._client.chat.completions, "create", other)
    with pytest.raises(LLMError, match="HTTP 400"):
        llm.complete([{"role": "user", "content": "x"}], None)


def test_api_chat_status(repo) -> None:
    with make_client(repo, None, AZURE_OPEN_AI_KEY="k", AZURE_OPEN_AI_ENDPOINT="https://e", AZURE_OPEN_AI_MODEL_NAME="dep") as c:
        s = c.get("/api/chat/status").json()
    assert s == {"configured": True, "model": "dep", "policy_search": False, "mode": "snapshot"}  # no client in snapshot mode


# --- graph display ----------------------------------------------------------------------------------

RISK_ID = "fabric:Ontology_Lineage.fact.loan_portfolio.risk_bucket"
LOANS_ID = "pg:bankdb.banking.loans"


@pytest.mark.parametrize(
    "text",
    [
        "show me a graph of banking.loans",
        "Can you draw the lineage of risk_bucket?",
        "I would like a diagram of the pipelines",
        "visualise the PII flow",
        "Visualization of what policies cover dim.account",
        "give me a chart",
        "a picture of the impact please",
        "GRAPH of customers",
        # asking to show / see a lineage is asking for a picture
        "i want to see the lineage of all ontology terms",
        "Show me the lineage of dim.branch",
        "show the impact of changing banking.loans",
        "Can I see the data flow into dim.account?",
        "display upstream and downstream of risk_bucket",
        "look at the dependencies of fact.loan_portfolio",
        "I'd like to SEE the lineage.",
        "I see the lineage is wrong, can you show me the lineage of risk_bucket",  # observation, then a request
    ],
)
def test_wants_graph_when_asked_directly(text) -> None:
    assert wants_graph(text)


@pytest.mark.parametrize(
    "text",
    [
        "What is the lineage of risk_bucket?",
        "Which columns hold PII?",
        "What does the policy say about GraphRAG?",
        "Is this an autograph or a paragraph?",
        "Which pipelines write to the lakehouse",
        "",
        # a lineage QUESTION is not a request for a picture
        "Where does fact.loan_portfolio.risk_bucket come from?",
        "What is the impact of changing banking.loans?",
        "Explain the lineage of dim.account",
        "show me for dim.branch table",  # no lineage word to key on
        "Show me the PII columns",
        "describe the view vw_loan_summary and its lineage",  # a "view" is a database object, not a verb here
        "Show me what the policy says about cash flow",
        "I see the lineage is incomplete. Why?",  # "I see ..." is an observation, not a request
        "See also: the retention policy. What is the data flow rule?",
    ],
)
def test_wants_graph_is_false_otherwise(text) -> None:
    assert not wants_graph(text)


def test_show_graph_is_only_offered_when_allowed(repo) -> None:
    off, on = GraphTools(repo, None, FakeEmbedder()), GraphTools(repo, None, FakeEmbedder(), allow_graph=True)
    assert GRAPH_TOOL not in [s["function"]["name"] for s in off.specs] and len(off.specs) == 11
    assert GRAPH_TOOL in [s["function"]["name"] for s in on.specs] and len(on.specs) == 12
    # Defence in depth: even if the model names the tool anyway, it is refused.
    assert "directly asks" in off.call(GRAPH_TOOL, {"kind": "pii"})["error"]


@pytest.fixture()
def graph_tools(demo_data) -> GraphTools:
    nodes, edges = demo_data
    nodes = [  # one column carries an embedding, as in the real graph
        {**n, "embedding": [0.01] * 384} if n["id"] == RISK_ID else n for n in nodes
    ] + [
        {"id": "pol-doc", "label": "PolicyDocument", "doc_id": "pol-doc", "title": "Retail Loan Policy"},
        {"id": "pol-c1", "label": "PolicyChunk", "chunk_id": "pol-c1", "heading": "2.1 Terms", "text": "t" * 900},
    ]
    edges = [
        *edges,
        {"from": "pol-doc", "rel": "HAS_CHUNK", "to": "pol-c1"},
        {"from": "pol-c1", "rel": "GOVERNS", "to": LOANS_ID, "method": "text-phrase"},
    ]
    return GraphTools(SnapshotRepository(nodes, edges), None, FakeEmbedder(), allow_graph=True)


def shown(result: dict[str, Any]) -> dict[str, Any]:
    assert "error" not in result, result
    assert result["graph_displayed"] is True
    g = result["_graph"]
    assert result["nodes"] == len(g["nodes"]) > 0 and result["edges"] == len(g["edges"])
    assert g["title"] and isinstance(g["cypher"], str) and g["cypher"].strip()
    ids = {n["id"] for n in g["nodes"]}
    assert all(e["source"] in ids and e["target"] in ids for e in g["edges"])
    assert "embedding" not in json.dumps(g)  # embeddings never reach the browser
    return g


def test_show_graph_lineage_of_a_column_and_a_table(graph_tools) -> None:
    col = shown(graph_tools.call("show_graph", {"kind": "lineage", "asset": "fact.loan_portfolio.risk_bucket", "direction": "upstream"}))
    assert col["kind"] == "lineage" and "risk_bucket" in col["title"] and "upstream" in col["title"]
    assert "'" + RISK_ID + "'" in col["cypher"] and "$id" not in col["cypher"]  # runnable as-is in the Query page
    assert any(n.get("start") for n in col["nodes"])
    tbl = shown(graph_tools.call("show_graph", {"kind": "lineage", "asset": "banking.loans"}))
    assert {n["label"] for n in tbl["nodes"]} <= {"Table", "View"}  # tables roll up to table level


def test_show_graph_impact_pii_pipelines_neighbors(graph_tools) -> None:
    assert "Impact of changing" in shown(graph_tools.call("show_graph", {"kind": "impact", "asset": "banking.customers"}))["title"]
    pii = shown(graph_tools.call("show_graph", {"kind": "pii"}))
    assert any(n["label"] == "Column" and n["properties"].get("is_pii") for n in pii["nodes"])
    pipes = shown(graph_tools.call("show_graph", {"kind": "pipelines"}))
    assert any(n["label"] == "Pipeline" for n in pipes["nodes"])
    near = shown(graph_tools.call("show_graph", {"kind": "neighbors", "asset": "banking.loans"}))
    assert any(n.get("start") for n in near["nodes"]) and "LIMIT 100" in near["cypher"]


def test_show_graph_policies_links_documents_chunks_and_assets(graph_tools) -> None:
    g = shown(graph_tools.call("show_graph", {"kind": "policies", "asset": "banking.loans"}))
    assert {"PolicyDocument", "PolicyChunk"} <= {n["label"] for n in g["nodes"]}
    assert {e["type"] for e in g["edges"]} >= {"HAS_CHUNK", "GOVERNS"}
    chunk = next(n for n in g["nodes"] if n["label"] == "PolicyChunk")
    assert len(chunk["properties"]["text"]) < 400  # long text is clipped
    assert "'" + LOANS_ID + "'" in g["cypher"] and "$ids" not in g["cypher"]  # asset ids inlined, runnable as-is
    assert "No policy sections" in graph_tools.call("show_graph", {"kind": "policies", "asset": "banking.customers"})["error"]


def test_bare_pipeline_names_resolve_via_search(tools) -> None:
    # resolve() alone cannot find a bare pipeline name; the tools fall back to an exact-name search hit.
    out = tools.call("describe_table", {"asset": "PL_CP_Loan_Portfolio"})
    assert "is a Pipeline, not a table" in out["error"]


def test_show_graph_bad_requests_are_readable_errors(graph_tools) -> None:
    for args, needle in (
        ({"kind": "hologram"}, "Unknown graph kind"),
        ({"kind": "lineage"}, "needs an asset name"),
        ({"kind": "lineage", "asset": "ghost_xyz"}, "No asset found"),
        ({"kind": "lineage", "asset": "PL_CP_Loan_Portfolio"}, "needs a table, view or column"),
    ):
        assert needle in graph_tools.call("show_graph", args)["error"]


def test_clean_view_strips_embeddings_clips_text_and_caps_nodes() -> None:
    nodes = [
        {"id": f"n{i}", "label": "Column", "start": i == 7,
         "properties": {"embedding": [0.1] * 384, "embedding_model": "bge", "embedding_text": "t", "name": f"c{i}", "text": "x" * 1000, "vec": [1.0] * 50, "tags": [1, 2]}}
        for i in range(10)
    ]
    edges = [{"id": "e1", "source": "n7", "target": "n0", "properties": {}}, {"id": "e2", "source": "n8", "target": "n9", "properties": {}}]
    view, truncated = clean_view({"nodes": nodes, "edges": edges}, max_nodes=5)
    assert truncated and len(view["nodes"]) == 5
    assert view["nodes"][0]["id"] == "n7"  # the start node survives the cap
    props = view["nodes"][0]["properties"]
    assert not [k for k in props if k.startswith("embedding")] and "vec" not in props and props["tags"] == [1, 2]
    assert len(props["text"]) < 310 and props["text"].endswith("…")
    assert [e["id"] for e in view["edges"]] == ["e1"]  # edges to dropped nodes are removed
    small, not_truncated = clean_view({"nodes": nodes[:3], "edges": []}, max_nodes=5)
    assert not not_truncated and len(small["nodes"]) == 3


def test_agent_sends_graph_to_ui_but_only_its_size_to_the_model(graph_tools) -> None:
    llm = ScriptedLLM(
        ask(call("show_graph", kind="lineage", asset="fact.loan_portfolio.risk_bucket", direction="upstream")),
        say("It is derived from three loan columns."),
    )
    res = run_chat([{"role": "user", "content": "draw the lineage of risk_bucket"}], llm, graph_tools)
    assert res.graph and res.graph["kind"] == "lineage" and len(res.graph["nodes"]) >= 2
    assert res.steps[0].summary.startswith("graph with ") and not res.steps[0].error
    sent_to_model = next(m for m in llm.sent[1][0] if m["role"] == "tool")["content"]
    payload = json.loads(sent_to_model)
    assert payload["graph_displayed"] is True and "_graph" not in payload and "properties" not in sent_to_model
    assert "show_graph" in [t["function"]["name"] for t in llm.sent[0][1]]


def test_agent_without_a_graph_returns_none(tools) -> None:
    res = run_chat([{"role": "user", "content": "q"}], ScriptedLLM(ask(call("find_assets", query="a")), say("ok")), tools)
    assert res.graph is None


def test_api_graph_only_for_a_direct_request(repo) -> None:
    drawn = ScriptedLLM(ask(call("show_graph", kind="pii")), say("Seven PII columns, mostly masked."))
    with make_client(repo, drawn) as c:
        r = c.post("/api/chat", json={"messages": [{"role": "user", "content": "Show me a graph of the PII flow"}]}).json()
    assert r["graph"]["kind"] == "pii" and r["graph"]["nodes"] and r["graph"]["cypher"]
    assert "show_graph" in [t["function"]["name"] for t in drawn.sent[0][1]]
    assert r["steps"][0]["tool"] == "show_graph" and r["steps"][0]["summary"].startswith("graph with ")

    plain = ScriptedLLM(say("Seven columns hold PII."))
    with make_client(repo, plain) as c:
        r2 = c.post("/api/chat", json={"messages": [{"role": "user", "content": "Which columns hold PII?"}]}).json()
    assert r2["graph"] is None
    assert "show_graph" not in [t["function"]["name"] for t in plain.sent[0][1]]

    # Only the LATEST message counts: a follow-up that does not ask for a picture gets none.
    followup = ScriptedLLM(say("Thanks!"))
    with make_client(repo, followup) as c:
        msgs = [
            {"role": "user", "content": "graph of the PII flow"},
            {"role": "assistant", "content": "Here."},
            {"role": "user", "content": "thanks, and which are masked?"},
        ]
        r3 = c.post("/api/chat", json={"messages": msgs}).json()
    assert r3["graph"] is None
    assert "show_graph" not in [t["function"]["name"] for t in followup.sent[0][1]]


# --- ontology terms ---------------------------------------------------------------------------------


def onto_repo(*, branch_fed_from_branches: bool = False) -> SnapshotRepository:
    """A tiny ontology mirroring the real anomaly: dim.branch (Branch) is fed from the accounts table
    (Account) while the branches table (Branch) feeds it nothing. fact.loans represents TWO terms."""
    nodes = [
        {"id": "t:branches", "label": "Table", "name": "branches", "full_name": "src.branches", "platform": "PostgreSQL"},
        {"id": "t:accounts", "label": "Table", "name": "accounts", "full_name": "src.accounts", "platform": "PostgreSQL"},
        {"id": "t:loans", "label": "Table", "name": "loans", "full_name": "src.loans", "platform": "PostgreSQL"},
        {"id": "t:dimb", "label": "Table", "name": "branch", "full_name": "dim.branch", "platform": "Fabric Lakehouse"},
        {"id": "t:factl", "label": "Table", "name": "loans", "full_name": "fact.loans", "platform": "Fabric Lakehouse"},
        {"id": "c:branches.id", "label": "Column", "name": "branch_id", "table": "src.branches"},
        {"id": "c:accounts.id", "label": "Column", "name": "account_id", "table": "src.accounts"},
        {"id": "c:loans.id", "label": "Column", "name": "loan_id", "table": "src.loans"},
        {"id": "c:dimb.id", "label": "Column", "name": "account_id", "table": "dim.branch"},
        {"id": "c:factl.id", "label": "Column", "name": "loan_id", "table": "fact.loans"},
        {"id": "concept:Branch", "label": "BusinessConcept", "name": "Branch", "description": "Physical bank branch"},
        {"id": "concept:Account", "label": "BusinessConcept", "name": "Account", "description": "A deposit account"},
        {"id": "concept:Loan", "label": "BusinessConcept", "name": "Loan", "description": "A credit facility"},
        {"id": "concept:CreditRisk", "label": "BusinessConcept", "name": "CreditRisk", "description": "Risk of the loan book"},
    ]
    edges = [
        *({"from": t, "rel": "HAS_COLUMN", "to": c} for t, c in (
            ("t:branches", "c:branches.id"), ("t:accounts", "c:accounts.id"), ("t:loans", "c:loans.id"),
            ("t:dimb", "c:dimb.id"), ("t:factl", "c:factl.id"))),
        {"from": "c:accounts.id", "rel": "FLOWS_TO", "to": "c:dimb.id", "transformation": "DIRECT", "pipeline": "PL_BRANCHES"},
        {"from": "c:loans.id", "rel": "FLOWS_TO", "to": "c:factl.id", "transformation": "DIRECT", "pipeline": "PL_LOANS"},
        {"from": "t:branches", "rel": "REPRESENTS", "to": "concept:Branch"},
        {"from": "t:dimb", "rel": "REPRESENTS", "to": "concept:Branch"},
        {"from": "t:accounts", "rel": "REPRESENTS", "to": "concept:Account"},
        {"from": "t:loans", "rel": "REPRESENTS", "to": "concept:Loan"},
        {"from": "t:factl", "rel": "REPRESENTS", "to": "concept:Loan"},
        {"from": "t:factl", "rel": "REPRESENTS", "to": "concept:CreditRisk"},
        {"from": "concept:Account", "rel": "RELATED_TO", "to": "concept:Branch", "via": "accounts.branch_id"},
    ]
    if branch_fed_from_branches:
        edges.append({"from": "c:branches.id", "rel": "FLOWS_TO", "to": "c:dimb.id", "transformation": "DIRECT", "pipeline": "PL_BRANCHES"})
    return SnapshotRepository(nodes, edges)


def terms(out: dict[str, Any]) -> dict[str, dict[str, Any]]:
    assert "error" not in out, out
    return {t["term"]: t for t in out["terms"]}


def test_get_ontology_lists_terms_with_their_tables_and_relations() -> None:
    t = GraphTools(onto_repo(), None, FakeEmbedder())
    by = terms(t.call("get_ontology"))
    assert set(by) == {"Account", "Branch", "CreditRisk", "Loan"}
    assert by["Branch"]["description"] == "Physical bank branch"
    assert by["Branch"]["represented_by"] == ["dim.branch (Fabric Lakehouse)", "src.branches (PostgreSQL)"]
    assert by["Account"]["related_to"] == [{"term": "Branch", "via": "accounts.branch_id"}]
    assert by["Branch"]["referenced_by"] == [{"term": "Account", "via": "accounts.branch_id"}]
    assert "embedding" not in json.dumps(by)


def test_get_ontology_shows_the_lineage_of_a_term_and_flags_the_branch_anomaly() -> None:
    t = GraphTools(onto_repo(), None, FakeEmbedder())
    branch = terms(t.call("get_ontology", {"term": "branch"}))["Branch"]  # case-insensitive
    assert branch["data_flows"] == [{
        "from": "src.accounts", "from_term": "Account", "to": "dim.branch", "to_term": "Branch",
        "pipelines": ["PL_BRANCHES"], "column_flows": 1, "treatment": "DIRECT",
    }]
    assert branch["lineage_notes"] == [
        "dim.branch represents Branch but receives data only from src.accounts (Account); "
        "nothing flows into it from src.branches, which also represents Branch."
    ]
    # Once the branches table really feeds dim.branch, the note goes away.
    fixed = terms(GraphTools(onto_repo(branch_fed_from_branches=True), None, FakeEmbedder()).call("get_ontology", {"term": "Branch"}))
    assert fixed["Branch"]["lineage_notes"] == []


def test_get_ontology_does_not_flag_a_table_that_represents_several_terms() -> None:
    by = terms(GraphTools(onto_repo(), None, FakeEmbedder()).call("get_ontology"))
    # fact.loans represents Loan AND CreditRisk and is fed by src.loans (Loan): nothing to flag for either.
    assert by["Loan"]["lineage_notes"] == [] and by["CreditRisk"]["lineage_notes"] == []
    flow = next(f for f in by["CreditRisk"]["data_flows"] if f["to"] == "fact.loans")
    assert flow["to_term"] == "CreditRisk, Loan"


def test_get_ontology_options_and_unknown_term() -> None:
    t = GraphTools(onto_repo(), None, FakeEmbedder())
    assert all("data_flows" not in x for x in t.call("get_ontology", {"include_lineage": False})["terms"])
    err = t.call("get_ontology", {"term": "zzz"})["error"]
    assert "No ontology term matches 'zzz'" in err and "Account, Branch, CreditRisk, Loan" in err


def test_get_ontology_on_the_demo_graph(repo) -> None:
    out = GraphTools(repo, None, FakeEmbedder()).call("get_ontology")
    by = terms(out)
    assert out["count"] == 6 and {"Customer", "Loan", "Account", "Branch"} <= set(by)
    cust = by["Customer"]
    assert any(f["from"] == "banking.customers" and f["to"] == "dim.customer" and f["pipelines"] for f in cust["data_flows"])
    assert all(x["lineage_notes"] == [] for x in out["terms"])  # the demo data is coherent


def test_find_assets_ignores_matches_inside_ids(repo) -> None:
    t = GraphTools(repo, None, FakeEmbedder())
    assert repo.search("ontology", 50)  # every Fabric asset id contains the lakehouse name "Ontology_Lineage"
    out = t.call("find_assets", {"query": "ontology"})
    assert out == {"assets": [], "count": 0, "total_matches": 0}
    risk = t.call("find_assets", {"query": "risk_bucket"})
    assert risk["count"] >= 1 and risk["total_matches"] >= risk["count"]
    capped = t.call("find_assets", {"query": "a", "limit": 3})
    assert capped["count"] == 3 and capped["total_matches"] > 3  # the model is told when it saw only a slice


def test_show_graph_ontology_covers_terms_tables_and_flows() -> None:
    t = GraphTools(onto_repo(), None, FakeEmbedder(), allow_graph=True)
    res = t.call("show_graph", {"kind": "ontology"})
    g = res["_graph"]
    assert g["kind"] == "ontology" and g["cypher"] is None  # no single query draws it
    labels = {n["label"] for n in g["nodes"]}
    assert {"BusinessConcept", "Table"} <= labels
    assert {e["type"] for e in g["edges"]} == {"REPRESENTS", "RELATED_TO", "FLOWS_TO"}
    flow = next(e for e in g["edges"] if e["type"] == "FLOWS_TO" and e["target"] == "t:dimb")
    assert flow["source"] == "t:accounts" and flow["properties"]["pipelines"] == ["PL_BRANCHES"]
    one = t.call("show_graph", {"kind": "ontology", "asset": "Loan"})
    assert one["_graph"]["title"] == "Ontology term Loan"
    assert {n["label"] for n in one["_graph"]["nodes"]} == {"BusinessConcept", "Table"}
    assert {n["id"] for n in one["_graph"]["nodes"] if n["label"] == "BusinessConcept"} == {"concept:Loan"}
    assert "No ontology term" in t.call("show_graph", {"kind": "ontology", "asset": "zzz"})["error"]


def test_prompt_steers_ontology_questions_and_is_honest_about_graphs() -> None:
    assert "get_ontology" in SYSTEM_PROMPT and 'Never search for the word "ontology"' in SYSTEM_PROMPT
    assert "Never say you cannot draw graphs" in SYSTEM_PROMPT
    assert 'never offer to "try" or "attempt"' in SYSTEM_PROMPT


def test_api_ontology_question_uses_the_ontology_tool(repo) -> None:
    llm = ScriptedLLM(ask(call("get_ontology")), say("Six ontology terms."))
    with make_client(repo, llm) as c:
        r = c.post("/api/chat", json={"messages": [{"role": "user", "content": "i want to see the lineage of all ontology terms"}]}).json()
    assert r["steps"][0]["tool"] == "get_ontology" and r["steps"][0]["summary"] == "6 results"
    # "i want to see the lineage" asks for a picture, so the graph tool is offered next to the ontology tool
    # (this scripted model chose not to draw, so no graph came back).
    offered = [t["function"]["name"] for t in llm.sent[0][1]]
    assert "show_graph" in offered and "get_ontology" in offered and r["graph"] is None


def test_api_lineage_question_without_show_or_see_gets_no_graph_tool(repo) -> None:
    llm = ScriptedLLM(say("Derived from loans."))
    with make_client(repo, llm) as c:
        r = c.post("/api/chat", json={"messages": [{"role": "user", "content": "What is the lineage of fact.loan_portfolio.risk_bucket?"}]}).json()
    assert r["graph"] is None and "show_graph" not in [t["function"]["name"] for t in llm.sent[0][1]]
