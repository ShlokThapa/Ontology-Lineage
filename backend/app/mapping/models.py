"""Builds the mapper / verifier / tie-breaker models from the settings (.env).

One model is enough: model 1 plays every role. Models 2 and 3 are optional extras, worth adding when model 1's
token limit becomes a problem (the verifier and tie-breaker calls then move to them) or when you want a verdict
from a different model than the one that proposed the link.
"""

from __future__ import annotations

from ..chat.llm import AzureChatLLM
from ..config import Settings
from .engine import Models


def load_models(settings: Settings) -> Models:
    """Model 1 maps (required). Verifier and tie-breaker are model 2 and 3 if set, otherwise model 1 again."""
    mapper = AzureChatLLM(settings, 1)  # raises LLMNotConfigured naming what is missing
    verifier = AzureChatLLM(settings, 2) if settings.llm_slot(2)["model"] else mapper
    adjudicator = AzureChatLLM(settings, 3) if settings.llm_slot(3)["model"] else mapper
    return Models(mapper, verifier, adjudicator)
