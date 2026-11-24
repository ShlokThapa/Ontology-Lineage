"""Fixed, read-only tools the chatbot can call.

The model never writes Cypher. Every tool is a reviewed query (or a call into the same repository the
lineage pages use) with bound parameters, run in a READ transaction, and every result is trimmed to a few
plain fields: no embeddings, no raw node properties, and long text is clipped.
"""

from __future__ import annotations

import inspect
import logging
from collections import Counter
from typing import Any, Callable, cast

from .. import cypher, lineage
from ..cypher import Direction, cypher_literal, inline_params
from ..graphmodel import GEdge, GNode, SubGraph, to_view
from ..neo4j_client import Neo4jClient
from ..repository import Repository
from .embeddings import EmbeddingUnavailable, QueryEmbedder
from .graph import GRAPH_KINDS, clean_view
from .lakehouse import LakehouseData, LakehouseError
from .quality import build_report, summary_for_model

log = logging.getLogger(__name__)

POLICY_INDEX = "policy_chunk_embedding"

# Result-size budgets: they bound the tokens one tool call can add to the conversation.
MAX_SECTION_CHARS = 1500
MAX_SNIPPET_CHARS = 400
MAX_ROWS = 60

_NO_POLICY_IN_SNAPSHOT = (
    "Policy documents are only available from the live Neo4j database, and this server is in offline snapshot "
    "mode. Tell the user in one sentence that policy questions can't be answered right now; do not offer to "
    "retry or list options. Answer any other part of the question with the other tools."
)

# --- Cypher -----------------------------------------------------------------------------------------

POLICY_VECTOR_SEARCH = """
CALL db.index.vector.queryNodes($index, $k, $vector) YIELD node, score
MATCH (d:PolicyDocument)-[:HAS_CHUNK]->(node)
RETURN d.doc_id AS doc_id, d.title AS doc_title, node.chunk_id AS chunk_id,
       node.section_path AS section_path, node.heading AS heading, node.text AS text, score
ORDER BY score DESC
"""

POLICY_SECTION = """
MATCH (d:PolicyDocument)-[:HAS_CHUNK]->(c:PolicyChunk {chunk_id: $chunk_id})
OPTIONAL MATCH (c)-[g:GOVERNS]->(a:Asset) WHERE coalesce(g.status, 'accepted') = 'accepted'
WITH d, c, collect(CASE WHEN a IS NULL THEN null ELSE
       {name: a.name, table: a.table, full_name: a.full_name, term: g.term, method: g.method} END) AS governs
RETURN d.doc_id AS doc_id, d.title AS doc_title, c.chunk_id AS chunk_id, c.section_path AS section_path,
       c.heading AS heading, c.text AS text, governs[0..30] AS governs, size(governs) AS governs_total
"""

POLICIES_FOR_ASSET = """
MATCH (a:Asset)
WHERE a.id = $id OR EXISTS { (:Asset {id: $id})-[:HAS_COLUMN]->(a) }
MATCH (d:PolicyDocument)-[:HAS_CHUNK]->(c:PolicyChunk)-[g:GOVERNS]->(a)
WHERE coalesce(g.status, 'accepted') = 'accepted'
RETURN a.name AS asset, a.table AS asset_table, d.doc_id AS doc_id, d.title AS doc_title,
       c.chunk_id AS chunk_id, c.section_path AS section_path, c.heading AS heading, c.text AS text,
       g.term AS term, g.method AS method
ORDER BY d.title, c.section_path
LIMIT $limit
"""

POLICY_DOCUMENTS = """
MATCH (d:PolicyDocument)
OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:PolicyChunk)
RETURN d.doc_id AS doc_id, d.title AS title, d.source_file AS source_file, count(c) AS sections
ORDER BY d.title
"""


# --- helpers ----------------------------------------------------------------------------------------


def _clip(text: Any, limit: int) -> str:
    s = "" if text is None else str(text).strip()
    return s if len(s) <= limit else s[:limit].rstrip() + " …[truncated]"


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _brief(n: GNode) -> dict[str, Any]:
    """The only node fields the model ever sees (never the raw property map, which holds embeddings)."""
    p = n.props
    out: dict[str, Any] = {"id": n.id, "kind": n.label, "name": n.name}
    for key in ("full_name", "table", "data_type", "platform", "comment", "description"):
        if p.get(key) not in (None, ""):
            out[key] = _clip(p[key], 200)
    if p.get("is_pii") is True:
        out["is_pii"] = True
    return out


