"""BM25 sparse vector builder for Qdrant ingestion.

Builds a corpus-level IDF table from all chunk texts, then converts
any query or chunk into a {token_id: tf-idf weight} sparse vector
compatible with Qdrant's SparseVector format.

The fitted index is saved to ingest/bm25_weights.pkl so the server
can load it at startup without re-reading the whole KB.
"""

from __future__ import annotations

import math
import pickle
import re
from pathlib import Path

_PICKLE_PATH = Path(__file__).parent / "bm25_weights.pkl"


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


class BM25Index:
    """Minimal BM25 index (k1=1.5, b=0.75) producing sparse vectors."""

    k1 = 1.5
    b = 0.75

    def __init__(self, corpus: list[str]) -> None:
        self.vocab: dict[str, int] = {}
        self.idf: dict[int, float] = {}
        self.avgdl: float = 0.0

        tokenized = [_tokenize(doc) for doc in corpus]
        N = len(tokenized)
        self.avgdl = sum(len(t) for t in tokenized) / max(N, 1)

        # Build vocab + document frequencies
        df: dict[str, int] = {}
        for tokens in tokenized:
            for tok in set(tokens):
                df[tok] = df.get(tok, 0) + 1

        for tok, freq in df.items():
            tid = len(self.vocab)
            self.vocab[tok] = tid
            self.idf[tid] = math.log((N - freq + 0.5) / (freq + 0.5) + 1)

    def encode(self, text: str, doc_len: int | None = None) -> dict[int, float]:
        """Return {token_id: weight} sparse vector for a text string."""
        tokens = _tokenize(text)
        dl = doc_len if doc_len is not None else len(tokens)
        tf_map: dict[str, int] = {}
        for tok in tokens:
            tf_map[tok] = tf_map.get(tok, 0) + 1

        sparse: dict[int, float] = {}
        for tok, tf in tf_map.items():
            tid = self.vocab.get(tok)
            if tid is None:
                continue
            idf = self.idf[tid]
            score = (
                idf
                * (tf * (self.k1 + 1))
                / (tf + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
            )
            if score > 0:
                sparse[tid] = round(score, 6)
        return sparse

    def save(self, path: Path = _PICKLE_PATH) -> None:
        with open(path, "wb") as f:
            pickle.dump(self, f)

    @classmethod
    def load(cls, path: Path = _PICKLE_PATH) -> BM25Index:
        with open(path, "rb") as f:
            return pickle.load(f)  # noqa: S301
