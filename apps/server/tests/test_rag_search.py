from types import SimpleNamespace


class _QueryOnlyClient:
    def __init__(self) -> None:
        self.calls = []

    def query_points(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            points=[
                SimpleNamespace(
                    payload={
                        "doc_id": "faq",
                        "title": "Clinic FAQ",
                        "text": "Sunrise Dental Clinic is open Monday through Friday.",
                    },
                    score=0.91,
                )
            ]
        )


def test_dense_search_uses_query_points_for_new_qdrant_client(monkeypatch):
    from bot.tools import rag_search

    client = _QueryOnlyClient()
    monkeypatch.setattr(rag_search, "_embed_query", lambda query: [0.1, 0.2, 0.3])
    monkeypatch.setattr(rag_search, "_get_qdrant", lambda: client)

    hits = rag_search._dense_search("opening hours", "clinic_kb_v1", 5)

    assert len(hits) == 1
    assert hits[0].doc_id == "faq"
    assert client.calls[0]["collection_name"] == "clinic_kb_v1"
    assert client.calls[0]["query"] == [0.1, 0.2, 0.3]
    assert client.calls[0]["using"] == "dense"
    assert client.calls[0]["limit"] == 5
    assert client.calls[0]["with_payload"] is True


def test_sparse_search_uses_query_points_for_new_qdrant_client(monkeypatch):
    from bot.tools import rag_search

    client = _QueryOnlyClient()
    bm25 = SimpleNamespace(encode=lambda query: {7: 1.25, 11: 0.5})
    monkeypatch.setattr(rag_search, "_get_bm25", lambda: bm25)
    monkeypatch.setattr(rag_search, "_get_qdrant", lambda: client)

    hits = rag_search._sparse_search("insurance", "clinic_kb_v1", 3)

    assert len(hits) == 1
    sparse_vector = client.calls[0]["query"]
    assert sparse_vector.indices == [7, 11]
    assert sparse_vector.values == [1.25, 0.5]
    assert client.calls[0]["using"] == "sparse"
    assert client.calls[0]["limit"] == 3
