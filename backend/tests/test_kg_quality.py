from app.services.retrieval import _payload_text


def test_payload_text_excludes_section_path():
    # name 干净,section_path 是纯定位元数据,不该进检索文本
    t = _payload_text({"name": "Mixtral", "section_path": "3 > 3.1"})
    assert t == "Mixtral"
    assert ">" not in t


def test_payload_text_keeps_other_fields():
    t = _payload_text({"name": "KV cache", "steps": ["a", "b"]})
    assert "KV cache" in t and "a" in t and "b" in t
