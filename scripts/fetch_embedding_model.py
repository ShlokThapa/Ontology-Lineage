"""One-time download of the local embedding model into ``.model_cache/``.

    backend\\.venv\\Scripts\\python scripts\\fetch_embedding_model.py

Uses the operating system's certificate store (the `truststore` package), so it works behind a corporate
proxy that re-signs TLS certificates, without turning certificate verification off. It runs as its own
process on purpose: injecting the OS trust store changes how Python builds SSL contexts, which the Neo4j
driver's ``neo4j+ssc://`` mode does not tolerate, so the API server never does it.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def main() -> int:
    try:
        import truststore

        truststore.inject_into_ssl()
    except ImportError:
        print("Note: `truststore` is not installed; using Python's default certificates.")

    from app.config import get_settings
    from fastembed import TextEmbedding

    settings = get_settings()
    settings.model_cache_dir.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {settings.embedding_model} to {settings.model_cache_dir} ...")
    model = TextEmbedding(settings.embedding_model, cache_dir=str(settings.model_cache_dir))
    vector = next(iter(model.embed(["connectivity check"])))
    print(f"OK: model loaded, {len(vector)} dimensions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
