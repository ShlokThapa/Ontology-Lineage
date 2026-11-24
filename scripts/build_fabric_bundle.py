"""Package the policy-mapping code for Fabric.

    backend\\.venv\\Scripts\\python scripts\\build_fabric_bundle.py

Writes ``dist/lineage_mapping_bundle.zip``: only the modules the mapping job needs (no web framework, no local
embedding model), so the notebook only has to ``pip install openai neo4j pydantic pydantic-settings``. Upload the
zip to the lakehouse (Files/lineage_studio/) and run ``fabric/nb_llm_policy_mapping.ipynb``.

The script then proves the bundle is self-contained: it unpacks it into an empty folder and imports the job in a
clean interpreter, failing if anything outside the bundle's dependencies is pulled in.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "backend" / "app"
OUT = ROOT / "dist" / "lineage_mapping_bundle.zip"

FILES = [
    "config.py", "cypher.py", "graphmodel.py", "neo4j_client.py", "repository.py", "serialization.py",
    "chat/__init__.py", "chat/llm.py",
    "mapping/__init__.py", "mapping/catalogue.py", "mapping/engine.py", "mapping/models.py",
    "mapping/prompts.py", "mapping/store.py",
]
FORBIDDEN = ("fastapi", "uvicorn", "fastembed", "onnxruntime", "starlette")


def build() -> Path:
    OUT.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in FILES:
            z.write(APP / rel, f"app/{rel}")
    return OUT


def verify(bundle: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        zipfile.ZipFile(bundle).extractall(tmp)
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]);"
            "import app.mapping.engine, app.mapping.store, app.mapping.models, app.mapping.catalogue, app.config;"
            f"bad = [m for m in {FORBIDDEN!r} if m in sys.modules];"
            "assert not bad, f'bundle pulls in {bad}';"
            "print('bundle imports cleanly, no web framework or embedding runtime')"
        )
        # -I: ignore the user's site and the current directory, so only the bundle and installed packages count.
        subprocess.run([sys.executable, "-I", "-c", code, tmp], check=True)


if __name__ == "__main__":
    path = build()
    print(f"wrote {path} ({path.stat().st_size / 1024:.0f} KB, {len(FILES)} files)")
    verify(path)