def _col_label(n: GNode | None, fallback: str) -> str:
    if n is None:
        return fallback
    table = n.props.get("table")
    return f"{table}.{n.name}" if table else n.name


def _section(row: dict[str, Any], limit: int = MAX_SECTION_CHARS) -> dict[str, Any]:
    return {
        "doc_id": row.get("doc_id"),
        "doc_title": row.get("doc_title"),
        "chunk_id": row.get("chunk_id"),
        "section_path": row.get("section_path") or row.get("heading"),
        "heading": row.get("heading"),
        "text": _clip(row.get("text"), limit),
    }


# --- tool specs -------------------------------------------------------------------------------------


def _spec(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required or []},
        },
    }


_ASSET = {"type": "string", "description": "Table, view or column name, e.g. 'dim.account' or 'loan_portfolio.risk_bucket'."}

TOOL_SPECS: list[dict[str, Any]] = [
    _spec(
        "search_policies",
        "Semantic search over the text of the bank's policy documents. Use it for any question about what a "
        "policy requires, allows or defines. Returns the best-matching policy sections with their text.",
        {
            "query": {"type": "string", "description": "What to look for, in natural language."},
            "top_k": {"type": "integer", "description": "How many sections to return (1-10, default 5)."},
        },
        ["query"],
    ),
    _spec(
        "get_policy_section",
        "Read one policy section in full by its chunk_id, and list the data assets the graph links to it.",
        {"chunk_id": {"type": "string", "description": "chunk_id from a previous policy tool result."}},
        ["chunk_id"],
    ),
    _spec(
        "get_policies_for_asset",
        "Policy sections the graph links (GOVERNS) to a table, view or column. For a table this includes "
        "its columns. These links come from an earlier automated mapping and may be incomplete.",
        {"asset": _ASSET},
        ["asset"],
    ),
    _spec("list_policy_documents", "List the policy documents in the graph with their section counts.", {}),
    _spec(
        "find_assets",
        "Find tables, views, columns, pipelines and business concepts by (part of) their name. To see the "
        "ontology's terms and their lineage use get_ontology, not this.",
        {
            "query": {"type": "string", "description": "Name or fragment to search for."},
            "limit": {"type": "integer", "description": "Max results (1-25, default 10)."},
        },
        ["query"],
    ),
    _spec(
        "describe_table",
        "Columns of a table or view (type, PII flag, classification) and the business concepts it represents.",
        {"asset": _ASSET},
        ["asset"],
    ),
    _spec(
        "get_lineage",
        "Column-level data flows (FLOWS_TO) upstream and/or downstream of a table, view or column, with "
        "the transformation and pipeline of each flow.",
        {
            "asset": _ASSET,
            "direction": {"type": "string", "enum": ["upstream", "downstream", "both"], "description": "Default both."},
            "depth": {"type": "integer", "description": "Hops to follow (1-6, default 3)."},
        },
        ["asset"],
    ),
    _spec(
        "get_impact",
        "Everything downstream of a table or column: affected tables, columns, pipelines, and the flows.",
        {"asset": _ASSET},
        ["asset"],
    ),
    _spec(
        "list_pii_columns",
        "Every PII source column, how it is treated on its way to Fabric (DIRECT / CAST / MASKED) and its "
        "status (OK, VIOLATION when moved unmasked, NOT MOVED).",
        {},
    ),
    _spec(
        "list_pipelines",
        "Pipelines and their activities, with the tables each activity reads from and writes to.",
        {"pipeline": {"type": "string", "description": "Optional: only pipelines whose name contains this text."}},
    ),
    _spec(
        "get_ontology",
        "The business ontology: its terms (business concepts such as Customer, Loan, Branch), what each term "
        "means, how terms relate, which tables represent each term, and the data flows between those tables "
        "(the lineage of the term). Use it for any question about ontology terms or business concepts.",
        {
            "term": {"type": "string", "description": "Optional: one ontology term, e.g. 'Branch'. Omit for all terms."},
            "include_lineage": {"type": "boolean", "description": "Include the data flows between the tables (default true)."},
        },
    ),
    _spec(
        "show_graph",
        "Draw a graph for the user. Only available because the user directly asked to see a graph, diagram "
        "or visual. The graph is fetched from Neo4j and shown under your answer; you receive only its size. "
        "Pick the kind that matches the question: lineage (data flow into/out of an asset), impact (everything "
        "downstream), policies (policy sections linked to an asset), pii (PII columns and where they go), "
        "pipelines (pipelines, activities and the tables they touch), neighbors (everything directly connected), "
        "ontology (the business terms, the tables representing them and the data flows between those tables; "
        "pass a term as asset to show just that one).",
        {
            "kind": {"type": "string", "enum": list(GRAPH_KINDS)},
            "asset": {
                "type": "string",
                "description": "Table, view or column name. Required for lineage, impact, policies and neighbors; "
                "for ontology an optional term name.",
            },
            "direction": {"type": "string", "enum": ["upstream", "downstream", "both"], "description": "Lineage only; default both."},
            "depth": {"type": "integer", "description": "Lineage only: hops to follow (1-6, default 3)."},
        },
        ["kind"],
    ),
    _spec(
        "list_data_tables",
        "The bank's data tables in the lakehouse (customers, accounts, loans, monthly transactions): each table's "
        "name (schema.table), how many rows it holds, and its columns with their types. Use it to see what data "
        "exists before querying it. Personal fields are masked in the data.",
        {},
    ),
    _spec(
        "query_data",
        "Run one read-only SELECT over the lakehouse tables and get the rows back (at most 60). Use it for exact "
        "numbers: counts, sums, averages, groupings, comparisons, and finding the rows behind a data problem. "
        "Tables are written schema.table, for example fact.loan_portfolio or dim.Customer. SQL dialect: DuckDB "
        "(use LIMIT, not TOP; join strings with ||). Never list individual customers' personal details; summarise.",
        {"sql": {"type": "string", "description": "A single SELECT (or WITH ... SELECT) statement."}},
        ["sql"],
    ),
    _spec(
        "data_quality_report",
        "Run the data quality checks (completeness, uniqueness, validity, consistency, timeliness) over the "
        "lakehouse tables and show the user a scorecard under your answer. Only available when the user is asking "
        "about the quality or health of the data. You receive the headline numbers and the worst findings.",
        {"table": {"type": "string", "description": "Optional: one table (schema.table). Omit to check every table."}},
    ),
]

