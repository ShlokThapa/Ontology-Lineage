"""LLM mapping of policy sections to the data assets they govern.

The job reads policy chunks and a metadata catalogue of the data estate (never row data), asks a mapper model
for links, has a second model verify them and a third break ties, checks every link in code, and writes
``GOVERNS`` relationships to Neo4j with full provenance. It runs as a script here (``python -m app.mapping``)
or from a Fabric notebook, and the Jobs page in the app runs the same functions (``app.mapping.service``).
Only ``MappingWriter`` changes the graph; the app's Cypher endpoints stay read-only.
"""
