"""rebuild_unified_kg 把 settings.kg_cluster_ann_threads 透传给 cluster_seeds。"""
import pytest
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services import kg_merge
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _concept(local_id, name, source_title):
    return {
        "local_id": local_id, "object_type": "concept",
        "payload": {"name": name, "section_path": "1"},
        "evidence": [{
            "source_id": "s", "source_title": source_title, "element_id": "e",
            "element_type": "p", "location_label": "1", "quoted_span": f"span-{name}",
            "confidence": 1.0,
        }],
    }


def test_rebuild_forwards_ann_threads(repo, monkeypatch):
    repo.settings.kg_cluster_ann_threads = 7  # sentinel value

    seen = []
    real = kg_merge.cluster_seeds

    def spy(*a, **k):
        seen.append(k.get("ann_threads"))
        return real(*a, **k)

    monkeypatch.setattr(kg_merge, "cluster_seeds", spy)

    nb = repo.create_notebook(NotebookCreate(name="nb"))
    # 同一归一化概念名,两个来源 → 跨文档合并 → 触发 cluster_seeds。
    repo.store_kg(nb.id, "s1", [_concept("l1", "Bandgap Reference", "A")], [])
    repo.store_kg(nb.id, "s2", [_concept("l2", "Bandgap Reference", "B")], [])
    repo.rebuild_unified_kg(nb.id)

    assert seen, "cluster_seeds 未被调用——检查 rebuild 是否短路"
    assert all(v == 7 for v in seen), f"期望全部透传 7,实得 {seen}"
