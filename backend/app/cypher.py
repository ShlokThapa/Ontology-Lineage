"""Cypher used by the lineage pages.

Every query returns nodes / relationships / paths so that the same text can be
pasted into the Query page ("Open in Query") and rendered as a graph there.
Depth values are validated integers and are inlined because variable-length
bounds cannot be parameters.
"""

from __future__ import annotations

from typing import Any, Literal

Direction = Literal["upstream", "downstream", "both"]


def cypher_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def inline_params(query: str, params: dict[str, Any]) -> str:
    """Inline string parameters so "Open in Query" works without :param."""
    for key, value in params.items():
        query = query.replace(f"${key}", cypher_literal(str(value)))
    return query

COLUMN_SEARCH_LABELS = ("Table", "View", "Column", "Pipeline", "BusinessConcept")

SEARCH = """CALL {
  MATCH (n:Asset)
  WHERE (n:Table OR n:View OR n:Column OR n:Pipeline OR n:BusinessConcept)
    AND (toLower(coalesce(n.name, '')) CONTAINS $q
         OR toLower(coalesce(n.full_name, '')) CONTAINS $q
         OR toLower(n.id) CONTAINS $q)
  RETURN n, toLower(n.name) AS title, n.id AS key
  UNION
  MATCH (n:PolicyDocument)
  WHERE toLower(coalesce(n.title, '')) CONTAINS $q
     OR toLower(coalesce(n.doc_id, '')) CONTAINS $q
     OR toLower(coalesce(n.source_file, '')) CONTAINS $q
  RETURN n, toLower(n.title) AS title, n.doc_id AS key
}
RETURN n
ORDER BY CASE WHEN title = $q THEN 0
              WHEN title STARTS WITH $q THEN 1
              WHEN n:PolicyDocument THEN 2
              ELSE 3 END, size(key)
LIMIT $limit"""

RESOLVE = """CALL {
  MATCH (n:Asset)
  WHERE n.id = $ref OR n.full_name = $ref OR n.id ENDS WITH $suffix
  RETURN n, CASE WHEN n.id = $ref THEN 0 WHEN n.full_name = $ref THEN 1 ELSE 2 END AS rank, n.id AS key
  UNION
  MATCH (n:PolicyDocument)
  WHERE n.doc_id = $ref OR n.title = $ref
  RETURN n, 0 AS rank, n.doc_id AS key
}
RETURN n
ORDER BY rank, size(key)
LIMIT 1"""

# Policy sections that govern any of the given tables / views / columns.
GOVERNING_POLICIES = """MATCH p = (:PolicyDocument)-[:HAS_CHUNK]->(:PolicyChunk)-[:GOVERNS]->(x:Asset)
WHERE x.id IN $ids
RETURN p"""

# For every column on a path, also return the HAS_COLUMN path to its owning table.
_OWNERS = "[x IN coalesce(nodes(p), [s]) | [h = (:Asset)-[:HAS_COLUMN]->(x) | h]] AS owners"


def _check_depth(depth: int) -> int:
    depth = int(depth)
    if not 1 <= depth <= 10:
        raise ValueError("depth must be between 1 and 10")
    return depth


def _seed_pattern(label: str) -> str | None:
    """Pattern binding `s` to the column(s) lineage starts from."""
    if label == "Column":
        return "(s:Column {id: $id})"
    if label in ("Table", "View"):
        return "(:Asset {id: $id})-[:HAS_COLUMN]->(s:Column)"
    if label == "BusinessConcept":
        return "(:BusinessConcept {id: $id})<-[:REPRESENTS]-(:Asset)-[:HAS_COLUMN]->(s:Column)"
    if label == "PolicyDocument":
        # Columns the policy governs directly, plus the columns of the tables it governs.
        return (
            "(:PolicyDocument {doc_id: $id})-[:HAS_CHUNK]->(:PolicyChunk)-[:GOVERNS]->(:Asset)"
            "-[:HAS_COLUMN*0..1]->(s:Column)"
        )
    return None


