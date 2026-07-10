from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_DOCS = (
    "README.md",
    "README_zh.md",
    "AGENTS.md",
    "architecture.md",
    "fangan_done.md",
    "docs/superpowers/specs/2026-07-10-architecture-remediation-design.md",
    "docs/superpowers/plans/2026-07-10-architecture-contract-alignment.md",
)
LIVE_REFERENCE_DOCS = ("README.md", "README_zh.md", "AGENTS.md", "architecture.md")


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _assert_phrases(expected: dict[str, str]) -> None:
    for name, phrase in expected.items():
        assert phrase in _read(name), f"{name} is missing contract phrase: {phrase}"


def test_ask_disconnect_documentation_matches_detached_worker_contract():
    _assert_phrases(
        {
            "README.md": "A transport disconnect stops delivery to that client only",
            "README_zh.md": "transport 断连只停止向当前客户端继续推送",
            "AGENTS.md": "A transport disconnect only stops delivery to that client",
            "architecture.md": "transport 断连只停止向该客户端继续推送",
            "fangan_done.md": "transport 断连只停止向该客户端推送",
            "docs/superpowers/specs/2026-07-10-architecture-remediation-design.md":
                "Ask transport 断连只停止向该客户端继续推送",
            "docs/superpowers/plans/2026-07-10-architecture-contract-alignment.md":
                "Ask 断连保持 detached execution",
        }
    )
    for name in CONTRACT_DOCS:
        text = _read(name)
        assert "frontend abort/client disconnect" not in text
        assert "Client disconnect / abort must propagate" not in text


def test_retrieval_documentation_scopes_federation_and_tier_tie_break_by_path():
    _assert_phrases(
        {
            "README.md": "Baseline `chunk` retrieval reads chunks from the active notebook only",
            "README_zh.md": "`chunk` 基线只从当前 active notebook 读取 chunk",
            "AGENTS.md": "Baseline `chunk` retrieval is active-notebook-only",
            "architecture.md": "`chunk` 基线只读取 active notebook 的 chunk",
            "fangan_done.md": "`chunk` 基线只读 active notebook 的 chunk",
            "docs/superpowers/specs/2026-07-10-architecture-remediation-design.md":
                "`chunk` 基线只读取 active notebook 的 chunk",
            "docs/superpowers/plans/2026-07-10-architecture-contract-alignment.md":
                "`chunk` 基线只读取 active notebook 的 chunk",
        }
    )
    _assert_phrases(
        {
            "README.md": "The exact-score `base` tie-break applies only to knowledge-object hits",
            "README_zh.md": "exact-score 的 `base` 次序只适用于知识对象命中",
            "AGENTS.md": "The exact-score `base` tie-break applies only to knowledge-object hits",
            "architecture.md": "exact-score 的 `base` 次序只适用于知识对象命中",
            "fangan_done.md": "exact-score 的 `base` 次序只适用于知识对象命中",
            "docs/superpowers/specs/2026-07-10-architecture-remediation-design.md":
                "exact-score 的 `base` 次序只适用于知识对象命中",
            "docs/superpowers/plans/2026-07-10-architecture-contract-alignment.md":
                "exact-score 的 `base` 次序只适用于知识对象命中",
        }
    )
    _assert_phrases(
        {
            "README.md": "`federated_retrieve_relations()` remains score-only",
            "README_zh.md": "`federated_retrieve_relations()` 的关系命中仍只按 score 排序",
            "AGENTS.md": "`federated_retrieve_relations()` sorts relation hits by score only",
            "architecture.md": "`federated_retrieve_relations()` 的关系命中只按 score 降序",
            "fangan_done.md": "`federated_retrieve_relations()` 的关系命中仍只按 score 排序",
            "docs/superpowers/specs/2026-07-10-architecture-remediation-design.md":
                "`federated_retrieve_relations()` 的关系命中仍只按 score 排序",
            "docs/superpowers/plans/2026-07-10-architecture-contract-alignment.md":
                "`federated_retrieve_relations()` 的关系命中仍只按 score 排序",
        }
    )
    for name in CONTRACT_DOCS:
        text = _read(name)
        assert "base `1.20`" not in text
        assert "base 1.20" not in text
        assert "Every mode federates retrieval" not in text
        assert "所有模式都跨 `tier=base`" not in text
    for name in CONTRACT_DOCS[1:2] + CONTRACT_DOCS[3:]:
        assert "remains score-only" not in _read(name)


