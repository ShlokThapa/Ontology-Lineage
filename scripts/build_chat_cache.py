"""Prepare the saved answers for the chat's suggested questions.

    backend\\.venv\\Scripts\\python scripts\\build_chat_cache.py              # every suggested question
    backend\\.venv\\Scripts\\python scripts\\build_chat_cache.py --only data-health kyc-gaps

Each question is sent through the real chat endpoint (live Neo4j, the lakehouse and the language model), so the saved
answer is exactly what a person would have got. The result goes to ``backend/app/chat/cached_answers.json``. An answer
in which a tool failed is not saved (it would freeze a half answer); fix the cause and run it again.

Run it again whenever the data, the policies or the list in ``suggested_questions.py`` change.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", nargs="*", help="question ids to rebuild (default: all)")
    parser.add_argument("--allow-errors", action="store_true", help="save answers even if a tool step failed")
    args = parser.parse_args()

    from fastapi.testclient import TestClient

    from app.chat.answer_cache import DEFAULT_FILE
    from app.chat.suggested_questions import QUESTIONS
    from app.config import get_settings
    from app.main import create_app

    settings = get_settings().model_copy(update={"chat_use_cache": False})
    wanted = [q for q in QUESTIONS if not args.only or q["id"] in args.only]
    if not wanted:
        print("No such question id. Known:", ", ".join(q["id"] for q in QUESTIONS))
        return 2

    try:
        existing = json.loads(DEFAULT_FILE.read_text(encoding="utf-8")).get("answers", {})
    except (OSError, ValueError):
        existing = {}
    answers = {k: v for k, v in existing.items() if k in {q["id"] for q in QUESTIONS}}

    failed = 0
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        for q in wanted:
            start = time.time()
            res = client.post("/api/chat", json={"messages": [{"role": "user", "content": q["question"]}]}, timeout=300)
            took = time.time() - start
            if res.status_code != 200:
                print(f"[FAIL] {q['id']}: HTTP {res.status_code} {res.text[:200]}")
                failed += 1
                continue
            body = res.json()
            errors = [s for s in body.get("steps", []) if s.get("error")]
            if errors and not args.allow_errors:
                print(f"[SKIP] {q['id']}: {len(errors)} tool step(s) failed ({errors[0]['summary'][:120]}); not saved")
                failed += 1
                continue
            body.pop("cached", None)
            body.pop("cached_at", None)
            answers[q["id"]] = {
                "question": q["question"], "prepared_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "response": body,
            }
            print(f"[ok]   {q['id']}: {took:.0f}s, {len(body['steps'])} steps, {body.get('tokens')} tokens"
                  f"{', dashboard' if body.get('dashboard') else ''}{', graph' if body.get('graph') else ''}")

    ordered = {q["id"]: answers[q["id"]] for q in QUESTIONS if q["id"] in answers}
    out = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "answers": ordered}
    tmp = DEFAULT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    tmp.replace(DEFAULT_FILE)
    print(f"Saved {len(ordered)} of {len(QUESTIONS)} answers to {DEFAULT_FILE.relative_to(ROOT)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
