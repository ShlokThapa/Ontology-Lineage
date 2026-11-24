"""Data access for the lineage pages: a live Neo4j implementation.

The snapshot (offline) implementation lives in :mod:`app.snapshot` and exposes
the same methods, so the API layer does not care which one it talks to.
"""

from __future__ import annotations

from typing import Any, Protocol

from . import cypher
from .cypher import Direction
from .graphmodel import GNode, SubGraph, from_neo4j_graph, node_key
from .neo4j_client import Neo4jClient

# Lineage pages are internal queries; give them a more generous row budget.
_INTERNAL_MAX_ROWS = 50_000


class Repository(Protocol):
    mode: str

    def resolve(self, ref: str) -> GNode | None: ...
    def search(self, q: str, limit: int = 25) -> list[GNode]: ...
    def lineage(self, start: GNode, direction: Direction, depth: int) -> SubGraph: ...
    def governing_policies(self, ids: list[str]) -> SubGraph: ...
    def pii(self) -> SubGraph: ...
    def catalogue(self) -> SubGraph: ...
    def pipelines(self) -> SubGraph: ...
    def concepts(self, include_tables: bool) -> SubGraph: ...
    def asset_neighbors(self, asset_id: str, limit: int = 200) -> SubGraph: ...
    def schema_info(self) -> dict[str, Any]: ...
    def schema_graph(self) -> SubGraph: ...


def _node_from_record(value: dict[str, Any]) -> GNode:
    props = value.get("properties", {})
    return GNode(id=node_key(props, value["elementId"]), labels=value.get("labels", []), props=props)


class LiveRepository:
    mode = "live"

    def __init__(self, client: Neo4jClient):
        self.client = client

    def _sub(self, query: str, params: dict[str, Any] | None = None) -> SubGraph:
        res = self.client.run_read(query, params or {}, max_rows=_INTERNAL_MAX_ROWS)
        return from_neo4j_graph(res.graph)

    def resolve(self, ref: str) -> GNode | None:
        res = self.client.run_read(cypher.RESOLVE, {"ref": ref, "suffix": "." + ref}, max_rows=1)
        if not res.records:
            return None
        return _node_from_record(res.records[0]["n"])

    def search(self, q: str, limit: int = 25) -> list[GNode]:
        res = self.client.run_read(cypher.SEARCH, {"q": q.lower(), "limit": limit}, max_rows=limit)
        return [_node_from_record(r["n"]) for r in res.records]

    def lineage(self, start: GNode, direction: Direction, depth: int) -> SubGraph:
        return self._sub(cypher.lineage(start.label, direction, depth), {"id": start.id})

    def governing_policies(self, ids: list[str]) -> SubGraph:
        if not ids:
            return SubGraph()
        return self._sub(cypher.GOVERNING_POLICIES, {"ids": ids})

    def pii(self) -> SubGraph:
        return self._sub(cypher.PII)

    def catalogue(self) -> SubGraph:
        return self._sub(cypher.CATALOGUE)

    def pipelines(self) -> SubGraph:
        return self._sub(cypher.PIPELINES)

    def concepts(self, include_tables: bool) -> SubGraph:
        return self._sub(cypher.CONCEPTS_WITH_TABLES if include_tables else cypher.CONCEPTS)

    def asset_neighbors(self, asset_id: str, limit: int = 200) -> SubGraph:
        return self._sub(cypher.NEIGHBORS_BY_ASSET_ID, {"id": asset_id, "limit": limit})

    def element_neighbors(self, element_id: str, limit: int = 200) -> dict[str, Any]:
        """Neighbourhood in the raw query-result graph format (for the Query page)."""
        res = self.client.run_read(cypher.NEIGHBORS_BY_ELEMENT_ID, {"id": element_id, "limit": limit}, max_rows=limit)
        return res.graph

    def schema_info(self) -> dict[str, Any]:
        labels = self.client.run_read(cypher.LABEL_COUNTS, max_rows=10_000).records
        rels = self.client.run_read(cypher.REL_COUNTS, max_rows=10_000).records
        props = self.client.run_read(cypher.PROPERTY_KEY_COUNTS, max_rows=10_000).records
        return {
            "labels": [{"name": r["label"], "count": r["count"]} for r in labels],
            "relationship_types": [{"name": r["type"], "count": r["count"]} for r in rels],
            "property_keys": [{"name": r["key"], "count": r["count"]} for r in props],
        }

    def schema_graph(self) -> SubGraph:
        res = self.client.run_read(cypher.SCHEMA_VISUALIZATION, max_rows=10_000)
        sub = from_neo4j_graph(res.graph)
        # Virtual schema nodes carry their label in the `name` property; key them by it.
        out = SubGraph()
        by_key: dict[str, str] = {}
        for key, n in sub.nodes.items():
            name = str(n.props.get("name") or (n.labels[0] if n.labels else key))
            by_key[key] = name
            out.add_node(GNode(id=name, labels=[name], props={"name": name}))
        for e in sub.edges.values():
            s, t = by_key.get(e.source), by_key.get(e.target)
            if s and t:
                out.edges[f"{s}-{e.type}->{t}"] = e.__class__(
                    key=f"{s}-{e.type}->{t}", source=s, target=t, type=e.type, props={}
                )
        return out
