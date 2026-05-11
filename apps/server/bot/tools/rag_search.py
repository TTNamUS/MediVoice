"""Hybrid RAG search tool: BM25 sparse + dense → RRF fusion → cross-encoder rerank.

Pipeline:
  1. Dense search  — configured query embedding → top-20 from Qdrant "dense" vector
  2. Sparse search — BM25 sparse vector       → top-20 from Qdrant "sparse" vector
  3. RRF fusion    — merge two ranked lists using 1/(k + rank) with k=60 → top-10
  4. Cross-encoder rerank — ms-marco-MiniLM-L-6-v2 → top-3 returned to LLM

Fallback: if cross-encoder fails → return RRF top-3 without reranking.

Tool schema returned to LLM:
    search_clinic_kb(query: str) -> list[{title, snippet, doc_id, score}]
"""

from __future__ import annotations

import logging
import os
import random
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from opentelemetry.trace import SpanKind

from bot.observability.otel_setup import get_tracer

# Load .env.local from the server dir — must happen before any config reads
_env_file = Path(__file__).parent.parent.parent / ".env.local"
if _env_file.exists():
    from dotenv import load_dotenv

    load_dotenv(_env_file)

logger = logging.getLogger(__name__)
tracer = get_tracer("medivoice.rag")

_TOP_N_EACH = 20  # candidates per retriever before fusion
_RRF_K = 60  # RRF constant
_RERANK_TOP_N = 10  # input to cross-encoder
_FINAL_TOP_N = 3  # returned to LLM
DENSE_DIM_GEMINI = 1536
GEMINI_EMBEDDING_MODEL = "gemini-embedding-2"


# ── Singleton clients (loaded once at server start) ───────────────────────────


@lru_cache(maxsize=1)
def _get_qdrant():
    from qdrant_client import QdrantClient

    url = os.getenv("QDRANT_URL", "http://localhost:6333")
    return QdrantClient(url=url)


@lru_cache(maxsize=1)
def _get_bm25():
    from pathlib import Path

    pkl = Path(__file__).parent.parent.parent / "ingest" / "bm25_weights.pkl"
    if not pkl.exists():
        raise FileNotFoundError(f"BM25 index not found at {pkl}. Run `make ingest-kb` first.")
    from ingest.bm25_index import BM25Index

    return BM25Index.load(pkl)


@lru_cache(maxsize=1)
def _get_cross_encoder():
    try:
        from sentence_transformers import CrossEncoder

        return CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
    except Exception as e:
        logger.warning("Cross-encoder unavailable (%s) — will use RRF only", e)
        return None


# ── Dense embedding (same model as ingest) ────────────────────────────────────


def _gemini_model() -> str:
    return os.getenv("GEMINI_EMBEDDING_MODEL", GEMINI_EMBEDDING_MODEL).strip()


def _gemini_dim() -> int:
    return _env_int("GEMINI_EMBEDDING_DIM", DENSE_DIM_GEMINI)


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name, str(default)).strip()
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _format_gemini_query(query: str) -> str:
    return f"task: question answering | query: {query}"


def _embed_gemini_query(query: str, api_key: str) -> list[float]:
    model = _gemini_model()
    dim = _gemini_dim()
    model_path = f"models/{model}"
    request: dict[str, Any] = {
        "model": model_path,
        "content": {"parts": [{"text": _format_gemini_query(query)}]},
        "outputDimensionality": dim,
    }
    if model == "gemini-embedding-001":
        request["content"] = {"parts": [{"text": query}]}
        request["taskType"] = "QUESTION_ANSWERING"

    resp = _post_gemini_with_retries(
        f"https://generativelanguage.googleapis.com/v1beta/{model_path}:embedContent",
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        payload=request,
        timeout=10,
    )
    return resp.json()["embedding"]["values"]


def _post_gemini_with_retries(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: int,
) -> Any:
    import httpx

    max_retries = _env_int("GEMINI_EMBEDDING_MAX_RETRIES", 3)
    retry_statuses = {429, 500, 502, 503, 504}

    for attempt in range(max_retries + 1):
        resp = httpx.post(url, headers=headers, json=payload, timeout=timeout)
        if resp.status_code not in retry_statuses:
            resp.raise_for_status()
            return resp

        if attempt == max_retries:
            resp.raise_for_status()

        retry_after = resp.headers.get("retry-after")
        if retry_after and retry_after.isdigit():
            delay = float(retry_after)
        else:
            delay = min(2**attempt, 8) + random.uniform(0, 0.25)
        logger.warning(
            "Gemini embeddings returned HTTP %s; retrying in %.1fs (%d/%d)",
            resp.status_code,
            delay,
            attempt + 1,
            max_retries,
        )
        time.sleep(delay)

    raise RuntimeError("Gemini embeddings retry loop exited unexpectedly")


