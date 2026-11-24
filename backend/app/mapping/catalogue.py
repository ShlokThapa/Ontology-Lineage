"""What the LLM sees about the data estate: a compact catalogue of tables and columns.

Metadata only: names, types, descriptions, PII classification and where each column's data comes from. Never
row data. Every asset gets a short, copy-friendly reference (``dim.account.balance``); the model must answer
with those references, and the code rejects anything that is not in the catalogue.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from ..graphmodel import GNode, SubGraph

_MAX_EXPR = 110
_MAX_SOURCES = 4


@dataclass
class Catalogue:
    text: str
    refs: dict[str, str]  # reference -> asset id, e.g. "dim.account.balance" -> "fabric:...:balance"
    kinds: dict[str, str]  # reference -> "Table" | "View" | "Column"
    lines: dict[str, str]  # reference -> its catalogue line (shown again to the verifier)
    categories: dict[str, list[str]] = field(default_factory=dict)  # "@PII" -> references of its columns
    hash: str = ""

    @property
    def columns(self) -> int:
        return sum(1 for k in self.kinds.values() if k == "Column")

    @property
    def tables(self) -> int:
        return sum(1 for k in self.kinds.values() if k != "Column")


def _clip(value: object, limit: int) -> str:
    s = " ".join(str(value or "").split())
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _table_ref(t: GNode) -> str:
    return str(t.props.get("full_name") or t.name)


def build_catalogue(sub: SubGraph) -> Catalogue:
    """Turn the graph returned by ``Repository.catalogue()`` into the text the models read."""
    nodes = sub.nodes
    tables = sorted((n for n in nodes.values() if n.label in ("Table", "View")), key=_table_ref)

    columns_of: dict[str, list[GNode]] = {}
    for e in sub.edges_of("HAS_COLUMN"):
        if e.source in nodes and e.target in nodes:
            columns_of.setdefault(e.source, []).append(nodes[e.target])
    concepts_of: dict[str, list[str]] = {}
    for e in sub.edges_of("REPRESENTS"):
        if e.source in nodes and e.target in nodes and nodes[e.target].label == "BusinessConcept":
            concepts_of.setdefault(e.source, []).append(nodes[e.target].name)
    classes_of: dict[str, list[str]] = {}
    for e in sub.edges_of("CLASSIFIED_AS"):
        if e.source in nodes and e.target in nodes:
            classes_of.setdefault(e.source, []).append(nodes[e.target].name)
    flows_into: dict[str, list] = {}
    for e in sub.edges_of("FLOWS_TO"):
        flows_into.setdefault(e.target, []).append(e)

    refs: dict[str, str] = {}
    kinds: dict[str, str] = {}
    lines: dict[str, str] = {}
    categories: dict[str, list[str]] = {}
    out: list[str] = []

    def unique(ref: str) -> str:
        base, n = ref, 2
        while ref in refs:
            ref, n = f"{base}#{n}", n + 1
        return ref

    def col_label(n: GNode | None) -> str:
        if n is None:
            return "?"
        table = n.props.get("table")
        return f"{table}.{n.name}" if table else n.name

    for t in tables:
        t_ref = unique(_table_ref(t))
        refs[t_ref], kinds[t_ref] = t.id, t.label
        head = f"TABLE {t_ref} ({t.label.lower()}, {t.props.get('platform') or 'platform unknown'})"
        if concepts_of.get(t.id):
            head += " | represents business term: " + ", ".join(sorted(set(concepts_of[t.id])))
        if t.props.get("comment"):
            head += " | " + _clip(t.props["comment"], 160)
        lines[t_ref] = head
        out.append(head)

        for c in sorted(columns_of.get(t.id, []), key=lambda n: n.name):
            c_ref = unique(f"{t_ref}.{c.name}")
            refs[c_ref], kinds[c_ref] = c.id, "Column"
            flags = list(classes_of.get(c.id, []))
            if c.props.get("is_pii") is True and "PII" not in flags:
                flags.insert(0, "PII")
            # Sources that share one transformation are listed together, so a derived column shows its
            # expression once instead of once per source column.
            groups: dict[tuple[str, str], list[str]] = {}
            for e in flows_into.get(c.id, [])[:_MAX_SOURCES]:
                how = str(e.props.get("transformation") or "DIRECT")
                expr = _clip(e.props.get("expression"), _MAX_EXPR) if e.props.get("expression") and how != "DIRECT" else ""
                groups.setdefault((how, expr), []).append(col_label(nodes.get(e.source)))
            sources = [
                ", ".join(names) + f" [{how}" + (f": {expr}" if expr else "") + "]"
                for (how, expr), names in groups.items()
            ]
            parts = [c_ref, str(c.props.get("data_type") or "type unknown")]
            if flags:
                parts.append("flags: " + ",".join(sorted(set(flags))))
            if c.props.get("comment"):
                parts.append(_clip(c.props["comment"], 160))
            if sources:
                parts.append("from " + "; ".join(sources))
            line = "  " + " | ".join(parts)
            lines[c_ref] = line
            out.append(line)

            for flag in set(flags):
                categories.setdefault("@" + flag.upper(), []).append(c_ref)

    header = [
        f"{sum(1 for k in kinds.values() if k != 'Column')} tables/views, "
        f"{sum(1 for k in kinds.values() if k == 'Column')} columns. Use the names below exactly as written.",
    ]
    if categories:
        header.append(
            "Categories you may use when a policy speaks about a whole group: "
            + "; ".join(f"{name} ({len(cols)} columns)" for name, cols in sorted(categories.items()))
        )
    text = "\n".join(header + [""] + out)
    return Catalogue(
        text=text,
        refs=refs,
        kinds=kinds,
        lines=lines,
        categories={k: sorted(v) for k, v in categories.items()},
        hash=hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
    )
