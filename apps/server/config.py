from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

LLMProvider = Literal["anthropic", "openai", "gemini"]
EmbeddingProvider = Literal["auto", "voyage", "gemini", "openai"]

# Default models per provider — override via env if needed
_DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-haiku-4-5-20251001",
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.0-flash",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).parent / ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── LLM provider selection ────────────────────────────────────────────────
    # Set LLM_PROVIDER to "anthropic" | "openai" | "gemini"
    llm_provider: LLMProvider = "anthropic"

    # Anthropic
    anthropic_api_key: str = ""
    anthropic_model: str = _DEFAULT_MODELS["anthropic"]

    # OpenAI
    openai_api_key: str = ""
    openai_model: str = _DEFAULT_MODELS["openai"]

    # Google Gemini
    gemini_api_key: str = ""
    gemini_model: str = _DEFAULT_MODELS["gemini"]

    # Deepgram
    deepgram_api_key: str = ""

    # Cartesia
    cartesia_api_key: str = ""
    cartesia_voice_id: str = "694f9389-aac1-45b6-b726-9d9369183238"

    # LiveKit (browser WebRTC + SIP)
    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""
    livekit_sip_trunk_id: str = ""

    # Langfuse
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    # OTel
    otel_exporter_otlp_endpoint: str = "http://localhost:4319"
    otel_service_name: str = "medivoice-server"

    # Dense embeddings
    voyage_api_key: str = ""
    embedding_provider: EmbeddingProvider = "auto"
    gemini_embedding_model: str = "gemini-embedding-2"
    gemini_embedding_dim: int = 1536

    # Qdrant
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "clinic_kb_v1"

    # Postgres
    database_url: str = "postgresql://medivoice:medivoice@localhost:5432/medivoice"

    # App
    app_env: str = "development"
    log_level: str = "INFO"

    def is_production(self) -> bool:
        return self.app_env == "production"

    def active_llm_api_key(self) -> str:
        return {
            "anthropic": self.anthropic_api_key,
            "openai": self.openai_api_key,
            "gemini": self.gemini_api_key,
        }[self.llm_provider]

    def active_llm_model(self) -> str:
        return {
            "anthropic": self.anthropic_model,
            "openai": self.openai_model,
            "gemini": self.gemini_model,
        }[self.llm_provider]

    def validate_critical(self) -> list[str]:
        """Return list of missing critical env vars."""
        missing = []
        # Active LLM provider key
        provider_key_field = f"{self.llm_provider}_api_key"
        if not getattr(self, provider_key_field):
            missing.append(provider_key_field.upper())
        # Always-required services
        for field in (
            "deepgram_api_key",
            "cartesia_api_key",
            "livekit_url",
            "livekit_api_key",
            "livekit_api_secret",
        ):
            if not getattr(self, field):
                missing.append(field.upper())
        return missing


@lru_cache
def get_settings() -> Settings:
    return Settings()
