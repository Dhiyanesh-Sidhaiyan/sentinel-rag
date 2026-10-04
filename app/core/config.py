"""Typed, environment-driven configuration (12-factor). Secrets are SecretStr and never logged."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # --- service ---
    app_name: str = "sentinel-rag"
    environment: Literal["local", "dev", "staging", "prod"] = "local"
    log_level: str = "INFO"
    log_json: bool = True
    cors_origins: list[str] = Field(default_factory=list)

    # --- backends ("memory" = zero-dependency local mode) ---
    vector_backend: Literal["memory", "qdrant"] = "memory"
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: SecretStr | None = None
    qdrant_collection: str = "knowledge_chunks"

    graph_backend: Literal["memory", "postgres"] = "memory"
    postgres_dsn: SecretStr | None = None
    postgres_pool_max: int = 10

    cache_backend: Literal["memory", "redis"] = "memory"
    redis_url: SecretStr = SecretStr("redis://localhost:6379/0")

    # --- models ---
    llm_provider: Literal["offline", "openai"] = "offline"
    llm_base_url: str | None = None  # any OpenAI-compatible gateway (vLLM, LiteLLM, Azure, internal gateway)
    llm_api_key: SecretStr | None = None
    llm_model: str = "gpt-4o-mini"
    llm_timeout_s: float = 30.0
    llm_max_retries: int = 2
    llm_max_concurrency: int = 16

    embedding_provider: Literal["hash", "openai"] = "hash"
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 512
    embedding_batch_size: int = 64

    # --- RAG / agent ---
    chunk_size: int = 900
    chunk_overlap: int = 150
    retrieval_candidates: int = 12
    final_top_k: int = 5
    relevance_threshold: float = 0.2
    grounding_threshold: float = 0.6
    max_agent_iterations: int = 2
    graph_hops: int = 2
    graph_fact_limit: int = 25

    # --- security ---
    auth_enabled: bool = True
    # JSON map of sha256(api_key) -> tenant_id. Raw keys are never stored.
    api_keys_sha256: dict[str, str] = Field(default_factory=dict)
    rate_limit_per_minute: int = 60
    max_question_chars: int = 2000
    max_document_chars: int = 500_000
    max_upload_bytes: int = 5 * 1024 * 1024
    injection_block_threshold: float = 0.7
    answer_cache_ttl_s: int = 300

    @model_validator(mode="after")
    def _validate_backends(self) -> "Settings":
        if self.graph_backend == "postgres" and self.postgres_dsn is None:
            raise ValueError("POSTGRES_DSN is required when GRAPH_BACKEND=postgres")
        if self.llm_provider == "openai" and self.llm_api_key is None:
            raise ValueError("LLM_API_KEY is required when LLM_PROVIDER=openai")
        if self.environment == "prod" and not self.auth_enabled:
            raise ValueError("AUTH_ENABLED must be true in prod")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
