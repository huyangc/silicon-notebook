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


def test_architecture_document_separates_mineru_transport_modes():
    readme = _read("README.md")
    readme_zh = _read("README_zh.md")
    architecture = _read("architecture.md")
    assert "LLM, embeddings, and rerank stay URL-based; MinerU separately supports" in readme
    assert "LLM、嵌入和 rerank 仍只通过 URL 服务访问；MinerU 则独立支持" in readme_zh
    assert "LLM、embedding 与 reranker 仍只通过 URL 服务访问" in architecture
    assert "`MINERU_MODE=http` 调用远端 `mineru-api`" in architecture
    assert "`MINERU_MODE=cli` 在隔离子进程" in architecture
    assert "`MINERU_MODE=off` 使用 pypdf" in architecture
    assert "every model (LLM, embeddings, rerank, MinerU)" not in readme
    assert "所有模型(LLM / 嵌入 / rerank /\nMinerU)" not in readme_zh
    assert "LLM、embedding、rerank 与 MinerU 均通过可配置 URL" not in architecture


def test_architecture_document_separates_reparse_from_source_delete():
    architecture = _read("architecture.md")
    assert "重新解析会清理旧的抽取与数据库派生状态，但保留原始文件" in architecture
    assert "删除 source 还会移除存储的本地文件和依赖该 source 的文章研究产物" in architecture


def test_architecture_document_separates_durable_job_state_from_process_local_cancel_event():
    architecture = _read("architecture.md")
    assert "`ask_jobs` 行持久化" in architecture
    assert "cancellation event 注册在进程内" in architecture
    assert "服务重启后仍为 `running` 的 job 会转为 `interrupted`" in architecture
    assert "持久化 Ask job 与 cancellation event" not in architecture


def test_ask_job_detail_documentation_describes_metadata_not_final_response():
    readme = _read("README.md")
    readme_zh = _read("README_zh.md")
    architecture = _read("architecture.md")

    assert "`status`, `trace`, and `answer_id`" in readme
    assert "reloads the conversation" in readme
    assert "`status`、`trace` 与 `answer_id`" in readme_zh
    assert "重新加载 conversation" in readme_zh
    assert "`status`、`trace`、`answer_id`" in architecture
    assert "不直接返回 `AskResponse`" in architecture
    assert "state/result" not in readme
    assert "状态与结果" not in readme_zh
    assert "状态/结果读取" not in architecture
