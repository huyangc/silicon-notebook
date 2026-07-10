from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_ask_disconnect_documentation_matches_detached_worker_contract():
    agents = _read("AGENTS.md")
    readme = _read("README.md")
    readme_zh = _read("README_zh.md")
    architecture = _read("architecture.md")

    assert "A transport disconnect only stops delivery to that client" in agents
    assert "continues in the background" in readme
    assert "断开连接只会停止当前客户端继续接收" in readme_zh
    assert "transport 断连只停止向该客户端继续推送" in architecture
    assert "frontend abort/client disconnect" not in agents
    assert "Client disconnect / abort must propagate" not in agents


def test_tier_documentation_matches_exact_score_tie_contract():
    agents = _read("AGENTS.md")
    readme = _read("README.md")
    readme_zh = _read("README_zh.md")
    architecture = _read("architecture.md")

    assert "only on an exact score tie" in agents
    assert "only when their relevance scores are exactly equal" in readme
    assert "仅在相关度分数完全相同时" in readme_zh
    assert "只在 score 完全相同时让 base 先排" in architecture
    for text in (agents, readme, readme_zh, architecture):
        assert "base `1.20`" not in text
        assert "base 1.20" not in text


def test_architecture_document_describes_current_workspace_boundary():
    architecture = _read("architecture.md")
    assert "来源栏 + Ask/Knowledge 主区域的两列 workspace" in architecture
    assert "前端（单文件）" not in architecture
    assert "工作区三栏" not in architecture
