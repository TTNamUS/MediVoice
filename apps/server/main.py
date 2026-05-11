"""MediVoice FastAPI application entry point."""

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from config import get_settings
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from api.connect import router as connect_router
from api.livekit import router as livekit_router
from bot.observability.langfuse_setup import setup_langfuse, shutdown_langfuse
from bot.observability.metrics import generate_metrics_output
from bot.observability.otel_setup import setup_tracing, shutdown_tracing
from db.pool import close_pool, init_pool

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()

    # Validate critical keys on startup
    missing = settings.validate_critical()
    if missing:
        logger.warning("Missing critical env vars: %s", ", ".join(missing))

    # Initialize OTel tracing
    setup_tracing(
        service_name=settings.otel_service_name,
        otlp_endpoint=settings.otel_exporter_otlp_endpoint,
        app_env=settings.app_env,
    )
    # Initialize Langfuse SDK (no-op if keys not set)
    setup_langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=settings.langfuse_host,
    )
    # Initialize Postgres connection pool
    await init_pool(settings.database_url)

    # Start gRPC server on :50051 (non-blocking — runs in background)
    grpc_server = None
    try:
        from grpc_server import serve as grpc_serve

        grpc_server = await grpc_serve(port=50051)
    except Exception as e:
        logger.warning("gRPC server not started (stubs may not be compiled): %s", e)

    logger.info("MediVoice server starting (env=%s)", settings.app_env)

    yield

    if grpc_server:
        from grpc_server import stop as grpc_stop

        await grpc_stop(grpc_server)
    await close_pool()
    shutdown_tracing()
    shutdown_langfuse()
    logger.info("MediVoice server shutdown complete")


app = FastAPI(
    title="MediVoice",
    description="Real-time voice AI receptionist for Sunrise Dental Clinic",
    version="0.1.0",
    lifespan=lifespan,
)
FastAPIInstrumentor.instrument_app(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(connect_router)
app.include_router(livekit_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "medivoice"}


@app.get("/")
async def root() -> dict[str, str]:
    return {"message": "MediVoice API — see /docs"}


@app.get("/metrics")
async def metrics() -> Response:
    body, content_type = generate_metrics_output()
    return Response(content=body, media_type=content_type)
