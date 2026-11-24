"""Local question embedder.

The vectors stored on ``PolicyChunk`` / ``Embeddable`` nodes were produced with
``BAAI/bge-small-en-v1.5`` (384 dimensions), so a question has to be embedded with the same model for
the vector indexes to return anything meaningful. The model runs locally (ONNX, no GPU, no API).

The model is loaded from ``model_cache_dir`` only: this process never downloads it, because some
corporate networks re-sign TLS certificates and a download from inside the app would fail. Fetch it once
with ``python scripts/fetch_embedding_model.py``.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)


class EmbeddingUnavailable(RuntimeError):
    """The local embedding model is not installed (or cannot be loaded)."""


class QueryEmbedder(Protocol):
    def embed_query(self, text: str) -> list[float]: ...

    def is_ready(self) -> bool: ...


class LocalEmbedder:
    def __init__(self, model_name: str, cache_dir: Path):
        self.model_name = model_name
        self.cache_dir = Path(cache_dir)
        self._model = None
        self._lock = threading.Lock()

    def _load(self):  # type: ignore[no-untyped-def]
        with self._lock:
            if self._model is None:
                try:
                    from fastembed import TextEmbedding

                    self._model = TextEmbedding(
                        self.model_name, cache_dir=str(self.cache_dir), local_files_only=True
                    )
                except Exception as exc:  # noqa: BLE001 - reported as a setup problem, not a crash
                    log.warning("Embedding model not available: %s", exc)
                    raise EmbeddingUnavailable(
                        f"The embedding model '{self.model_name}' is not installed in {self.cache_dir}. "
                        "Run: python scripts/fetch_embedding_model.py"
                    ) from exc
            return self._model

    def is_ready(self) -> bool:
        try:
            self._load()
            return True
        except EmbeddingUnavailable:
            return False

    def embed_query(self, text: str) -> list[float]:
        model = self._load()
        vector = next(iter(model.embed([text])))
        return [float(x) for x in vector]
