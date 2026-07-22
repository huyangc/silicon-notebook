from types import SimpleNamespace

from app.services.knowledge_query import KnowledgeQueryService
from tests.model_testkit import RecordingModelProvider


class _Embedder:
    configured = True

    def __init__(self):
        self.queries = []

    def embed_query(self, query):
        self.queries.append(query)
        return [1.0, 0.0]


class _Ann:
    def set_ef(self, value):
        self.ef = value

    def knn_query(self, vector, k):
        assert list(vector) == [1.0, 0.0]
        assert k == 1
        return [[0]], [[0.1]]


def test_semantic_search_uses_bound_retrieval_query_embedding():
    embedder = _Embedder()
    models = RecordingModelProvider(
        embedding_clients={"retrieval_query_embedding": embedder}
    )
    index = SimpleNamespace(
        ann_labels=["ko-1"], manifest={"dim": 2}
    )
    scale = SimpleNamespace(
        load=lambda notebook_id: index,
        open_ann=lambda loaded, kind: _Ann(),
    )
    service = KnowledgeQueryService(
        settings=SimpleNamespace(),
        model_provider=models,
        event_log=SimpleNamespace(emit=lambda event: None),
        database=None,
        catalog=None,
        knowledge=None,
        chunk_store=None,
        unified_kg=None,
        scale_runtime=scale,
        retrieval=lambda: (_ for _ in ()).throw(
            AssertionError("semantic search must not use a generic retrieval embedder")
        ),
        schemas=None,
        snapshots=None,
        notebook_languages=lambda: {},
    )

    hits = service.semantic_search("nb-1", "cascode", 5)

    assert hits == [{
        "object_id": "ko-1",
        "name": "",
        "score": 0.9,
        "match": "semantic",
    }]
    assert embedder.queries == ["cascode"]
    assert ("embedding", "retrieval_query_embedding") in models.calls


def test_semantic_search_skips_when_query_workload_unbound():
    models = RecordingModelProvider()
    scale = SimpleNamespace(
        load=lambda notebook_id: (_ for _ in ()).throw(
            AssertionError("unbound query workload must stop before index loading")
        )
    )
    service = KnowledgeQueryService(
        settings=SimpleNamespace(), model_provider=models,
        event_log=SimpleNamespace(emit=lambda event: None), database=None,
        catalog=None, knowledge=None, chunk_store=None, unified_kg=None,
        scale_runtime=scale, retrieval=lambda: None, schemas=None,
        snapshots=None, notebook_languages=lambda: {},
    )

    assert service.semantic_search("nb-1", "q", 5) == []