def test_workspace_documentation_names_three_tabs_and_actual_toolbar_actions():
    _assert_phrases(
        {
            "README.md": "three tabs — **Ask**, **Knowledge**, and **Deep Report**",
            "README_zh.md": "三个 tab——**问答**、**知识库**、**深度报告**",
            "AGENTS.md": "three tabs: **Ask**, **Knowledge**, and **Deep Report**",
            "architecture.md": "问答 / 知识库 / 深度报告三个 tab",
            "fangan_done.md": "问答 / 知识库 / 深度报告三个 tab",
            "docs/superpowers/specs/2026-07-10-architecture-remediation-design.md":
                "问答 / 知识库 / 深度报告三个 tab",
            "docs/superpowers/plans/2026-07-10-architecture-contract-alignment.md":
                "问答 / 知识库 / 深度报告三个 tab",
        }
    )
    _assert_phrases(
        {
            "README.md": "The Analysis menu itself contains only the promotion queue",
            "README_zh.md": "「分析」菜单本身只包含晋升队列",
            "AGENTS.md": "The Analysis menu itself contains only the promotion queue",
            "architecture.md": "「分析」菜单本身只含晋升队列",
            "fangan_done.md": "「分析」菜单当前只含晋升队列",
            "docs/superpowers/specs/2026-07-10-architecture-remediation-design.md":
                "「分析」菜单只含晋升队列",
            "docs/superpowers/plans/2026-07-10-architecture-contract-alignment.md":
                "「分析」菜单只含晋升队列",
        }
    )
    for name in CONTRACT_DOCS:
        text = _read(name)
        assert "two tabs" not in text
        assert "两个 tab" not in text
        assert "Ask/Knowledge 主区域" not in text
    for name in LIVE_REFERENCE_DOCS:
        text = _read(name)
        assert "Studio-style article research" not in text
        assert "Studio 类文章研究" not in text
        assert "Mind Map" not in text
        assert "Infographic" not in text
        assert "派生规则审核" not in text


def test_source_cleanup_documentation_matches_reparse_and_delete_boundaries():
    _assert_phrases(
        {
            "README.md": "Reparse preserves the source row and original file",
            "README_zh.md": "重新解析保留 source 行与原始文件",
            "AGENTS.md": "Reparse preserves the source row and original file",
            "architecture.md": "重新解析保留 source 行与原始文件",
            "fangan_done.md": "重新解析保留 source 行与原始文件",
            "docs/superpowers/specs/2026-07-10-architecture-remediation-design.md":
                "重新解析保留 source 行与原始文件",
            "docs/superpowers/plans/2026-07-10-architecture-contract-alignment.md":
                "重新解析保留 source 行与原始文件",
        }
    )
    _assert_phrases(
        {
            "README.md": "deletes the source row",
            "README_zh.md": "删除 source 行",
            "AGENTS.md": "deletes the source row",
            "architecture.md": "删除 source 行",
            "fangan_done.md": "删除 source 行",
            "docs/superpowers/specs/2026-07-10-architecture-remediation-design.md":
                "删除 source 行",
            "docs/superpowers/plans/2026-07-10-architecture-contract-alignment.md":
                "删除 source 行",
        }
    )
    for name in CONTRACT_DOCS:
        text = _read(name)
        assert "article research artifacts" not in text
        assert "文章研究产物" not in text
    for name in CONTRACT_DOCS[1:2] + CONTRACT_DOCS[3:]:
        assert "deletes the source row" not in _read(name)


def test_current_docs_describe_reports_and_sharing_without_retired_article_contracts():
    for name in LIVE_REFERENCE_DOCS:
        text = _read(name)
        for obsolete in (
            "/articles",
            "/derived-rules",
            "article_claims",
            "derived_rule_candidates",
            "Article Studio",
            "article research",
        ):
            assert obsolete not in text, f"{name} still presents {obsolete!r} as current"

    readme = _read("README.md")
    readme_zh = _read("README_zh.md")
    agents = _read("AGENTS.md")
    fangan_done = _read("fangan_done.md")
    assert "`reports` table and `/reports` APIs" in readme
    assert "`reports` 表与 `/reports` API" in readme_zh
    assert "`reports` table and `/reports` APIs" in agents
    assert "small notebooks can be copied; large notebooks can be joined read-only" in agents
    assert "There is no live collaborative editing or change-password flow" in agents
    assert "Single-user mode for now" not in agents
    assert "no change-password / sharing / collaboration" not in agents
    assert "更新日期：2026-07-10" in fangan_done
    assert "历史记录：Article Studio（已退役）" in fangan_done
    assert "历史记录（已退役）：Derived Rule Candidate" in fangan_done


def test_architecture_document_keeps_other_current_runtime_boundaries():
    readme = _read("README.md")
    readme_zh = _read("README_zh.md")
    architecture = _read("architecture.md")
    assert "LLM, embeddings, and rerank stay URL-based; MinerU separately supports" in readme
    assert "LLM、嵌入和 rerank 仍只通过 URL 服务访问；MinerU 则独立支持" in readme_zh
    assert "`ask_jobs` 行持久化" in architecture
    assert "cancellation event 注册在进程内" in architecture
    assert "服务重启后仍为 `running` 的 job 会转为 `interrupted`" in architecture
    assert "`status`、`trace`、`answer_id`" in architecture
    assert "不直接返回 `AskResponse`" in architecture
