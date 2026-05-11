"""OpenTelemetry initialization for MediVoice server.

Call setup_tracing() once at app startup. After that, use:
    from opentelemetry import trace
    tracer = trace.get_tracer("medivoice.bot")
"""

import logging

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

logger = logging.getLogger(__name__)

_tracer_provider: TracerProvider | None = None


def setup_tracing(
    service_name: str,
    otlp_endpoint: str,
    app_env: str = "development",
) -> TracerProvider:
    global _tracer_provider

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": "0.1.0",
            "deployment.environment": app_env,
        }
    )

    provider = TracerProvider(resource=resource)

    # Always export to OTLP collector (→ Jaeger)
    try:
        otlp_exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
        logger.info("OTel OTLP exporter configured → %s", otlp_endpoint)
    except Exception as e:
        logger.warning("OTel OTLP exporter failed to init: %s — using console fallback", e)
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)
    _tracer_provider = provider
    return provider


def get_tracer(name: str = "medivoice.bot") -> trace.Tracer:
    return trace.get_tracer(name)


def shutdown_tracing() -> None:
    if _tracer_provider:
        _tracer_provider.shutdown()
