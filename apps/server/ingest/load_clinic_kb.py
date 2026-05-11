"""Ingest clinic KB into Qdrant with dense + sparse vectors.

Usage:
    python -m ingest.load_clinic_kb            # incremental upsert
    python -m ingest.load_clinic_kb --reset    # drop collection + re-ingest
    python -m ingest.load_clinic_kb --dry-run  # print cost estimate only

Dense:  voyage-3 embeddings (1024-dim) via Voyage AI
        or gemini-embedding-2 embeddings (1536-dim by default, Gemini)
        Fallback: text-embedding-3-small (1536-dim, OpenAI)
Sparse: BM25 weights built from corpus, stored as Qdrant SparseVector
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Any

# Allow running as `python -m ingest.load_clinic_kb` from apps/server/
sys.path.insert(0, str(Path(__file__).parent.parent))
# Load .env.local from the server dir
_env_file = Path(__file__).parent.parent / ".env.local"
if _env_file.exists():
    from dotenv import load_dotenv

    load_dotenv(_env_file)

from ingest.bm25_index import BM25Index  # noqa: E402
from ingest.chunker import Chunk, chunk_directory  # noqa: E402

KB_DIR = Path(__file__).parent.parent.parent.parent / "data" / "clinic_kb"
REPORT_DIR = Path(__file__).parent.parent.parent.parent / "eval" / "reports"

DENSE_DIM = 1024  # voyage-3
DENSE_DIM_GEMINI = 1536  # gemini-embedding-2, override with GEMINI_EMBEDDING_DIM
DENSE_DIM_FALLBACK = 1536  # text-embedding-3-small
BATCH_SIZE = 64
GEMINI_BATCH_SIZE = 16
GEMINI_EMBEDDING_MODEL = "gemini-embedding-2"


# ── Embedding helpers ──────────────────────────────────────────────────────────


def _embed_voyage(texts: list[str], api_key: str) -> list[list[float]]:
    import httpx

    resp = httpx.post(
        "https://api.voyageai.com/v1/embeddings",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": "voyage-3", "input": texts},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    return [item["embedding"] for item in sorted(data, key=lambda x: x["index"])]


def _embed_openai_fallback(texts: list[str], api_key: str) -> list[list[float]]:
    import httpx

    resp = httpx.post(
        "https://api.openai.com/v1/embeddings",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": "text-embedding-3-small", "input": texts},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    return [item["embedding"] for item in sorted(data, key=lambda x: x["index"])]


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


def _format_gemini_document(text: str, title: str | None = None) -> str:
    title = title or "none"
    return f"title: {title} | text: {text}"


def _embed_gemini_documents(
    texts: list[str],
    api_key: str,
    titles: list[str | None] | None = None,
) -> tuple[list[list[float]], str, int]:
    model = _gemini_model()
    dim = _gemini_dim()
    model_path = f"models/{model}"
    titles = titles or [None] * len(texts)

    requests: list[dict[str, Any]] = []
    for text, title in zip(texts, titles):
        request: dict[str, Any] = {
            "model": model_path,
            "content": {"parts": [{"text": _format_gemini_document(text, title)}]},
            "outputDimensionality": dim,
        }
        if model == "gemini-embedding-001":
            request["content"] = {"parts": [{"text": text}]}
            request["taskType"] = "RETRIEVAL_DOCUMENT"
            if title:
                request["title"] = title
        requests.append(request)

    resp = _post_with_retries(
        f"https://generativelanguage.googleapis.com/v1beta/{model_path}:batchEmbedContents",
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        payload={"requests": requests},
        timeout=60,
    )
    embeddings = resp.json()["embeddings"]
    return [item["values"] for item in embeddings], model, dim


def _post_with_retries(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: int,
) -> Any:
    import httpx

    max_retries = _env_int("GEMINI_EMBEDDING_MAX_RETRIES", 6)
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
            delay = min(2**attempt, 30) + random.uniform(0, 0.5)
        print(
            f"  [warn] Gemini embeddings returned HTTP {resp.status_code}; "
            f"retrying in {delay:.1f}s ({attempt + 1}/{max_retries})"
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


def embed_batch(
    texts: list[str],
    titles: list[str | None] | None = None,
) -> tuple[list[list[float]], str, int]:
    """Embed a batch. Returns (embeddings, model_used, dim)."""
    voyage_key = os.getenv("VOYAGE_API_KEY", "")
    gemini_key = os.getenv("GEMINI_API_KEY", "")
    openai_key = os.getenv("OPENAI_API_KEY", "")
    explicit_provider = os.getenv("EMBEDDING_PROVIDER", "auto").strip().lower() != "auto"

    for provider in _embedding_provider_order():
        try:
            if provider == "voyage" and voyage_key:
                return _embed_voyage(texts, voyage_key), "voyage-3", DENSE_DIM
            if provider == "gemini" and gemini_key:
                return _embed_gemini_documents(texts, gemini_key, titles)
            if provider == "openai" and openai_key:
                return (
                    _embed_openai_fallback(texts, openai_key),
                    "text-embedding-3-small",
                    DENSE_DIM_FALLBACK,
                )
        except Exception as e:
            if explicit_provider:
                raise
            print(f"  [warn] {provider} embeddings failed ({e}), trying next provider")

    raise RuntimeError(
        "No embedding API key found. Set VOYAGE_API_KEY, GEMINI_API_KEY, or OPENAI_API_KEY."
    )


def _batch_size_for_model(model_name: str) -> int:
    if model_name.startswith("gemini"):
        return _env_int("GEMINI_EMBEDDING_BATCH_SIZE", GEMINI_BATCH_SIZE)
    return _env_int("EMBEDDING_BATCH_SIZE", BATCH_SIZE)


# ── Qdrant helpers ─────────────────────────────────────────────────────────────


def _get_qdrant_client():
    from qdrant_client import QdrantClient

    url = os.getenv("QDRANT_URL", "http://localhost:6333")
    return QdrantClient(url=url)


def _ensure_collection(client, collection: str, dense_dim: int) -> None:
    from qdrant_client.models import (
        Distance,
        SparseIndexParams,
        SparseVectorParams,
        VectorParams,
    )

    existing = [c.name for c in client.get_collections().collections]
    if collection in existing:
        _validate_collection_dim(client, collection, dense_dim)
        return
    client.create_collection(
        collection_name=collection,
        vectors_config={"dense": VectorParams(size=dense_dim, distance=Distance.COSINE)},
        sparse_vectors_config={"sparse": SparseVectorParams(index=SparseIndexParams())},
    )
    print(f"  Created collection '{collection}' (dense_dim={dense_dim})")


def _validate_collection_dim(client, collection: str, dense_dim: int) -> None:
    try:
        info = client.get_collection(collection)
        params = info.config.params
        vectors = params.vectors
        dense_config = vectors.get("dense") if isinstance(vectors, dict) else vectors
        existing_dim = getattr(dense_config, "size", None)
    except Exception:
        return

    if existing_dim is not None and existing_dim != dense_dim:
        raise RuntimeError(
            f"Collection '{collection}' dense vector size is {existing_dim}, "
            f"but selected embedding model produces {dense_dim}. "
            "Run ingestion with --reset or use a different QDRANT_COLLECTION."
        )


def _reset_collection(client, collection: str, dense_dim: int) -> None:
    from qdrant_client.models import (
        Distance,
        SparseIndexParams,
        SparseVectorParams,
        VectorParams,
    )

    try:
        client.delete_collection(collection)
    except Exception:
        pass
    client.create_collection(
        collection_name=collection,
        vectors_config={"dense": VectorParams(size=dense_dim, distance=Distance.COSINE)},
        sparse_vectors_config={"sparse": SparseVectorParams(index=SparseIndexParams())},
    )
    print(f"  Reset + recreated collection '{collection}'")


def _point_id(chunk: Chunk) -> int:
    return int(chunk.id[:8], 16)


def _existing_matching_point_ids(
    client,
    collection: str,
    chunks: list[Chunk],
    model_name: str,
    dense_dim: int,
) -> set[int]:
    existing_ids: set[int] = set()
    point_to_chunk = {_point_id(chunk): chunk for chunk in chunks}
    point_ids = list(point_to_chunk.keys())
    lookup_batch_size = _env_int("QDRANT_RESUME_LOOKUP_BATCH_SIZE", 256)

    for i in range(0, len(point_ids), lookup_batch_size):
        records = client.retrieve(
            collection_name=collection,
            ids=point_ids[i : i + lookup_batch_size],
            with_payload=True,
            with_vectors=False,
        )
        for record in records:
            point_id = int(record.id)
            chunk = point_to_chunk.get(point_id)
            payload = record.payload or {}
            if chunk is None or payload.get("text") != chunk.text:
                continue

            payload_model = payload.get("embedding_model")
            payload_dim = payload.get("dense_dim")
            if payload_model is None and payload_dim is None:
                existing_ids.add(point_id)
            elif payload_model == model_name and payload_dim == dense_dim:
                existing_ids.add(point_id)

    return existing_ids


def _upsert_batch(
    client,
    collection: str,
    chunks: list[Chunk],
    dense_vecs: list[list[float]],
    bm25: BM25Index,
    model_name: str,
    dense_dim: int,
) -> None:
    from qdrant_client.models import PointStruct, SparseVector

    points = []
    for chunk, dense in zip(chunks, dense_vecs):
        sparse_dict = bm25.encode(chunk.text, doc_len=chunk.tokens * 4)
        if not sparse_dict:
            sparse_dict = {0: 0.001}  # Qdrant requires non-empty sparse vector

        points.append(
            PointStruct(
                id=int(chunk.id[:8], 16),  # convert first 8 hex chars → uint64
                vector={
                    "dense": dense,
                    "sparse": SparseVector(
                        indices=list(sparse_dict.keys()),
                        values=list(sparse_dict.values()),
                    ),
                },
                payload={
                    "chunk_id": chunk.id,
                    "doc_id": chunk.doc_id,
                    "title": chunk.title,
                    "category": chunk.category,
                    "tags": chunk.tags,
                    "text": chunk.text,
                    "tokens": chunk.tokens,
                    "source_path": chunk.source_path,
                    "embedding_model": model_name,
                    "dense_dim": dense_dim,
                },
            )
        )
    client.upsert(collection_name=collection, points=points)


# ── Main ───────────────────────────────────────────────────────────────────────


def main(reset: bool = False, dry_run: bool = False) -> None:
    collection = os.getenv("QDRANT_COLLECTION", "clinic_kb_v1")

    print("\nMediVoice KB Ingestion")
    print(f"  KB dir:     {KB_DIR}")
    print(f"  Collection: {collection}")
    print(f"  Reset:      {reset}\n")

    # 1. Chunk all docs
    t0 = time.perf_counter()
    chunks = chunk_directory(KB_DIR)
    print(f"  Chunked {len(chunks)} chunks from {KB_DIR}")

    if not chunks:
        print("  ERROR: No chunks found. Check KB_DIR path.")
        sys.exit(1)

    # 2. Build BM25 index from corpus
    corpus = [c.text for c in chunks]
    bm25 = BM25Index(corpus)
    bm25_pkl = Path(__file__).parent / "bm25_weights.pkl"
    bm25.save(bm25_pkl)
    print(f"  BM25 index: {len(bm25.vocab)} tokens → saved to {bm25_pkl.name}")

    # 3. Corpus size summary
    total_tokens = sum(c.tokens for c in chunks)
    print(f"  Tokens: {total_tokens}\n")

    if dry_run:
        print("  --dry-run: stopping here. No Qdrant writes.")
        return

    # 4. Sample-embed first batch to detect model + dim
    sample_texts = [chunks[0].text]
    _, model_name, dense_dim = embed_batch(sample_texts, titles=[chunks[0].title])
    print(f"  Embedding model: {model_name} (dim={dense_dim})")
    batch_size = _batch_size_for_model(model_name)
    print(f"  Embedding batch size: {batch_size}")

    # 5. Setup Qdrant collection
    client = _get_qdrant_client()
    if reset:
        _reset_collection(client, collection, dense_dim)
    else:
        _ensure_collection(client, collection, dense_dim)

    skipped_existing = 0
    chunks_to_ingest = chunks
    if not reset:
        existing_ids = _existing_matching_point_ids(
            client, collection, chunks, model_name, dense_dim
        )
        skipped_existing = len(existing_ids)
        if skipped_existing:
            chunks_to_ingest = [chunk for chunk in chunks if _point_id(chunk) not in existing_ids]
            print(f"  Resume: {skipped_existing}/{len(chunks)} chunks already present")

    # 6. Embed + upsert in batches
    upserted = 0
    for i in range(0, len(chunks_to_ingest), batch_size):
        batch = chunks_to_ingest[i : i + batch_size]
        texts = [c.text for c in batch]
        titles = [c.title for c in batch]
        try:
            dense_vecs, _, _ = embed_batch(texts, titles=titles)
        except Exception as e:
            raise RuntimeError(f"Embedding batch {i // batch_size} failed after retries") from e
        _upsert_batch(client, collection, batch, dense_vecs, bm25, model_name, dense_dim)
        upserted += len(batch)
        complete_count = skipped_existing + upserted
        print(f"  Upserted {complete_count}/{len(chunks)} chunks…")
        time.sleep(0.2)  # gentle rate-limit jitter

    elapsed = time.perf_counter() - t0

    # 7. Write ingest report
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "chunks": len(chunks),
        "upserted_chunks": upserted,
        "skipped_existing_chunks": skipped_existing,
        "complete": skipped_existing + upserted == len(chunks),
        "docs": len({c.doc_id for c in chunks}),
        "total_tokens": total_tokens,
        "embedding_model": model_name,
        "dense_dim": dense_dim,
        "bm25_vocab_size": len(bm25.vocab),
        "collection": collection,
        "elapsed_s": round(elapsed, 1),
    }
    report_path = REPORT_DIR / "ingest_run.json"
    report_path.write_text(json.dumps(report, indent=2))

    print(
        f"\n  Ingested {skipped_existing + upserted}/{len(chunks)} chunks from {report['docs']} docs."
    )
    if skipped_existing:
        print(f"  Resumed: skipped {skipped_existing} existing chunks, upserted {upserted}.")
    print(f"  Dense: {model_name}. Sparse: BM25.")
    print(f"  Elapsed: {elapsed:.1f}s | Report: {report_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest clinic KB into Qdrant")
    parser.add_argument("--reset", action="store_true", help="Drop and recreate collection")
    parser.add_argument("--dry-run", action="store_true", help="Print cost estimate, no writes")
    args = parser.parse_args()
    main(reset=args.reset, dry_run=args.dry_run)
