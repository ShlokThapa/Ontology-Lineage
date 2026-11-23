"""Application settings, loaded from environment variables / `.env`."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Look for .env in the repo root first, then in backend/.
        env_file=(REPO_DIR / ".env", BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    mode: Literal["live", "snapshot"] = "live"

    neo4j_uri: str = "neo4j+s://localhost:7687"
    # NEO4J_USERNAME is what the Aura credentials file uses.
    neo4j_user: str = Field(default="neo4j", validation_alias=AliasChoices("NEO4J_USER", "NEO4J_USERNAME"))
    neo4j_password: str = ""
    neo4j_database: str = "neo4j"

    # Comma-separated list in the env var, e.g. "http://localhost:5173,https://my.app"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    max_rows: int = Field(default=1000, ge=1, le=100_000)
    query_timeout_seconds: float = Field(default=30, gt=0, le=600)

    # --- Chatbot (Azure OpenAI) ------------------------------------------------
    # Names follow the .env this project already uses; the usual AZURE_OPENAI_* spellings also work.
    azure_openai_key: str = Field(
        default="", validation_alias=AliasChoices("AZURE_OPEN_AI_KEY", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_KEY")
    )
    azure_openai_endpoint: str = Field(
        default="", validation_alias=AliasChoices("AZURE_OPEN_AI_ENDPOINT", "AZURE_OPENAI_ENDPOINT")
    )
    # The Azure *deployment* name (what the API calls `model`).
    azure_openai_model: str = Field(
        default="",
        validation_alias=AliasChoices("AZURE_OPEN_AI_MODEL_NAME", "AZURE_OPENAI_DEPLOYMENT", "AZURE_OPENAI_MODEL"),
    )
    azure_openai_version: str = Field(
        default="2024-12-01-preview",
        validation_alias=AliasChoices("AZURE_OPEN_AI_VERSION", "AZURE_OPENAI_API_VERSION"),
    )
    chat_max_tool_rounds: int = Field(default=6, ge=1, le=12)
    chat_timeout_seconds: float = Field(default=90, gt=0, le=600)

    # Questions are embedded locally with the SAME model that embedded the policy chunks in Neo4j.
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    model_cache_dir: Path = REPO_DIR / ".model_cache"

    snapshot_dir: Path = REPO_DIR / "data"
    # Saved search phrases for the Bloom page
    phrases_file: Path = REPO_DIR / "data" / "search_phrases.json"
    # Directory containing the built React app (served by FastAPI in production).
    static_dir: Path = REPO_DIR / "frontend" / "dist"

    @field_validator("mode", mode="before")
    @classmethod
    def _lower_mode(cls, v: str) -> str:
        return v.strip().lower() if isinstance(v, str) else v

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def neo4j_host(self) -> str:
        """Host part of the bolt URI (without scheme / port), for display."""
        uri = self.neo4j_uri
        if "://" in uri:
            uri = uri.split("://", 1)[1]
        return uri.split("/", 1)[0].split(":", 1)[0]


@lru_cache
def get_settings() -> Settings:
    return Settings()
