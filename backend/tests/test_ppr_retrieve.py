from app.core.config import Settings


def test_ppr_settings_defaults_off():
    s = Settings(_env_file=None)
    assert s.graph_ppr_enabled is False
    assert s.ppr_damping == 0.5
    assert s.ppr_passage_node_weight == 0.05
    assert s.ppr_top_chunks == 20
