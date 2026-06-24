# tests/test_graph_src_chunks.py
from app.core.config import Settings


def test_graph_ppr_default_on():
    assert Settings(_env_file=None).graph_ppr_enabled is True
