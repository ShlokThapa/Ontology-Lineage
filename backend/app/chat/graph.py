"""Graph display for the chat.

A graph is produced only when the user's latest message directly asks for one. That rule is enforced here,
in code, not left to the model: ``wants_graph`` decides whether the ``show_graph`` tool is offered at all.
"""

from __future__ import annotations

import re
from typing import Any

# Words that mean "show me a picture". \b on both sides keeps "GraphRAG" and "autograph" from matching.
_GRAPH_WORDS = re.compile(
    r"\b(graphs?|graphical(ly)?|diagrams?|visual(s|ly)?|visuali[sz](e|ed|ing|ation|ations)|draw(ing)?|plot|"
    r"charts?|pictures?|illustrat\w*)\b",
    re.IGNORECASE,
)

# "show / see / display / look at" followed closely by a lineage-style noun ("I want to see the lineage of ...",
# "show me the impact of changing ..."): lineage is a picture by nature. The gap is capped and may not cross
# a sentence end, and the bare verb "view" is left out because a view is also a database object.
_SEE_LINEAGE = re.compile(
    r"\b(show|see|display|look at)\b[^.?!\n]{0,40}?"
    r"\b(lineage|impact|data ?flows?|upstream|downstream|dependenc(y|ies))\b",
    re.IGNORECASE,
)

MAX_GRAPH_NODES = 120
_MAX_PROP_TEXT = 300
_MAX_LIST = 20  # longer numeric lists are embeddings, never sent to the browser

GRAPH_KINDS = ("lineage", "impact", "policies", "pii", "pipelines", "neighbors", "ontology")


def wants_graph(text: str) -> bool:
    """True when the message directly asks for a graph / diagram / visual, or to show / see a lineage."""
    text = text or ""
    if _GRAPH_WORDS.search(text):
        return True
    for sentence in re.split(r"[.?!\n]+", text):
        sentence = sentence.strip()
        for m in _SEE_LINEAGE.finditer(sentence):
            # "I see the lineage is incomplete" states an observation. Only that exact opening is ignored;
            # "Can I see ...", "I want to see ..." and a later "..., can you show me the lineage" still count.
            if m.group(1).lower() == "see" and re.fullmatch(r"i\s+", sentence[: m.start(1)], re.IGNORECASE):
                continue
            return True
    return False


def _clean_props(props: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in props.items():
        # `embedding` is the vector; embedding_model / embedding_run / embedding_text are indexing bookkeeping.
        if key.startswith("embedding") or (isinstance(value, list) and len(value) > _MAX_LIST):
            continue
        if isinstance(value, str) and len(value) > _MAX_PROP_TEXT:
            value = value[:_MAX_PROP_TEXT].rstrip() + " …"
        out[key] = value
    return out


def clean_view(view: dict[str, list[dict[str, Any]]], max_nodes: int = MAX_GRAPH_NODES) -> tuple[dict[str, Any], bool]:
    """Make a ``to_view`` result safe to ship: no embeddings, clipped text, bounded size.

    Nodes marked ``start`` are kept first, so a cap never drops the thing the user asked about.
    Returns the cleaned view and whether it was truncated.
    """
    nodes = sorted(view["nodes"], key=lambda n: not n.get("start"))
    truncated = len(nodes) > max_nodes
    nodes = nodes[:max_nodes]
    keep = {n["id"] for n in nodes}
    return (
        {
            "nodes": [{**n, "properties": _clean_props(n.get("properties") or {})} for n in nodes],
            "edges": [
                {**e, "properties": _clean_props(e.get("properties") or {})}
                for e in view["edges"]
                if e["source"] in keep and e["target"] in keep
            ],
        },
        truncated,
    )