def _embedding_provider_order() -> list[str]:
    provider = os.getenv("EMBEDDING_PROVIDER", "auto").strip().lower()
    if provider == "auto":
        return ["voyage", "openai", "gemini"]
    if provider in {"voyage", "gemini", "openai"}:
        return [provider]
    raise ValueError("EMBEDDING_PROVIDER must be auto, voyage, gemini, or openai")


@lru_cache(maxsize=256)
def _embed_query(query: str) -> list[float]:
    """Cache identical queries to skip re-embedding."""
    voyage_key = os.getenv("VOYAGE_API_KEY", "")
    gemini_key = os.getenv("GEMINI_API_KEY", "")
    openai_key = os.getenv("OPENAI_API_KEY", "")
    explicit_provider = os.getenv("EMBEDDING_PROVIDER", "auto").strip().lower() != "auto"

    for provider in _embedding_provider_order():
        try:
            if provider == "voyage" and voyage_key:
                import httpx

                resp = httpx.post(
                    "https://api.voyageai.com/v1/embeddings",
                    headers={"Authorization": f"Bearer {voyage_key}"},
                    json={"model": "voyage-3", "input": [query]},
                    timeout=10,
                )
                resp.raise_for_status()
                return resp.json()["data"][0]["embedding"]

            if provider == "gemini" and gemini_key:
                return _embed_gemini_query(query, gemini_key)

            if provider == "openai" and openai_key:
                import httpx

                resp = httpx.post(
                    "https://api.openai.com/v1/embeddings",
                    headers={"Authorization": f"Bearer {openai_key}"},
                    json={"model": "text-embedding-3-small", "input": [query]},
                    timeout=10,
                )
                resp.raise_for_status()
                return resp.json()["data"][0]["embedding"]
        except Exception:
            if explicit_provider:
                raise
            logger.warning("%s embeddings failed; trying next provider", provider, exc_info=True)

    raise RuntimeError("No VOYAGE_API_KEY, GEMINI_API_KEY, or OPENAI_API_KEY set for RAG search.")


# ── Retrieval steps ────────────────────────────────────────────────────────────


@dataclass
class _Hit:
    doc_id: str
    title: str
    text: str
    score: float


def _points_from_query_response(response: Any) -> list[Any]:
    """Return scored points from either new or legacy Qdrant client responses."""
    return list(getattr(response, "points", response))


def _dense_query_points(client: Any, collection: str, vec: list[float], top_n: int) -> list[Any]:
    if hasattr(client, "query_points"):
        response = client.query_points(
            collection_name=collection,
            query=vec,
            using="dense",
            limit=top_n,
            with_payload=True,
        )
        return _points_from_query_response(response)

    return client.search(
        collection_name=collection,
        query_vector=("dense", vec),
        limit=top_n,
        with_payload=True,
    )


def _sparse_query_points(client: Any, collection: str, sparse_vector: Any, top_n: int) -> list[Any]:
    if hasattr(client, "query_points"):
        response = client.query_points(
            collection_name=collection,
            query=sparse_vector,
            using="sparse",
            limit=top_n,
            with_payload=True,
        )
        return _points_from_query_response(response)

    from qdrant_client.models import NamedSparseVector

    return client.search(
        collection_name=collection,
        query_vector=NamedSparseVector(name="sparse", vector=sparse_vector),
        limit=top_n,
        with_payload=True,
    )


def _hit_from_qdrant_point(point: Any) -> _Hit:
    payload = point.payload or {}
    return _Hit(
        doc_id=payload.get("doc_id", ""),
        title=payload.get("title", ""),
        text=payload.get("text", ""),
        score=point.score,
    )


def _dense_search(query: str, collection: str, top_n: int) -> list[_Hit]:
    vec = _embed_query(query)
    client = _get_qdrant()
    results = _dense_query_points(client, collection, vec, top_n)
    return [_hit_from_qdrant_point(r) for r in results]


def _sparse_search(query: str, collection: str, top_n: int) -> list[_Hit]:
    from qdrant_client.models import SparseVector

    bm25 = _get_bm25()
    sparse_dict = bm25.encode(query)
    if not sparse_dict:
        return []

    client = _get_qdrant()
    sparse_vector = SparseVector(
        indices=list(sparse_dict.keys()),
        values=list(sparse_dict.values()),
    )
    results = _sparse_query_points(client, collection, sparse_vector, top_n)
    return [_hit_from_qdrant_point(r) for r in results]


