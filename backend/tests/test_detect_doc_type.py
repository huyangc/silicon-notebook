"""上传前类型检测：detect_doc_type_from_sample 纯函数 + POST /api/detect-doc-types 批量端点。"""
from fastapi.testclient import TestClient

from app.services.extraction_profiles import detect_doc_type_from_sample

# 学术论文线索（abstract/we propose/experimental results/references/et al/doi/related work）
ACADEMIC = (
    "Abstract. We propose a new method. Experimental results show gains. "
    "References: Smith et al. 2020, doi:10.1/x. Related work covers prior art."
)
# 教材线索（chapter/theorem/proof/definition/exercise）
TEXTBOOK = (
    "Chapter 3 Introduction. Theorem 3.1 states the bound. Proof follows. "
    "Definition: a filter is a network. Exercise 3.2: compute the gain."
)


def test_detect_academic_sample():
    assert detect_doc_type_from_sample(ACADEMIC) == "academic_paper"


def test_detect_textbook_sample():
    assert detect_doc_type_from_sample(TEXTBOOK) == "textbook"


def test_detect_empty_returns_none():
    assert detect_doc_type_from_sample("") is None
    assert detect_doc_type_from_sample("   ") is None


def test_detect_ambiguous_returns_none():
    # 通用文字无线索 → None
    assert detect_doc_type_from_sample("Some generic text about circuits and signals.") is None
    # 两类各命中一次，未达 MIN_HITS=2 / 无领先 → None（不强行归类）
    assert detect_doc_type_from_sample("This abstract discusses one chapter.") is None


def _client():
    from app.core.config import get_settings
    get_settings.cache_clear()
    from app.main import create_app
    return TestClient(create_app())


def test_detect_endpoint_batch():
    resp = _client().post("/api/detect-doc-types", json={"items": [
        {"name": "paper.md", "sample": ACADEMIC},
        {"name": "book.md", "sample": TEXTBOOK},
        {"name": "vague.md", "sample": "generic text about signals"},
    ]})
    assert resp.status_code == 200
    out = {r["name"]: r["doc_type_id"] for r in resp.json()}
    assert out["paper.md"] == "academic_paper"
    assert out["book.md"] == "textbook"
    assert out["vague.md"] == ""  # 未检出 → 空串（前端落到"自动检测"）


def test_detect_endpoint_empty_items():
    resp = _client().post("/api/detect-doc-types", json={"items": []})
    assert resp.status_code == 200
    assert resp.json() == []