GraphParts = tuple[str, dict[str, list[dict[str, Any]]], "str | None"]  # (title, view, cypher)

GRAPH_TOOL = "show_graph"
GRAPH_RESULT_KEY = "_graph"  # carries the full graph out of a tool result; never sent to the model
QUALITY_TOOL = "data_quality_report"
DASHBOARD_RESULT_KEY = "_dashboard"  # the full scorecard for the browser; the model only gets a summary


# --- tool implementations ---------------------------------------------------------------------------


class GraphTools:
    def __init__(
        self,
        repo: Repository,
        client: Neo4jClient | None,
        embedder: QueryEmbedder | None,
        allow_graph: bool = False,
        data: LakehouseData | None = None,
        allow_quality: bool = False,
    ):
        self.repo = repo
        self.client = client
        self.embedder = embedder
        self.data = data
        # data_quality_report is offered only for a turn that is about data quality (see quality.wants_quality).
        self.allow_quality = allow_quality
        # show_graph exists only for a turn in which the user directly asked for a graph (see chat/graph.py).
        self.allow_graph = allow_graph
        self._handlers: dict[str, Callable[..., dict[str, Any]]] = {
            GRAPH_TOOL: self.show_graph,
            "search_policies": self.search_policies,
            "get_policy_section": self.get_policy_section,
            "get_policies_for_asset": self.get_policies_for_asset,
            "list_policy_documents": self.list_policy_documents,
            "find_assets": self.find_assets,
            "describe_table": self.describe_table,
            "get_ontology": self.get_ontology,
            "get_lineage": self.get_lineage,
            "get_impact": self.get_impact,
            "list_pii_columns": self.list_pii_columns,
            "list_pipelines": self.list_pipelines,
            "list_data_tables": self.list_data_tables,
            "query_data": self.query_data,
            QUALITY_TOOL: self.data_quality_report,
        }

    @property
    def specs(self) -> list[dict[str, Any]]:
        hidden = set()
        if not self.allow_graph:
            hidden.add(GRAPH_TOOL)
        if not self.allow_quality:
            hidden.add(QUALITY_TOOL)
        return [s for s in TOOL_SPECS if s["function"]["name"] not in hidden]

    @property
    def vector_search_ready(self) -> bool:
        return self.client is not None and self.embedder is not None and self.embedder.is_ready()

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run a tool. Problems the model can recover from come back as {"error": ...}; an unreachable
        database propagates so the API can answer 503 instead of letting the model improvise."""
        arguments = arguments if isinstance(arguments, dict) else {}
        handler = self._handlers.get(name)
        if handler is None:
            return {"error": f"Unknown tool '{name}'."}
        try:
            inspect.signature(handler).bind(**arguments)  # reject wrong/unknown arguments up front
        except TypeError:
            return {"error": f"Bad arguments for {name}."}
        try:
            return handler(**arguments)
        except EmbeddingUnavailable as exc:
            return {"error": f"Semantic search is not set up on this server: {exc}"}
        except _ToolError as exc:
            return {"error": str(exc)}
        except LakehouseError as exc:
            return {"error": f"The data tables are not available right now: {exc}"}

    # -- shared -----------------------------------------------------------------------------------

    def _live(self) -> Neo4jClient:
        if self.client is None:
            raise _ToolError(_NO_POLICY_IN_SNAPSHOT)
        return self.client

    def _resolve(self, ref: str) -> GNode:
        ref = (ref or "").strip()
        if not ref:
            raise _ToolError("An asset name is required.")
        node = self.repo.resolve(ref)
        if node is None:
            # resolve() matches ids, full names and dotted suffixes only; a bare pipeline or concept name
            # is found by search, so accept a single exact-name hit instead of suggesting the same name back.
            hits = self.repo.search(ref, 5)
            exact = [n for n in hits if ref.lower() in (n.name.lower(), str(n.props.get("full_name") or "").lower())]
            if len(exact) == 1:
                return exact[0]
            hints = [n.props.get("full_name") or n.name for n in hits]
            raise _ToolError(f"No asset found for '{ref}'." + (f" Did you mean: {', '.join(map(str, hints))}?" if hints else ""))
        return node

    # -- lakehouse data ---------------------------------------------------------------------------

    def _snapshot(self) -> Any:
        if self.data is None or not self.data.configured:
            raise _ToolError("The lakehouse data is not connected on this server.")
        return self.data.snapshot()

    def list_data_tables(self) -> dict[str, Any]:
        snap = self._snapshot()
        tables = [
            {
                "table": t.full_name, "rows": t.rows,
                "columns": [{"name": n, "type": ty} for n, ty in t.columns],
                **({"note": f"only the first {t.loaded} rows are loaded"} if t.sampled else {}),
            }
            for t in snap.tables
        ]
        return {"tables": tables, "count": len(tables), "as_of": snap.as_of.isoformat(timespec="seconds")}

    def query_data(self, sql: str) -> dict[str, Any]:
        res = self._snapshot().query(str(sql))
        return {**res, "row_count": len(res["rows"])}

    def data_quality_report(self, table: str = "") -> dict[str, Any]:
        if not self.allow_quality:
            raise _ToolError("A data quality scorecard is only built when the user asks about data quality.")
        snap = self._snapshot()
        try:
            report = build_report(snap, (table or "").strip() or None)
        except ValueError as exc:
            raise _ToolError(str(exc)) from exc
        return {**summary_for_model(report), DASHBOARD_RESULT_KEY: report}

    # -- policies ---------------------------------------------------------------------------------

    def search_policies(self, query: str, top_k: int = 5) -> dict[str, Any]:
        client = self._live()
        if self.embedder is None:
            raise _ToolError("Semantic search is not configured.")
        vector = self.embedder.embed_query((query or "").strip()[:1000])
        res = client.run_read(
            POLICY_VECTOR_SEARCH,
            {"index": POLICY_INDEX, "k": _int(top_k, 5, 1, 10), "vector": vector},
            max_rows=10,
        )
        sections = [{**_section(r), "score": round(float(r["score"]), 3)} for r in res.records]
        return {"sections": sections, "count": len(sections)}

    def get_policy_section(self, chunk_id: str) -> dict[str, Any]:
        res = self._live().run_read(POLICY_SECTION, {"chunk_id": str(chunk_id)}, max_rows=1)
        if not res.records:
            raise _ToolError(f"No policy section with chunk_id '{chunk_id}'.")
        r = res.records[0]
        governs = [
            {k: v for k, v in g.items() if v not in (None, "")} for g in (r.get("governs") or []) if g
        ]
        return {
            "sections": [_section(r, limit=6000)],
            "linked_assets": governs,
            "linked_assets_total": r.get("governs_total", len(governs)),
        }

    def get_policies_for_asset(self, asset: str) -> dict[str, Any]:
        node = self._resolve(asset)
        res = self._live().run_read(POLICIES_FOR_ASSET, {"id": node.id, "limit": MAX_ROWS}, max_rows=MAX_ROWS)
        sections = []
        for r in res.records:
            s = _section(r, limit=MAX_SNIPPET_CHARS)
            s["asset"] = f"{r['asset_table']}.{r['asset']}" if r.get("asset_table") else r.get("asset")
            s["matched_term"] = r.get("term")
            s["method"] = r.get("method")
            sections.append(s)
        return {"asset": _brief(node), "sections": sections, "count": len(sections)}

    def list_policy_documents(self) -> dict[str, Any]:
        res = self._live().run_read(POLICY_DOCUMENTS, max_rows=100)
        return {"documents": res.records, "count": len(res.records)}

    # -- lineage and metadata ---------------------------------------------------------------------

    def find_assets(self, query: str, limit: int = 10) -> dict[str, Any]:
        if not (query or "").strip():
            raise _ToolError("A search text is required.")
        needle = query.strip().lower()
        # repo.search also matches inside ids, so "ontology" hits every asset of the lakehouse called
        # Ontology_Lineage. Keep only assets whose own name (or description) contains the text.
        named = [n for n in self.repo.search(query.strip(), 200) if needle in _searchable(n)]
        shown = named[: _int(limit, 10, 1, 25)]
        return {"assets": [_brief(n) for n in shown], "count": len(shown), "total_matches": len(named)}

    def describe_table(self, asset: str) -> dict[str, Any]:
        node = self._resolve(asset)
        if node.label not in ("Table", "View"):
            raise _ToolError(f"'{asset}' is a {node.label}, not a table or view.")
        sub = self.repo.asset_neighbors(node.id, 500)
        # Classification nodes sit two hops from a table, so read them from the PII view of the graph.
        pii_sub = self.repo.pii()
        classified: dict[str, list[str]] = {}
        for e in pii_sub.edges_of("CLASSIFIED_AS"):
            tgt = pii_sub.nodes.get(e.target)
            classified.setdefault(e.source, []).append(tgt.name if tgt else e.target)
        columns = []
        for e in sub.edges_of("HAS_COLUMN"):
            if e.source != node.id or e.target not in sub.nodes:
                continue
            col = {k: v for k, v in _brief(sub.nodes[e.target]).items() if k not in ("id", "table", "kind")}
            if e.target in classified:
                col["classification"] = ", ".join(sorted(classified[e.target]))
            columns.append(col)
        columns.sort(key=lambda c: c["name"])
        concepts = sorted(
            sub.nodes[e.target].name
            for e in sub.edges_of("REPRESENTS")
            if e.source == node.id and e.target in sub.nodes
        )
        return {"table": _brief(node), "columns": columns[:MAX_ROWS], "column_count": len(columns), "represents": concepts}

    def get_lineage(self, asset: str, direction: str = "both", depth: int = 3) -> dict[str, Any]:
        node = self._resolve(asset)
        if direction not in ("upstream", "downstream", "both"):
            direction = "both"
        sub = self.repo.lineage(node, direction, _int(depth, 3, 1, 6))
        owners = sub.owner_map()
        flows = []
        for e in sub.edges_of("FLOWS_TO"):
            src, tgt = sub.nodes.get(e.source), sub.nodes.get(e.target)
            flows.append(
                {
                    "from": _col_label(src, e.source),
                    "to": _col_label(tgt, e.target),
                    "transformation": e.props.get("transformation") or "DIRECT",
                    "pipeline": e.props.get("pipeline"),
                    "expression": _clip(e.props.get("expression"), 160) or None,
                }
            )
        flows.sort(key=lambda f: (f["from"], f["to"]))
        tables = sorted({_table_of(sub, n, owners) for n in sub.nodes.values() if n.label in ("Column",)} - {None})
        return {
            "start": _brief(node),
            "direction": direction,
            "flows": flows[:MAX_ROWS],
            "flow_count": len(flows),
            "tables_involved": tables[:MAX_ROWS],
        }

    def get_impact(self, asset: str) -> dict[str, Any]:
        node = self._resolve(asset)
        sub = self.repo.lineage(node, "downstream", 6)
        rows, summary = lineage.impact(sub, node)
        flows = [
            {
                "depth": r["depth"],
                "from": f"{r['source_table']}.{r['source_column']}",
                "to": f"{r['target_table']}.{r['target_column']}",
                "transformation": r["transformation"] or "DIRECT",
                "pipeline": r["pipeline"],
            }
            for r in rows[:MAX_ROWS]
        ]
        return {
            "start": _brief(node),
            "summary": summary["sentence"],
            "impacted_tables": summary["table_names"],
            "impacted_pipelines": summary["pipeline_names"],
            "flows": flows,
            "flow_count": len(rows),
        }

    def list_pii_columns(self) -> dict[str, Any]:
        rows, summary = lineage.pii(self.repo.pii())
        out = [
            {
                "column": f"{r['source_table']}.{r['source_column']}",
                "classification": r["classification"],
                "treatment": r["treatment"],
                "target": f"{r['target_table']}.{r['target_column']}" if r.get("target_column") else None,
                "pipeline": r.get("pipeline"),
                "status": r["status"],
            }
            for r in rows[:MAX_ROWS]
        ]
        return {"summary": summary, "rows": out, "row_count": len(rows)}

    def list_pipelines(self, pipeline: str = "") -> dict[str, Any]:
        rows, summary = lineage.pipelines(self.repo.pipelines())
        needle = (pipeline or "").strip().lower()
        out = [
            {
                "pipeline": r["pipeline"],
                "activity": r["activity"],
                "type": r["activity_type"],
                "reads_from": r["reads_from"],
                "writes_to": r["writes_to"],
            }
            for r in rows
            if not needle or needle in r["pipeline"].lower()
        ]
        return {"summary": summary, "activities": out[:MAX_ROWS], "activity_count": len(out)}

    # -- ontology (business concepts) -------------------------------------------------------------

    def _ontology(self, term: str = "") -> dict[str, Any]:
        """The business concepts, the assets representing them, and the table-to-table data flows among
        those assets. Shared by get_ontology (text) and the 'ontology' graph."""
        sub = self.repo.concepts(True)
        every = {n.id: n for n in sub.nodes.values() if n.label == "BusinessConcept"}
        if not every:
            raise _ToolError("The graph has no business concepts (ontology terms).")
        represented: dict[str, list[GNode]] = {cid: [] for cid in every}
        for e in sub.edges_of("REPRESENTS"):
            asset = sub.nodes.get(e.source)
            if asset is not None and e.target in represented:
                represented[e.target].append(asset)
        # An asset can represent several terms (e.g. a loan portfolio table: Loan and CreditRisk).
        concept_of: dict[str, set[str]] = {}
        for cid, assets in represented.items():
            for a in assets:
                concept_of.setdefault(a.id, set()).add(cid)

        shown = dict(every)
        needle = (term or "").strip().lower()
        if needle:
            shown = {cid: c for cid, c in every.items() if needle in c.name.lower()}
            if not shown:
                names = ", ".join(sorted(c.name for c in every.values()))
                raise _ToolError(f"No ontology term matches '{term}'. The terms are: {names}.")

        # Table-level flows between the represented tables, found by following each one downstream.
        flows: dict[tuple[str, str], dict[str, Any]] = {}
        starts = [a for assets in represented.values() for a in assets if a.label in ("Table", "View")]
        for start in {a.id: a for a in starts}.values():
            tsub, _ = lineage.table_level(self.repo.lineage(start, "downstream", 3), start)
            for e in tsub.edges_of("FLOWS_TO"):
                if e.source in concept_of and e.target in concept_of:
                    flows.setdefault((e.source, e.target), {"edge": e, "nodes": tsub.nodes})
        return {
            "every": every, "shown": shown, "represented": represented, "concept_of": concept_of,
            "flows": flows, "related": sub.edges_of("RELATED_TO"),
        }

    def get_ontology(self, term: str = "", include_lineage: bool = True) -> dict[str, Any]:
        m = self._ontology(term)
        every, concept_of = m["every"], m["concept_of"]
        names = {cid: c.name for cid, c in every.items()}
        assets: dict[str, GNode] = {}
        for lst in m["represented"].values():
            assets.update({a.id: a for a in lst})

        def terms_of(asset_id: str) -> str:
            return ", ".join(sorted(names[c] for c in concept_of[asset_id]))

        out = []
        for cid, c in sorted(m["shown"].items(), key=lambda kv: kv[1].name):
            mine = sorted(m["represented"][cid], key=lambda a: _graph_name(a))
            mine_ids = {a.id for a in mine}
            entry: dict[str, Any] = {
                "term": c.name,
                "description": c.props.get("description"),
                "represented_by": [f"{_graph_name(a)} ({a.props.get('platform') or 'platform unknown'})" for a in mine],
                "related_to": [
                    {"term": names.get(e.target, e.target), "via": e.props.get("via")}
                    for e in m["related"] if e.source == cid
                ],
                "referenced_by": [
                    {"term": names.get(e.source, e.source), "via": e.props.get("via")}
                    for e in m["related"] if e.target == cid
                ],
            }
            if include_lineage:
                flows, notes = [], []
                for (s, t), f in sorted(m["flows"].items(), key=lambda kv: (_graph_name(assets[kv[0][0]]), _graph_name(assets[kv[0][1]]))):
                    if s in mine_ids or t in mine_ids:
                        props = f["edge"].props
                        flows.append({
                            "from": _graph_name(assets[s]), "from_term": terms_of(s),
                            "to": _graph_name(assets[t]), "to_term": terms_of(t),
                            "pipelines": props.get("pipelines") or [],
                            "column_flows": props.get("count"),
                            "treatment": props.get("label") or props.get("transformation"),
                        })
                entry["data_flows"] = flows
                # A table that receives data, but never from a table representing the same term while another
                # table of that term sits unused as a source, is worth a look (e.g. a "branch" table fed only
                # from the accounts table, while the branches table feeds nothing).
                targets = {t for (_, t) in m["flows"]}
                for a in mine:
                    sources = {s for (s, t) in m["flows"] if t == a.id}
                    idle = [x for x in mine if x.id != a.id and x.id not in targets]
                    if sources and idle and not any(cid in concept_of[s] for s in sources):
                        notes.append(
                            f"{_graph_name(a)} represents {c.name} but receives data only from "
                            + ", ".join(sorted(f"{_graph_name(assets[s])} ({terms_of(s)})" for s in sources))
                            + f"; nothing flows into it from {', '.join(sorted(_graph_name(x) for x in idle))}, "
                            f"which also represent{'s' if len(idle) == 1 else ''} {c.name}."
                        )
                entry["lineage_notes"] = notes
            out.append(entry)
        return {"terms": out, "count": len(out)}

    # -- graph display ----------------------------------------------------------------------------

    def show_graph(self, kind: str, asset: str = "", direction: str = "both", depth: int = 3) -> dict[str, Any]:
        """Fetch a graph from Neo4j for the UI to draw. The model gets a summary; the full graph rides in
        ``_graph`` (stripped by the agent before the result goes back to the model)."""
        if not self.allow_graph:
            raise _ToolError("A graph is only drawn when the user directly asks for one.")
        if kind not in GRAPH_KINDS:
            raise _ToolError(f"Unknown graph kind '{kind}'. Use one of: {', '.join(GRAPH_KINDS)}.")
        if kind in ("lineage", "impact", "policies", "neighbors") and not (asset or "").strip():
            raise _ToolError(f"A '{kind}' graph needs an asset name.")
        title, view, query = getattr(self, f"_graph_{kind}")(asset, direction, depth)
        cleaned, truncated = clean_view(view)
        if not cleaned["nodes"]:
            raise _ToolError("There is nothing to draw for that request.")
        kinds = Counter(n["label"] for n in cleaned["nodes"])
        return {
            "graph_displayed": True,
            "title": title,
            "nodes": len(cleaned["nodes"]),
            "edges": len(cleaned["edges"]),
            "node_kinds": dict(kinds),
            "truncated": truncated,
            "note": "The graph is already shown to the user under your answer. Reply in at most two sentences "
            "saying what it shows. Do not list its nodes or edges, and do not call more tools to describe it "
            "unless the user also asked a separate question.",
            GRAPH_RESULT_KEY: {
                "kind": kind,
                "title": title,
                "nodes": cleaned["nodes"],
                "edges": cleaned["edges"],
                "cypher": query,
                "truncated": truncated,
            },
        }

    def _lineage_view(
        self, asset: str, direction: str, depth: int
    ) -> tuple[dict[str, list[dict[str, Any]]], str, str]:
        node = self._resolve(asset)
        if node.label not in ("Column", "Table", "View"):
            raise _ToolError(f"A lineage graph needs a table, view or column, but '{asset}' is a {node.label}.")
        sub = self.repo.lineage(node, cast(Direction, direction), depth)
        if node.label == "Column":
            view_sub, start_ids = lineage.column_level(sub, node), {node.id}
        else:
            view_sub, start_ids = lineage.table_level(sub, node)
        query = inline_params(cypher.lineage(node.label, cast(Direction, direction), depth), {"id": node.id})
        return to_view(view_sub, start_ids=start_ids), query, _graph_name(node)

    def _graph_lineage(self, asset: str, direction: str, depth: int) -> GraphParts:
        direction = direction if direction in ("upstream", "downstream", "both") else "both"
        view, query, name = self._lineage_view(asset, direction, _int(depth, 3, 1, 6))
        return f"Lineage of {name} ({direction})", view, query

    def _graph_impact(self, asset: str, direction: str, depth: int) -> GraphParts:
        view, query, name = self._lineage_view(asset, "downstream", 6)
        return f"Impact of changing {name}", view, query

    def _graph_policies(self, asset: str, direction: str, depth: int) -> GraphParts:
        node = self._resolve(asset)
        own = self.repo.asset_neighbors(node.id, 500)
        ids = sorted({node.id, *(e.target for e in own.edges_of("HAS_COLUMN") if e.source == node.id)})
        sub = self.repo.governing_policies(ids)
        if not sub.edges:
            raise _ToolError(f"No policy sections are linked to '{asset}' in the graph.")
        query = cypher.GOVERNING_POLICIES.replace("$ids", "[" + ", ".join(cypher_literal(i) for i in ids) + "]")
        return f"Policies linked to {_graph_name(node)}", to_view(sub, start_ids={node.id}), query

    def _graph_pii(self, asset: str, direction: str, depth: int) -> GraphParts:
        sub = self.repo.pii()
        return "PII columns and where they flow", to_view(sub), cypher.PII

    def _graph_pipelines(self, asset: str, direction: str, depth: int) -> GraphParts:
        return "Pipelines and the tables they read and write", to_view(self.repo.pipelines()), cypher.PIPELINES

    def _graph_ontology(self, asset: str, direction: str, depth: int) -> GraphParts:
        m = self._ontology(asset)
        g = SubGraph()
        shown_tables: set[str] = set()
        for cid, c in m["shown"].items():
            g.add_node(c)
            for a in m["represented"][cid]:
                g.add_node(a)
                shown_tables.add(a.id)
                g.add_edge(GEdge(key=f"rep:{a.id}->{cid}", source=a.id, target=cid, type="REPRESENTS"))
        for e in m["related"]:
            if e.source in m["shown"] and e.target in m["shown"]:
                g.add_edge(GEdge(key=e.key, source=e.source, target=e.target, type="RELATED_TO", props=e.props))
        for (s, t), f in m["flows"].items():
            if s in shown_tables and t in shown_tables:
                e = f["edge"]
                g.add_edge(GEdge(key=e.key, source=s, target=t, type="FLOWS_TO", props=e.props))
        only = next(iter(m["shown"].values())) if len(m["shown"]) == 1 else None
        title = f"Ontology term {only.name}" if only else "Ontology terms, their tables and the data flows between them"
        return title, to_view(g), None  # no single Cypher query draws this graph

    def _graph_neighbors(self, asset: str, direction: str, depth: int) -> GraphParts:
        node = self._resolve(asset)
        sub = self.repo.asset_neighbors(node.id, 100)
        query = inline_params(cypher.NEIGHBORS_BY_ASSET_ID, {"id": node.id}).replace("$limit", "100")
        return f"Everything connected to {_graph_name(node)}", to_view(sub, start_ids={node.id}), query


def _searchable(n: GNode) -> str:
    """The text a name search should match: the asset's own name, never its id."""
    table = n.props.get("table")
    parts = [n.name, n.props.get("full_name"), f"{table}.{n.name}" if table else None, n.props.get("description")]
    return " ".join(str(p) for p in parts if p).lower()


def _graph_name(n: GNode) -> str:
    table, name = n.props.get("table"), n.name
    return str(n.props.get("full_name") or (f"{table}.{name}" if table else name))


class _ToolError(Exception):
    """A problem the model can read and recover from (bad name, nothing found, not available)."""


def _table_of(sub: SubGraph, col: GNode, owners: dict[str, str]) -> str | None:
    owner = sub.nodes.get(owners.get(col.id, ""))
    if owner is not None:
        return str(owner.props.get("full_name") or owner.name)
    table = col.props.get("table")
    return str(table) if table else None
