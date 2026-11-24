"""Answers prepared in advance for the suggested questions.

``cached_answers.json`` sits next to the code. When a conversation starts with exactly one of the suggested
questions, the saved answer is returned at once instead of asking the model again. Only a first message qualifies:
a follow-up depends on what was said before, so it always goes to the model.

The file records when it was prepared; the page shows that date so a saved answer is never mistaken for a live one.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_FILE = Path(__file__).with_name("cached_answers.json")


def normalise(text: str) -> str:
    """Lower case, one space between words, no closing punctuation: 'Which loans? ' matches 'which loans'."""
    return re.sub(r"\s+", " ", (text or "").strip().lower()).rstrip(" ?.!")


class AnswerCache:
    def __init__(self, path: Path | None = None, enabled: bool = True):
        self.path = path or DEFAULT_FILE
        self.enabled = enabled
        self._mtime = -1.0
        self._by_question: dict[str, dict[str, Any]] = {}
        self._ids: set[str] = set()
        self.generated_at: str | None = None

    def _load(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            self._mtime, self._by_question, self._ids, self.generated_at = -1.0, {}, set(), None
            return
        if mtime == self._mtime:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("Could not read %s; saved answers are off", self.path)
            self._mtime, self._by_question, self._ids = mtime, {}, set()
            return
        answers = data.get("answers") or {}
        self._by_question = {normalise(a.get("question", "")): a for a in answers.values() if isinstance(a, dict) and a.get("response")}
        self._ids = {k for k, a in answers.items() if isinstance(a, dict) and a.get("response")}
        self.generated_at = data.get("generated_at")
        self._mtime = mtime

    def has(self, question_id: str) -> bool:
        if not self.enabled:
            return False
        self._load()
        return question_id in self._ids

    def get(self, question: str) -> dict[str, Any] | None:
        """The saved response for a question, or None."""
        if not self.enabled:
            return None
        self._load()
        hit = self._by_question.get(normalise(question))
        if hit is None:
            return None
        return {**hit["response"], "cached": True, "cached_at": hit.get("prepared_at") or self.generated_at}
