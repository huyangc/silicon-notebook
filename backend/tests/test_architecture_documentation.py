from pathlib import Path

from app.repositories.ports import (
    AskCandidatePort,
    AskGraphPort,
    AskStreamPort,
    RetrievalPort,
)
from app.repositories.ownership_manifest import OWNER_BY_MEMBER
from tests import test_repository_facade_contract as facade_contract
from tests.test_repository_callers_static import (
    EXPECTED_REMEDIATION_SITES as CALLER_REMEDIATION_SITES,
    INDEPENDENT_PRIVATE_SITES,
    INDEPENDENT_SQL_SITES,
    private_repository_sites,
    product_sql_sites,
)
from tests.test_repository_protocol_coverage import protocol_calls


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
COMPOSITION_HISTORY_DOCS = (
    "docs/superpowers/plans/2026-07-10-repository-composition-refactor.md",
    "docs/superpowers/specs/2026-07-10-repository-composition-refactor-design.md",
)


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
    assert "更新日期：2026-07-12" in fangan_done
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


def test_repository_documentation_matches_composed_runtime_and_v9_compatibility():
    """Task 28: docs describe the composed repository (runtime + stores +
    consumer ports), the one-way dependency direction, the PostgreSQL
    extension boundary, v9 compatibility and the backup-only verifier —
    and stop presenting the retired mixin-inheritance stage as current."""
    _assert_phrases(
        {
            "README.md":
                "`SQLiteRepository` is the compatibility facade over a composed `RepositoryRuntime`",
            "README_zh.md": "`SQLiteRepository` 是组合式 `RepositoryRuntime` 之上的兼容 facade",
            "AGENTS.md": "`SQLiteRepository` is the compatibility facade over `RepositoryRuntime`",
            "architecture.md": "不再通过 mixin 继承复用实现",
            "fangan_done.md": "组合式 `RepositoryRuntime` 之上的兼容 facade",
        }
    )
    _assert_phrases(
        {
            "README.md": "a future PostgreSQL adapter replaces the store layer behind the same ports",
            "README_zh.md": "未来 PostgreSQL adapter 只需在同一 ports 后替换 store 层",
            "AGENTS.md": "a future PostgreSQL repository swaps the store layer behind the same ports",
            "architecture.md": "facade → runtime → application services → stores → `SqliteDatabase`",
        }
    )
    for name in LIVE_REFERENCE_DOCS + ("fangan_done.md",):
        text = _read(name)
        assert "verify_repository_snapshot.py" in text, (
            f"{name} must document the backup-only real-database verifier"
        )
        assert (
            "repository_v9" in text
            or "v9 fixture" in text
            or "v9 compatibility fixture" in text
            or "v9 兼容 fixture" in text
        ), (
            f"{name} must document the frozen schema-v9 compatibility guard"
        )
        # Retired descriptions of the mixin-inheritance stage must not read
        # as current architecture anywhere in the live reference docs.
        assert "The facade inherits both implementations" not in text
        assert "facade 通过继承复用两者" not in text
        assert "cohesive SQLite domains should be extracted incrementally" not in text
        assert "identity/sharing mixin 是迁移接缝" not in text
        assert "仍混合 persistence 与业务编排" not in text
        assert "已拆为 mixin 接缝" not in text


def test_completed_repository_boundary_claims_are_source_guarded():
    """The completion prose is coupled to production-source architecture guards.

    These helpers are shared with the architecture suites instead of copying or
    weakening their exact exception/debt ledgers here.
    """
    assert set(product_sql_sites()) - set(INDEPENDENT_SQL_SITES) == set()
    assert set(private_repository_sites()) - set(INDEPENDENT_PRIVATE_SITES) == set()
    assert CALLER_REMEDIATION_SITES == {
        "product_sql": set(),
        "private_repository": set(),
    }
    assert facade_contract.facade_body_violations(
        facade_contract.SQLiteRepository
    ) == []
    assert facade_contract.manifest_delegate_mismatches(
        facade_contract.SQLiteRepository, OWNER_BY_MEMBER
    ) == []

    assert protocol_calls("RetrievalPort") - set(RetrievalPort.__dict__) == set()
    for name, protocol in (
        ("AskCandidatePort", AskCandidatePort),
        ("AskGraphPort", AskGraphPort),
        ("AskStreamPort", AskStreamPort),
    ):
        declared = {
            member
            for member, value in protocol.__dict__.items()
            if callable(value) and not member.startswith("_")
        }
        assert protocol_calls(name) == declared

    _assert_phrases(
        {
            "README.md": "Application services do not assemble product SQL",
            "README_zh.md": "application service 不拼装主业务库 SQL",
            "AGENTS.md": "Application services do not assemble product SQL",
            "architecture.md": "application service 不拼装主业务库 SQL",
            "fangan_done.md": "application service 不再拼装主业务库 SQL",
        }
    )
    _assert_phrases(
        {
            "README.md": "one-hop delegates",
            "README_zh.md": "单跳委托",
            "AGENTS.md": "one-hop delegates",
            "architecture.md": "单跳委托",
            "fangan_done.md": "单跳委托",
        }
    )