def lineage(label: str, direction: Direction, depth: int) -> str:
    d = _check_depth(depth)
    down = direction in ("downstream", "both")
    up = direction in ("upstream", "both")
    start = "(start:PolicyDocument {doc_id: $id})" if label == "PolicyDocument" else "(start:Asset {id: $id})"
    branches = [f"MATCH {start}\nRETURN start AS s, null AS p, [] AS owners"]

    seed = _seed_pattern(label)
    if seed:
        if down:
            branches.append(
                f"MATCH {seed}\nOPTIONAL MATCH p = (s)-[:FLOWS_TO*1..{d}]->(:Column)\nRETURN s, p, {_OWNERS}"
            )
        if up:
            branches.append(
                f"MATCH {seed}\nOPTIONAL MATCH p = (:Column)-[:FLOWS_TO*1..{d}]->(s)\nRETURN s, p, {_OWNERS}"
            )
    if label in ("Table", "View"):
        if down:
            branches.append(
                f"MATCH p = (:Asset {{id: $id}})-[:FEEDS*1..{d}]->(:Asset)\nRETURN null AS s, p, [] AS owners"
            )
        if up:
            branches.append(
                f"MATCH p = (:Asset)-[:FEEDS*1..{d}]->(:Asset {{id: $id}})\nRETURN null AS s, p, [] AS owners"
            )
    if label == "BusinessConcept":
        branches.append(
            "MATCH p = (:Asset)-[:REPRESENTS]->(:BusinessConcept {id: $id})\nRETURN null AS s, p, [] AS owners"
        )
    if label == "PolicyDocument":
        branches.append(
            "MATCH p = (:PolicyDocument {doc_id: $id})-[:HAS_CHUNK]->(:PolicyChunk)-[:GOVERNS]->(:Asset)\n"
            "RETURN null AS s, p, [x IN nodes(p) | [h = (:Asset)-[:HAS_COLUMN]->(x) | h]] AS owners"
        )
    if label == "Pipeline":
        branches.append(
            "MATCH p = (:Pipeline {id: $id})-[:CONTAINS]->(:Activity)-[:READS_FROM|WRITES_TO]->(:Asset)\n"
            "RETURN null AS s, p, [] AS owners"
        )
    if label == "Activity":
        branches.append(
            "MATCH p = (:Pipeline)-[:CONTAINS]->(:Activity {id: $id})-[:READS_FROM|WRITES_TO]->(:Asset)\n"
            "RETURN null AS s, p, [] AS owners"
        )
    return "\nUNION\n".join(branches)


def impact(label: str, depth: int) -> str:
    return lineage(label, "downstream", depth)


PII = """MATCH (src:Column)
WHERE src.is_pii = true AND NOT coalesce(src.platform, '') STARTS WITH 'Fabric'
OPTIONAL MATCH c = (src)-[:CLASSIFIED_AS]->(:Classification)
OPTIONAL MATCH f = (src)-[:FLOWS_TO]->(tgt:Column)
OPTIONAL MATCH tc = (tgt)-[:CLASSIFIED_AS]->(:Classification)
RETURN src, c, f, tc,
       [x IN [src, tgt] WHERE x IS NOT NULL | [h = (:Asset)-[:HAS_COLUMN]->(x) | h]] AS owners"""

PIPELINES = """MATCH p = (:Pipeline)-[:CONTAINS]->(a:Activity)
OPTIONAL MATCH r = (a)-[:READS_FROM|WRITES_TO|PRECEDES|INVOKES]->(:Asset)
RETURN p, r"""

CONCEPTS = """MATCH (c:BusinessConcept)
OPTIONAL MATCH p = (c)-[:RELATED_TO]->(:BusinessConcept)
RETURN c, p"""

CONCEPTS_WITH_TABLES = (
    CONCEPTS + "\nUNION\nMATCH p = (:Asset)-[:REPRESENTS]->(:BusinessConcept)\nRETURN null AS c, p"
)

NEIGHBORS_BY_ELEMENT_ID = """MATCH (n) WHERE elementId(n) = $id
OPTIONAL MATCH p = (n)-[]-()
RETURN n, p
LIMIT $limit"""

NEIGHBORS_BY_ASSET_ID = """MATCH (n) WHERE (n:Asset AND n.id = $id) OR (n:PolicyDocument AND n.doc_id = $id)
OPTIONAL MATCH p = (n)-[]-()
RETURN n, p
LIMIT $limit"""

SCHEMA_VISUALIZATION = "CALL db.schema.visualization()"

LABEL_COUNTS = "MATCH (n) UNWIND labels(n) AS label RETURN label, count(*) AS count ORDER BY label"
REL_COUNTS = "MATCH ()-[r]->() RETURN type(r) AS type, count(*) AS count ORDER BY type"
PROPERTY_KEY_COUNTS = """CALL {
  MATCH (n) UNWIND keys(n) AS key RETURN key
  UNION ALL
  MATCH ()-[r]->() UNWIND keys(r) AS key RETURN key
}
RETURN key, count(*) AS count ORDER BY key"""
