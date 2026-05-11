"""Langfuse SDK initialization for MediVoice (Langfuse v4).

Call setup_langfuse() once at app startup.
Credentials are passed explicitly so they come from app Settings,
not from global env vars (which the OTel collector might also read).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langfuse import Langfuse

logger = logging.getLogger(__name__)

_langfuse: Langfuse | None = None


def setup_langfuse(
    public_key: str,
    secret_key: str,
    host: str = "https://cloud.langfuse.com",
) -> Langfuse | None:
    """Initialize Langfuse SDK. Returns None if keys are missing or invalid."""
    global _langfuse

    if not public_key or not public_key.startswith("pk-lf-"):
        logger.info("Langfuse disabled — LANGFUSE_PUBLIC_KEY not set")
        return None
    if not secret_key or not secret_key.startswith("sk-lf-"):
        logger.info("Langfuse disabled — LANGFUSE_SECRET_KEY not set")
        return None

    try:
        from langfuse import Langfuse

        # Langfuse v4: host= and base_url= are both accepted
        _langfuse = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=host,
        )
        logger.info("Langfuse SDK initialized → %s", host)
        return _langfuse
    except Exception as e:
        logger.warning("Langfuse init failed: %s — LLM observability disabled", e)
        return None


def get_langfuse() -> Langfuse | None:
    return _langfuse


def is_enabled() -> bool:
    return _langfuse is not None


def shutdown_langfuse() -> None:
    if _langfuse is not None:
        try:
            _langfuse.shutdown()
        except Exception:
            pass
