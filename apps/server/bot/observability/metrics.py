"""Prometheus metrics registry for MediVoice.

All metrics are defined here once and imported wherever needed.
Call generate_metrics_output() in the /metrics FastAPI endpoint.
"""

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# Use a dedicated registry to avoid conflicts with default global registry
# (which also collects Python process metrics we don't want to expose).
REGISTRY = CollectorRegistry()

# ── TTFB histograms — per service, per entrypoint ─────────────────────────────
# Buckets tuned for voice: 100ms increments up to 2s, then coarser.
_TTFB_BUCKETS = (100, 200, 300, 500, 800, 1000, 1200, 1500, 2000, float("inf"))

ttfb_histogram = Histogram(
    "medivoice_ttfb_ms",
    "Time-to-first-byte per service stage in milliseconds",
    ["service", "provider", "entrypoint"],
    buckets=_TTFB_BUCKETS,
    registry=REGISTRY,
)

# ── LLM token counters ────────────────────────────────────────────────────────
llm_tokens_in = Counter(
    "medivoice_llm_tokens_in",
    "Total LLM input (prompt) tokens",
    ["provider", "model"],
    registry=REGISTRY,
)

llm_tokens_out = Counter(
    "medivoice_llm_tokens_out",
    "Total LLM output (completion) tokens",
    ["provider", "model"],
    registry=REGISTRY,
)

# ── TTS character counter ─────────────────────────────────────────────────────
tts_chars_out = Counter(
    "medivoice_tts_chars_out",
    "Total characters sent to TTS service",
    ["provider"],
    registry=REGISTRY,
)

# ── Agent turn counter ────────────────────────────────────────────────────────
agent_turns = Counter(
    "medivoice_agent_turns",
    "Number of conversation turns handled per agent",
    ["agent"],
    registry=REGISTRY,
)

# ── Interruption counter ──────────────────────────────────────────────────────
interruptions = Counter(
    "medivoice_interruptions",
    "Number of caller barge-in / interruption events",
    ["entrypoint"],
    registry=REGISTRY,
)

# ── Active sessions gauge ─────────────────────────────────────────────────────
active_sessions = Gauge(
    "medivoice_active_sessions",
    "Number of currently active voice sessions",
    ["transport"],
    registry=REGISTRY,
)

# ── Service health ────────────────────────────────────────────────────────────
service_up = Gauge(
    "medivoice_up",
    "Whether the MediVoice API process is up (1 = up)",
    registry=REGISTRY,
)
service_up.set(1)


def generate_metrics_output() -> tuple[bytes, str]:
    """Return (body, content_type) for the /metrics endpoint."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
