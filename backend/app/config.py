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
    # Extra deployments for the policy-mapping job (model 1 above is the mapper; 2 verifies; 3 breaks ties).
    # Endpoint / key / version are optional: left blank, they reuse model 1's.
    azure_openai_model_2: str = Field(default="", validation_alias=AliasChoices("AZURE_OPEN_AI_MODEL_NAME_2"))
    azure_openai_endpoint_2: str = Field(default="", validation_alias=AliasChoices("AZURE_OPEN_AI_ENDPOINT_2"))
    azure_openai_key_2: str = Field(default="", validation_alias=AliasChoices("AZURE_OPEN_AI_KEY_2"))
    azure_openai_version_2: str = Field(default="", validation_alias=AliasChoices("AZURE_OPEN_AI_VERSION_2"))
    azure_openai_model_3: str = Field(default="", validation_alias=AliasChoices("AZURE_OPEN_AI_MODEL_NAME_3"))
    azure_openai_endpoint_3: str = Field(default="", validation_alias=AliasChoices("AZURE_OPEN_AI_ENDPOINT_3"))
    azure_openai_key_3: str = Field(default="", validation_alias=AliasChoices("AZURE_OPEN_AI_KEY_3"))
    azure_openai_version_3: str = Field(default="", validation_alias=AliasChoices("AZURE_OPEN_AI_VERSION_3"))
    chat_max_tool_rounds: int = Field(default=6, ge=1, le=12)
    chat_timeout_seconds: float = Field(default=90, gt=0, le=600)

    # --- Jobs page: Fabric triggers -----------------------------------------------------------------
    # Which workspace: an id, or a name that is looked up (the project's workspace is called Ontology_Lineage).
    fabric_workspace_id: str = ""
    fabric_workspace_name: str = "Ontology_Lineage"
    # How to sign in. A service principal (added to the workspace as Contributor or higher) is the durable way;
    # FABRIC_ACCESS_TOKEN is a pasted bearer token for a quick try (it expires, usually within the hour).
    fabric_tenant_id: str = ""
    fabric_client_id: str = ""
    fabric_client_secret: str = ""
    fabric_access_token: str = ""
    fabric_api_url: str = "https://api.fabric.microsoft.com"
    fabric_login_url: str = "https://login.microsoftonline.com"
    # How notebook / pipeline parameters are sent: "array" ({"parameters": [{name, type, value}]}) as in the Fabric
    # reference, or "execution_data" ({"executionData": {"parameters": {name: {value, type}}}}), an older shape.
    fabric_param_style: Literal["array", "execution_data"] = "array"
    fabric_poll_seconds: float = Field(default=10, ge=0, le=300)
    fabric_step_timeout_minutes: int = Field(default=90, ge=1, le=1440)

    # The Jobs page may write to Neo4j (run the mapping with --write, accept / reject links, undo a run).
    # This is a proof of concept with no login; set MAPPING_UI_WRITE=false to make those buttons refuse.
    mapping_ui_write: bool = True

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

    def llm_slot(self, slot: int = 1) -> dict[str, str]:
        """Resolved connection settings for LLM model 1, 2 or 3. Slots 2 and 3 fall back to slot 1 for any
        endpoint / key / version they do not set themselves; an empty ``model`` means the slot is not configured."""
        if slot == 1:
            return {
                "endpoint": self.azure_openai_endpoint.strip(),
                "key": self.azure_openai_key.strip(),
                "model": self.azure_openai_model.strip(),
                "version": self.azure_openai_version.strip(),
            }
        pick = {
            2: (self.azure_openai_endpoint_2, self.azure_openai_key_2, self.azure_openai_model_2, self.azure_openai_version_2),
            3: (self.azure_openai_endpoint_3, self.azure_openai_key_3, self.azure_openai_model_3, self.azure_openai_version_3),
        }[slot]
        first = self.llm_slot(1)
        return {
            "endpoint": pick[0].strip() or first["endpoint"],
            "key": pick[1].strip() or first["key"],
            "model": pick[2].strip(),
            "version": pick[3].strip() or first["version"],
        }

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
