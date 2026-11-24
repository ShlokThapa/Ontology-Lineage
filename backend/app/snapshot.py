"""Offline repository backed by ``data/nodes.json`` and ``data/edges.json``.

File format (as written by ``scripts/export_snapshot.py``):

* nodes.json: ``[{"id": "...", "label": "Column", "name": "...", <properties>}, ...]``
* edges.json: ``[{"from": "...", "rel": "FLOWS_TO", "to": "...", <properties>}, ...]``

Lineage is computed in memory and produces the same :class:`SubGraph` shapes as
the live Cypher queries in :mod:`app.cypher`.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from .cypher import Direction
from .graphmodel import GENERIC_LABEL, GEdge, GNode, SubGraph, primary_label

SEARCH_LABELS = {"Table", "View", "Column", "Pipeline", "BusinessConcept", "PolicyDocument"}


class SnapshotRepository:
    mode = "snapshot"

    def __init__(self, nodes: list[dict[str, Any]], edges: list[dict[str, Any]]):
        self.nodes: dict[str, GNode] = {}
        for raw in nodes:
            props = {k: v for k, v in raw.items() if k not in ("label", "labels")}
            labels = raw.get("labels") or [raw.get("label") or "Node"]
            if GENERIC_LABEL not in labels:
                labels = [*labels, GENERIC_LABEL]
            node = GNode(id=str(raw["id"]), labels=list(labels), props=props)
            self.nodes[node.id] = node

        self.edges: list[GEdge] = []
        self.out: dict[str, dict[str, list[GEdge]]] = defaultdict(lambda: defaultdict(list))
        self.inc: dict[str, dict[str, list[GEdge]]] = defaultdict(lambda: defaultdict(list))
        for i, raw in enumerate(edges):
            props = {k: v for k, v in raw.items() if k not in ("from", "rel", "to")}
            e = GEdge(key=f"e{i}", source=str(raw["from"]), target=str(raw["to"]), type=str(raw["rel"]), props=props)
            if e.source not in self.nodes or e.target not in self.nodes:
                continue
            self.edges.append(e)
            self.out[e.source][e.type].append(e)
            self.inc[e.target][e.type].append(e)

    @classmethod
    def from_dir(cls, directory: Path) -> "SnapshotRepository":
        nodes = json.loads((directory / "nodes.json").read_text(encoding="utf-8"))
        edges = json.loads((directory / "edges.json").read_text(encoding="utf-8"))
        return cls(nodes, edges)

    # -- helpers ---------------------------------------------------------------

    def _out(self, node_id: str, *types: str) -> list[GEdge]:
        return [e for t in types for e in self.out.get(node_id, {}).get(t, [])]

    def _in(self, node_id: str, *types: str) -> list[GEdge]:
        return [e for t in types for e in self.inc.get(node_id, {}).get(t, [])]

    def _add_edge(self, sub: SubGraph, e: GEdge) -> None:
        sub.add_node(self.nodes[e.source])
        sub.add_node(self.nodes[e.target])
        sub.add_edge(e)

    def _add_owner(self, sub: SubGraph, column_id: str) -> None:
        for e in self._in(column_id, "HAS_COLUMN"):
            self._add_edge(sub, e)

    def _bfs(self, sub: SubGraph, seeds: Iterable[str], rel: str, depth: int, downstream: bool) -> set[str]:
        """Add every edge on a path of length <= depth from the seeds. Returns visited node ids."""
        dist = {s: 0 for s in seeds}
        frontier = list(dist)
        while frontier:
            nxt = []
            for cur in frontier:
                if dist[cur] >= depth:
                    continue
                edges = self._out(cur, rel) if downstream else self._in(cur, rel)
                for e in edges:
                    self._add_edge(sub, e)
                    other = e.target if downstream else e.source
                    if other not in dist:
                        dist[other] = dist[cur] + 1
                        nxt.append(other)
            frontier = nxt
        return set(dist)

    # -- repository API --------------------------------------------------------

    def resolve(self, ref: str) -> GNode | None:
        if ref in self.nodes:
            return self.nodes[ref]
        candidates = [
            n for n in self.nodes.values() if n.props.get("full_name") == ref or n.id.endswith("." + ref)
        ]
        candidates.sort(key=lambda n: (n.props.get("full_name") != ref, len(n.id)))
        return candidates[0] if candidates else None

    def search(self, q: str, limit: int = 25) -> list[GNode]:
        q = q.lower()
        hits = []
        for n in self.nodes.values():
            if not SEARCH_LABELS.intersection(n.labels):
                continue
            name = str(n.props.get("name") or n.props.get("title") or "").lower()
            if q in name or q in str(n.props.get("full_name") or "").lower() or q in n.id.lower():
                rank = 0 if name == q else 1 if name.startswith(q) else 2
                hits.append((rank, len(n.id), n))
        hits.sort(key=lambda h: (h[0], h[1]))
        return [h[2] for h in hits[:limit]]

    def _seeds(self, start: GNode) -> set[str]:
        if start.label == "Column":
            return {start.id}
        if start.label in ("Table", "View"):
            return {e.target for e in self._out(start.id, "HAS_COLUMN")}
        if start.label == "BusinessConcept":
            tables = [e.source for e in self._in(start.id, "REPRESENTS")]
            return {e.target for t in tables for e in self._out(t, "HAS_COLUMN")}
        if start.label == "PolicyDocument":
            seeds: set[str] = set()
            for g in self._governed(start.id):
                if self.nodes[g.target].label == "Column":
                    seeds.add(g.target)
                seeds.update(e.target for e in self._out(g.target, "HAS_COLUMN"))
            return seeds
        return set()

    @staticmethod
    def _active(g: GEdge) -> bool:
        """Same rule as cypher.GOVERNS_ACTIVE: a link without a status counts as accepted."""
        return (g.props.get("status") or "accepted") == "accepted"

    def _governed(self, doc_id: str) -> list[GEdge]:
        return [g for c in self._out(doc_id, "HAS_CHUNK") for g in self._out(c.target, "GOVERNS") if self._active(g)]

    def governing_policies(self, ids: list[str]) -> SubGraph:
        sub = SubGraph()
        for asset in ids:
            for g in (g for g in self._in(asset, "GOVERNS") if self._active(g)):
                for h in self._in(g.source, "HAS_CHUNK"):
                    self._add_edge(sub, h)
                    self._add_edge(sub, g)
        return sub

    def lineage(self, start: GNode, direction: Direction, depth: int) -> SubGraph:
        sub = SubGraph()
        sub.add_node(start)
        down = direction in ("downstream", "both")
        up = direction in ("upstream", "both")

        seeds = self._seeds(start)
        for s in seeds:
            sub.add_node(self.nodes[s])
        if down:
            self._bfs(sub, seeds, "FLOWS_TO", depth, downstream=True)
        if up:
            self._bfs(sub, seeds, "FLOWS_TO", depth, downstream=False)
        # Owning tables of every column involved (mirrors the `owners` list in Cypher).
        for nid in [n.id for n in sub.nodes.values() if n.label == "Column"]:
            self._add_owner(sub, nid)

        if start.label in ("Table", "View"):
            if down:
                self._bfs(sub, [start.id], "FEEDS", depth, downstream=True)
            if up:
                self._bfs(sub, [start.id], "FEEDS", depth, downstream=False)
        if start.label == "BusinessConcept":
            for e in self._in(start.id, "REPRESENTS"):
                self._add_edge(sub, e)
        if start.label == "PolicyDocument":
            for c in self._out(start.id, "HAS_CHUNK"):
                for g in (g for g in self._out(c.target, "GOVERNS") if self._active(g)):
                    self._add_edge(sub, c)
                    self._add_edge(sub, g)
                    if self.nodes[g.target].label == "Column":
                        self._add_owner(sub, g.target)
        if start.label == "Pipeline":
            for c in self._out(start.id, "CONTAINS"):
                for e in self._out(c.target, "READS_FROM", "WRITES_TO"):
                    self._add_edge(sub, c)
                    self._add_edge(sub, e)
        if start.label == "Activity":
            for c in self._in(start.id, "CONTAINS"):
                for e in self._out(start.id, "READS_FROM", "WRITES_TO"):
                    self._add_edge(sub, c)
                    self._add_edge(sub, e)
        return sub

    def _is_fabric_column(self, n: GNode) -> bool:
        return str(n.props.get("platform") or "").startswith("Fabric")

    def pii(self) -> SubGraph:
        sub = SubGraph()
        for n in self.nodes.values():
            if n.label != "Column" or n.props.get("is_pii") is not True or self._is_fabric_column(n):
                continue
            sub.add_node(n)
            self._add_owner(sub, n.id)
            for e in self._out(n.id, "CLASSIFIED_AS"):
                self._add_edge(sub, e)
            for f in self._out(n.id, "FLOWS_TO"):
                self._add_edge(sub, f)
                self._add_owner(sub, f.target)
                for e in self._out(f.target, "CLASSIFIED_AS"):
                    self._add_edge(sub, e)
        return sub

    def catalogue(self) -> SubGraph:
        """Tables / views with their columns, business terms, classifications and incoming flows
        (the same shape cypher.CATALOGUE returns)."""
        sub = SubGraph()
        for t in self.nodes.values():
            if t.label not in ("Table", "View"):
                continue
            sub.add_node(t)
            for e in self._out(t.id, "REPRESENTS"):
                self._add_edge(sub, e)
            for h in self._out(t.id, "HAS_COLUMN"):
                self._add_edge(sub, h)
                for e in self._out(h.target, "CLASSIFIED_AS"):
                    self._add_edge(sub, e)
                for e in self._in(h.target, "FLOWS_TO"):
                    self._add_edge(sub, e)
        return sub

    def pipelines(self) -> SubGraph:
        sub = SubGraph()
        for n in self.nodes.values():
            if n.label != "Pipeline":
                continue
            for c in self._out(n.id, "CONTAINS"):
                if self.nodes[c.target].label != "Activity":
                    continue
                self._add_edge(sub, c)
                for e in self._out(c.target, "READS_FROM", "WRITES_TO", "PRECEDES", "INVOKES"):
                    self._add_edge(sub, e)
        return sub

    def concepts(self, include_tables: bool) -> SubGraph:
        sub = SubGraph()
        for n in self.nodes.values():
            if n.label != "BusinessConcept":
                continue
            sub.add_node(n)
            for e in self._out(n.id, "RELATED_TO"):
                if self.nodes[e.target].label == "BusinessConcept":
                    self._add_edge(sub, e)
            if include_tables:
                for e in self._in(n.id, "REPRESENTS"):
                    self._add_edge(sub, e)
        return sub

    def asset_neighbors(self, asset_id: str, limit: int = 200) -> SubGraph:
        sub = SubGraph()
        node = self.nodes.get(asset_id)
        if node is None:
            return sub
        sub.add_node(node)
        edges = [e for es in self.out.get(asset_id, {}).values() for e in es]
        edges += [e for es in self.inc.get(asset_id, {}).values() for e in es]
        for e in edges[:limit]:
            self._add_edge(sub, e)
        return sub

    def schema_info(self) -> dict[str, Any]:
        labels: Counter[str] = Counter()
        props: Counter[str] = Counter()
        for n in self.nodes.values():
            labels.update(n.labels)
            props.update(n.props.keys())
        rels: Counter[str] = Counter(e.type for e in self.edges)
        for e in self.edges:
            props.update(e.props.keys())
        return {
            "labels": [{"name": k, "count": v} for k, v in sorted(labels.items())],
            "relationship_types": [{"name": k, "count": v} for k, v in sorted(rels.items())],
            "property_keys": [{"name": k, "count": v} for k, v in sorted(props.items())],
        }

    def schema_graph(self) -> SubGraph:
        sub = SubGraph()
        for n in self.nodes.values():
            lbl = primary_label(n.labels)
            sub.add_node(GNode(id=lbl, labels=[lbl], props={"name": lbl}))
        for e in self.edges:
            s = primary_label(self.nodes[e.source].labels)
            t = primary_label(self.nodes[e.target].labels)
            key = f"{s}-{e.type}->{t}"
            sub.add_edge(GEdge(key=key, source=s, target=t, type=e.type))
        return sub