def _rrf_fusion(dense_hits: list[_Hit], sparse_hits: list[_Hit], top_n: int) -> list[_Hit]:
    """Reciprocal Rank Fusion: score = Σ 1/(k + rank_i)."""
    scores: dict[str, float] = {}
    texts: dict[str, _Hit] = {}

    for rank, hit in enumerate(dense_hits):
        key = hit.text[:80]  # dedup key
        scores[key] = scores.get(key, 0.0) + 1.0 / (_RRF_K + rank + 1)
        texts[key] = hit

    for rank, hit in enumerate(sparse_hits):
        key = hit.text[:80]
        scores[key] = scores.get(key, 0.0) + 1.0 / (_RRF_K + rank + 1)
        if key not in texts:
            texts[key] = hit

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_n]
    return [
        _Hit(
            doc_id=texts[k].doc_id,
            title=texts[k].title,
            text=texts[k].text,
            score=s,
        )
        for k, s in ranked
    ]


def _rerank(query: str, hits: list[_Hit]) -> list[_Hit]:
    """Cross-encoder rerank. Falls back to input order on failure."""
    encoder = _get_cross_encoder()
    if encoder is None or not hits:
        return hits

    try:
        pairs = [[query, h.text] for h in hits]
        scores = encoder.predict(pairs)
        ranked = sorted(zip(hits, scores), key=lambda x: x[1], reverse=True)
        return [h for h, _ in ranked]
    except Exception as e:
        logger.warning("Cross-encoder rerank failed (%s) — using RRF order", e)
        return hits


# ── Public search function ────────────────────────────────────────────────────


def search_dense_only(query: str, top_n: int = _FINAL_TOP_N) -> list[dict[str, Any]]:
    """Dense-only retrieval — used by eval for baseline comparison."""
    collection = os.getenv("QDRANT_COLLECTION", "clinic_kb_v1")
    hits = _dense_search(query, collection, top_n)
    return [
        {"title": h.title, "snippet": h.text[:300], "doc_id": h.doc_id, "score": h.score}
        for h in hits
    ]


async def search_clinic_kb(query: str) -> list[dict[str, Any]]:
    """Hybrid RAG search tool registered with the LLM.

    Returns top-3 chunks: [{title, snippet, doc_id, score}]
    """
    collection = os.getenv("QDRANT_COLLECTION", "clinic_kb_v1")

    with tracer.start_as_current_span("tool.search_clinic_kb", kind=SpanKind.INTERNAL) as span:
        span.set_attribute("query_len", len(query))

        # Step 1 & 2: dense + sparse retrieval (both top-20)
        dense_hits = _dense_search(query, collection, _TOP_N_EACH)
        span.set_attribute("dense_results", len(dense_hits))

        sparse_hits = _sparse_search(query, collection, _TOP_N_EACH)
        span.set_attribute("sparse_results", len(sparse_hits))

        # Step 3: RRF fusion → top-10
        fused = _rrf_fusion(dense_hits, sparse_hits, _RERANK_TOP_N)
        span.set_attribute("rrf_results", len(fused))

        # Step 4: Cross-encoder rerank → top-3
        t_rerank = time.perf_counter()
        reranked = _rerank(query, fused)[:_FINAL_TOP_N]
        rerank_ms = round((time.perf_counter() - t_rerank) * 1000, 1)
        span.set_attribute("rerank_ms", rerank_ms)

        top_score = reranked[0].score if reranked else 0.0
        span.set_attribute("final_results", len(reranked))
        span.set_attribute("top_score", round(top_score, 4))

        logger.debug(
            "RAG: query=%r dense=%d sparse=%d rrf=%d rerank_ms=%.1f final=%d",
            query[:60],
            len(dense_hits),
            len(sparse_hits),
            len(fused),
            rerank_ms,
            len(reranked),
        )

    return [
        {
            "title": h.title,
            "snippet": h.text[:400],
            "doc_id": h.doc_id,
            "score": round(h.score, 4),
        }
        for h in reranked
    ]


# ── Pipecat tool definition ───────────────────────────────────────────────────

SEARCH_KB_TOOL = {
    "name": "search_clinic_kb",
    "description": (
        "Search the Sunrise Dental Clinic knowledge base for information about "
        "services, policies, hours, doctors, location, or FAQs. "
        "Use this tool whenever the caller asks about clinic-specific information. "
        "Do NOT call it for personal patient data — use patient_lookup instead."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural-language search query, e.g. 'cancellation policy' or 'Dr. Lee hours'",
            }
        },
        "required": ["query"],
    },
}
