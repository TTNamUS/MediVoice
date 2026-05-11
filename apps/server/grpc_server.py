"""MediVoice gRPC server — runs on :50051 alongside FastAPI on :8000.

Services:
  Check      → Qdrant + Postgres health
  GetMetrics → TTFB p50/p95 from in-memory ring buffer
  TriggerEval → spawns offline_eval.py subprocess

gRPC reflection enabled so grpcurl works without the .proto file:
  grpcurl -plaintext localhost:50051 medivoice.MediVoice/Check

Run standalone (for testing):
  python grpc_server.py

In production: started from main.py lifespan alongside uvicorn.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import grpc
from grpc_reflection.v1alpha import reflection

logger = logging.getLogger(__name__)

# ── In-memory TTFB ring buffer ────────────────────────────────────────────────
# Populated by MetricsBridge via record_ttfb(); read by GetMetrics RPC.
_TTFB_BUFFER: deque[dict] = deque(maxlen=2000)
_SESSION_COUNTER: dict[str, int] = {"browser": 0, "sip": 0}


def record_ttfb(ttfb_ms: float, transport: str = "browser") -> None:
    """Called by MetricsBridge to push TTFB measurements into the ring buffer."""
    import time

    _TTFB_BUFFER.append({"ttfb_ms": ttfb_ms, "transport": transport, "ts": time.time()})


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    idx = int(len(sorted_vals) * p / 100)
    return sorted_vals[min(idx, len(sorted_vals) - 1)]


# ── Generated stub import (with fallback for environments without compiled protos) ──

try:
    import sys

    _PROTO_DIR = str(Path(__file__).parent / "proto" / "generated")
    if _PROTO_DIR not in sys.path:
        sys.path.insert(0, _PROTO_DIR)
    import medivoice_pb2
    import medivoice_pb2_grpc

    _STUBS_AVAILABLE = True
except ImportError:
    _STUBS_AVAILABLE = False
    logger.warning(
        "gRPC stubs not compiled. Run: "
        "python -m grpc_tools.protoc -I proto --python_out=proto/generated "
        "--grpc_python_out=proto/generated proto/medivoice.proto"
    )


# ── Servicer implementation ───────────────────────────────────────────────────


class MediVoiceServicer:
    """Implements the MediVoice gRPC service."""

    async def Check(self, request, context):
        checks = []
        overall_ok = True

        # Postgres health
        try:
            from db.pool import get_pool

            pool = get_pool()
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            checks.append({"name": "postgres", "ok": True, "message": "reachable"})
        except Exception as e:
            checks.append({"name": "postgres", "ok": False, "message": str(e)[:120]})
            overall_ok = False

        # Qdrant health
        try:
            import httpx
            from config import get_settings

            settings = get_settings()
            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.get(f"{settings.qdrant_url}/readyz")
                resp.raise_for_status()
            checks.append({"name": "qdrant", "ok": True, "message": "reachable"})
        except Exception as e:
            checks.append({"name": "qdrant", "ok": False, "message": str(e)[:120]})
            overall_ok = False

        if _STUBS_AVAILABLE:
            status = medivoice_pb2.SERVING if overall_ok else medivoice_pb2.NOT_SERVING
            pb_checks = [
                medivoice_pb2.HealthCheck(name=c["name"], ok=c["ok"], message=c["message"])
                for c in checks
            ]
            return medivoice_pb2.HealthResponse(status=status, checks=pb_checks)

        # Fallback: return dict (used by test harness)
        return {
            "status": "SERVING" if overall_ok else "NOT_SERVING",
            "checks": checks,
        }

    async def GetMetrics(self, request, context):
        import time

        window_minutes = max(1, getattr(request, "window_minutes", 5) or 5)
        cutoff = time.time() - (window_minutes * 60)

        recent = [e for e in _TTFB_BUFFER if e["ts"] >= cutoff]
        ttfb_values = [e["ttfb_ms"] for e in recent]

        p50 = _percentile(ttfb_values, 50)
        p95 = _percentile(ttfb_values, 95)

        transport_counts: dict[str, int] = {}
        for e in recent:
            t = e.get("transport", "unknown")
            transport_counts[t] = transport_counts.get(t, 0) + 1

        active = sum(_SESSION_COUNTER.values())
        total_24h = len([e for e in _TTFB_BUFFER if e["ts"] >= time.time() - 86400])

        if _STUBS_AVAILABLE:
            return medivoice_pb2.MetricsResponse(
                p50_ms=round(p50, 1),
                p95_ms=round(p95, 1),
                active_sessions=active,
                total_turns_24h=total_24h,
                sample_count=len(ttfb_values),
                transport_breakdown=json.dumps(transport_counts),
            )
        return {
            "p50_ms": round(p50, 1),
            "p95_ms": round(p95, 1),
            "active_sessions": active,
            "total_turns_24h": total_24h,
            "sample_count": len(ttfb_values),
            "transport_breakdown": json.dumps(transport_counts),
        }

    async def TriggerEval(self, request, context):
        job_id = str(uuid.uuid4())[:8]
        limit = getattr(request, "limit", 0) or 0
        category = getattr(request, "category", "") or ""

        cmd = [
            "python",
            "-m",
            "eval.runners.offline_eval",
        ]
        if limit > 0:
            cmd += ["--limit", str(limit)]
        if category:
            cmd += ["--category", category]

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(Path(__file__).parent),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info("Eval job %s started (pid=%d)", job_id, proc.pid)
            msg = f"Eval job {job_id} started (pid={proc.pid})"
            status = "started"
        except Exception as e:
            msg = f"Failed to start eval: {e}"
            status = "error"
            logger.exception(msg)

        if _STUBS_AVAILABLE:
            return medivoice_pb2.EvalResponse(job_id=job_id, status=status, message=msg)
        return {"job_id": job_id, "status": status, "message": msg}


# ── Server lifecycle ──────────────────────────────────────────────────────────


async def serve(port: int = 50051) -> grpc.aio.Server:
    """Start the async gRPC server. Returns the server object."""
    if not _STUBS_AVAILABLE:
        logger.error("Cannot start gRPC server: stubs not compiled. See warning above.")
        return None

    server = grpc.aio.server(
        ThreadPoolExecutor(max_workers=4),
        options=[
            ("grpc.max_send_message_length", 4 * 1024 * 1024),
            ("grpc.max_receive_message_length", 4 * 1024 * 1024),
        ],
    )
    servicer = MediVoiceServicer()
    medivoice_pb2_grpc.add_MediVoiceServicer_to_server(servicer, server)

    # gRPC reflection — lets grpcurl introspect without .proto file
    service_names = (
        medivoice_pb2.DESCRIPTOR.services_by_name["MediVoice"].full_name,
        reflection.SERVICE_NAME,
    )
    reflection.enable_server_reflection(service_names, server)

    server.add_insecure_port(f"[::]:{port}")
    await server.start()
    logger.info("gRPC server started on :%d (reflection enabled)", port)
    return server


async def stop(server: grpc.aio.Server) -> None:
    if server:
        await server.stop(grace=5)
        logger.info("gRPC server stopped")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    async def _main():
        server = await serve()
        if server:
            await server.wait_for_termination()

    asyncio.run(_main())
