"""Policy documents: search, lineage overlay (GOVERNS) and starting lineage from a policy."""

import pytest
from fastapi.testclient import TestClient

from app import cypher, lineage
from app.config import Settings
from app.graphmodel import GEdge, GNode, SubGraph, node_key
from app.main import create_app
from app.snapshot import SnapshotRepository


from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOANS = "pg:bankdb.banking.loans"
PORTFOLIO = "fabric:Ontology_Lineage.fact.loan_portfolio"
RISK = "fabric:Ontology_Lineage.fact.loan_portfolio.risk_bucket"
DOC = "demobank-loan-policy"


@pytest.fixture(scope="module")
def policy_repo(demo_data) -> SnapshotRepository:
    nodes, edges = demo_data
    nodes = [
        *nodes,
        {"id": DOC, "label": "PolicyDocument", "doc_id": DOC, "title": "DemoBank Retail Loan Policy"},
        {"id": "c1", "label": "PolicyChunk", "chunk_id": "c1", "heading": "3.1 Loan identification and terms"},
        {"id": "c2", "label": "PolicyChunk", "chunk_id": "c2", "heading": "3.5 Credit risk scoring"},
    ]
    edges = [
        *edges,
        {"from": DOC, "rel": "HAS_CHUNK", "to": "c1"},
        {"from": DOC, "rel": "HAS_CHUNK", "to": "c2"},
        {"from": "c1", "rel": "GOVERNS", "to": LOANS, "score": 0.91, "method": "similarity_auto"},
        {"from": "c2", "rel": "GOVERNS", "to": RISK, "score": 0.8, "method": "similarity_auto"},
    ]
    return SnapshotRepository(nodes, edges)


@pytest.fixture()
def pclient(policy_repo) -> TestClient:
    app = create_app(Settings(mode="snapshot", static_dir=ROOT / "__no_frontend__"), repo=policy_repo)
    with TestClient(app) as c:
        yield c


def test_node_key_prefers_business_keys() -> None:
    assert node_key({"id": "a", "doc_id": "b"}, "4:x:1") == "a"
    assert node_key({"doc_id": "b"}, "4:x:1") == "b"
    assert node_key({"chunk_id": "c"}, "4:x:1") == "c"
    assert node_key({}, "4:x:1") == "4:x:1"


def test_search_and_resolve_policy_documents(pclient, policy_repo) -> None:
    rows = pclient.get("/api/search", params={"q": "loan policy"}).json()["rows"]
    assert rows[0]["id"] == DOC and rows[0]["label"] == "PolicyDocument"
    assert rows[0]["caption"] == "DemoBank Retail Loan Policy"
    assert policy_repo.resolve(DOC).label == "PolicyDocument"


def test_attach_policies_collapses_chunks_and_rolls_up_columns() -> None:
    view = SubGraph()
    view.add_node(GNode("t", ["Table"]))
    view.add_node(GNode("t.a", ["Column"]))
    pol = SubGraph()
    for n in (GNode("d", ["PolicyDocument"], {"title": "P"}), GNode("c1", ["PolicyChunk"], {"heading": "S1"}),
              GNode("c2", ["PolicyChunk"], {"heading": "S2"}), GNode("t", ["Table"]),
              GNode("t.a", ["Column"], {"name": "a"})):
        pol.add_node(n)
    pol.add_edge(GEdge("h1", "d", "c1", "HAS_CHUNK"))
    pol.add_edge(GEdge("h2", "d", "c2", "HAS_CHUNK"))
    pol.add_edge(GEdge("g1", "c1", "t", "GOVERNS", {"score": 0.7}))
    pol.add_edge(GEdge("g2", "c2", "t.a", "GOVERNS", {"score": 0.9}))

    assert lineage.attach_policies(view, pol, owners={"t.a": "t"}) == 1
    gov = [e for e in view.edges.values() if e.type == "GOVERNS"]
    assert len(gov) == 1  # both sections, one edge to the table
    assert gov[0].source == "d" and gov[0].target == "t"
    assert gov[0].props["sections"] == ["S1", "S2"] and gov[0].props["score"] == 0.9
    assert "PolicyChunk" not in {n.label for n in view.nodes.values()}

    # A filter on another document adds nothing.
    view2 = SubGraph()
    view2.add_node(GNode("t", ["Table"]))
    assert lineage.attach_policies(view2, pol, only_doc="other") == 0


@pytest.mark.parametrize("level", ["column", "table"])
def test_concept_lineage_includes_governing_policy(pclient, level) -> None:
    res = pclient.get("/api/lineage", params={"id": "concept:Loan", "level": level}).json()
    assert res["summary"]["policies"] == 1
    gov = [e for e in res["edges"] if e["type"] == "GOVERNS"]
    assert {e["source"] for e in gov} == {DOC}
    assert LOANS in {e["target"] for e in gov}
    off = pclient.get("/api/lineage", params={"id": "concept:Loan", "level": level, "policies": "false"}).json()
    assert not [e for e in off["edges"] if e["type"] == "GOVERNS"]


def test_lineage_can_start_from_a_policy_document(pclient) -> None:
    res = pclient.get("/api/lineage", params={"id": DOC, "level": "table"}).json()
    assert res["start"]["label"] == "PolicyDocument"
    labels = {n["label"] for n in res["nodes"]}
    assert "PolicyChunk" not in labels
    targets = {e["target"] for e in res["edges"] if e["type"] == "GOVERNS"}
    assert {LOANS, PORTFOLIO} <= targets  # the risk_bucket column rolls up to its table
    assert any(e["type"] == "FLOWS_TO" for e in res["edges"])


def test_policy_cypher_uses_doc_id() -> None:
    q = cypher.lineage("PolicyDocument", "both", 3)
    assert "(start:PolicyDocument {doc_id: $id})" in q
    assert f"[:HAS_CHUNK]->(:PolicyChunk)-{cypher.GOVERNS_ACTIVE}->" in q
    assert "WHERE x.id IN $ids" in cypher.GOVERNING_POLICIES
    # Only accepted links (or links from before statuses existed) are ever read.
    assert "coalesce(g.status, 'accepted') = 'accepted'" in cypher.GOVERNING_POLICIES
