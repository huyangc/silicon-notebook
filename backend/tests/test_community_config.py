from app.core.config import Settings


def test_community_defaults():
    s = Settings(_env_file=None)
    assert s.community_layer_enabled is True
    assert s.community_min_size == 3
    assert s.community_peers_topk == 8
    assert s.community_rerank_candidates == 200


def test_community_env_alias(monkeypatch):
    monkeypatch.setenv("COMMUNITY_PEERS_TOPK", "5")
    assert Settings(_env_file=None).community_peers_topk == 5