def test_repository_runtime_and_verifier_completion_claims_are_synchronized():
    _assert_phrases(
        {
            "README.md": "Synchronous Ask/report submission failures",
            "README_zh.md": "Ask/report 同步提交失败",
            "AGENTS.md": "Synchronous Ask/report submission failures",
            "architecture.md": "Ask/report 同步提交失败",
            "fangan_done.md": "Ask/report 同步提交失败",
        }
    )
    _assert_phrases(
        {
            "README.md": "only SHM mtime is exempt",
            "README_zh.md": "只豁免 SHM mtime",
            "AGENTS.md": "only SHM mtime is exempt",
            "architecture.md": "只豁免 SHM mtime",
            "fangan_done.md": "只豁免 SHM mtime",
        }
    )


def test_repository_schema_baseline_wording_is_exact_and_not_stale():
    english = (
        "The refactor does not change the schema version present on its master "
        "baseline\n(SCHEMA_VERSION = 10). The committed v9 compatibility fixture "
        "upgrades through\nthe existing v10 migration and remains readable."
    )
    chinese = (
        "本次重构不改变其 master 基线已有的 schema 版本（`SCHEMA_VERSION = 10`）。"
        "已提交的 v9 兼容 fixture 会经由既有 v10 migration 升级，并保持可读。"
    )
    for name in ("README.md", "AGENTS.md"):
        assert english in _read(name), f"{name} is missing the exact schema statement"
    for name in ("README_zh.md", "architecture.md", "fangan_done.md") + COMPOSITION_HISTORY_DOCS:
        assert chinese in _read(name), f"{name} is missing the schema statement"

    for name in COMPOSITION_HISTORY_DOCS:
        text = _read(name)
        for stale in (
            "SCHEMA_VERSION=9",
            "SCHEMA_VERSION = 9",
            "SCHEMA_VERSION 保持 9",
            "SCHEMA_VERSION remains 9",
            "schema v9 and frozen-master",
        ):
            assert stale not in text, f"{name} retains stale schema wording: {stale}"


def test_ask_mode_documentation_keeps_chunk_default_and_alias_only_retirement():
    """`chunk` (default) / `reasoning` / `graph` are the modes; persisted
    `fast`/`global` ids survive only as aliases to `chunk`.  The older
    fast/global product description must not resurface."""
    _assert_phrases(
        {
            "README.md": "Retired ids `fast` and `global` are transparently remapped to `chunk`",
            "README_zh.md": "退役 id `fast`、`global` 透明映射到 `chunk`",
            "AGENTS.md": "retired `fast`/`global` ids map to `chunk` only for persisted-session compatibility",
            "architecture.md": "退役 mode id 只保留兼容映射",
            "fangan_done.md": "KG-native Ask（chunk / graph / reasoning",
        }
    )
    for name in LIVE_REFERENCE_DOCS + ("fangan_done.md",):
        text = _read(name)
        assert "Global QA" not in text
        assert 'mode="global"' not in text
        assert 'mode="fast"' not in text


def test_superseded_spec_scope_is_repository_only_with_pydantic_lifespan_deferred():
    remediation = _read(
        "docs/superpowers/specs/2026-07-10-architecture-remediation-design.md"
    )
    assert "取代范围仅限 Repository 工作" in remediation
    assert "Pydantic 模型分文件" in remediation
    assert "仍延后为独立工作" in remediation
    composition = _read(
        "docs/superpowers/specs/2026-07-10-repository-composition-refactor-design.md"
    )
    assert "`SCHEMA_VERSION` 现为 10" in composition
    assert "不是本重构新增的迁移" in composition
    for name in ("architecture.md", "fangan_done.md"):
        text = _read(name)
        assert "延后为独立工作" in text, (
            f"{name} must keep the Pydantic/lifespan deferral factual"
        )
