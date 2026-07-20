import inspect
import re
from pathlib import Path

from app.repositories.ports import (
    AskCandidatePort,
    AskGraphPort,
    AskStreamPort,
    RetrievalPort,
)
from app.repositories.ownership_manifest import OWNER_BY_MEMBER
from app.services import report_engine, report_execution, repository_runtime
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
REMEDIATION_DOCS = (
    "docs/superpowers/specs/2026-07-11-repository-review-remediation-design.md",
    "docs/superpowers/plans/2026-07-11-repository-review-remediation.md",
)


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _between(name: str, start: str, end: str | None = None) -> str:
    text = _read(name)
    section = text.split(start, 1)[1]
    return section.split(end, 1)[0] if end else section


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


def test_workspace_documentation_names_four_tabs_and_actual_toolbar_actions():
    _assert_phrases(
        {
            "README.md": "four tabs — **问答** (Ask), **知识库** (Knowledge), **记忆** (Memory), and **深度报告** (Deep Report)",
            "README_zh.md": "四个 tab——**问答**（Ask）、**知识库**（Knowledge）、**记忆**（Memory）、**深度报告**（Deep Report）",
            "AGENTS.md": "four tabs: **问答** (Ask), **知识库** (Knowledge), **记忆** (Memory), and **深度报告** (Deep Report)",
            "architecture.md": "问答 (Ask) / 知识库 (Knowledge) / 记忆 (Memory) / 深度报告 (Deep Report) 四个 tab",
            "fangan_done.md": "问答 (Ask) / 知识库 (Knowledge) / 记忆 (Memory) / 深度报告 (Deep Report) 四个 tab",
            "silicon_notebook_fangan.md": "问答 (Ask) | 知识库 (Knowledge) | 记忆 (Memory) | 深度报告 (Deep Report)",
        }
    )
    _assert_phrases(
        {
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


def test_live_workspace_docs_have_no_memory_omitting_tab_contracts():
    """Current docs must not retain a pre-Memory tab list.

    Dated 2026-07-10 history is intentionally preserved; the matching historical
    plan/spec phrases remain guarded by the preceding test.
    """
    live_docs = (
        "README.md",
        "README_zh.md",
        "AGENTS.md",
        "architecture.md",
        "fangan_done.md",
        "silicon_notebook_fangan.md",
    )
    for name in live_docs:
        current_lines = [
            line
            for line in _read(name).splitlines()
            if "2026-07-10" not in line
        ]
        current = "\n".join(current_lines)
        assert re.search(r"\bthree[- ]tabs?\b", current, re.I) is None, (
            f"{name} retains a current three-tab workspace phrase"
        )
        assert "三个 tab" not in current

        for match in re.finditer(
            r"Ask.{0,80}Knowledge.{0,80}Deep Report", current, re.I
        ):
            assert "Memory" in match.group(0), (
                f"{name} has a current English tab list without Memory: {match.group(0)}"
            )
        for match in re.finditer(
            r"问答.{0,80}知识库.{0,80}深度报告", current
        ):
            assert "Memory" in match.group(0) or "记忆" in match.group(0), (
                f"{name} has a current Chinese tab list without Memory: {match.group(0)}"
            )


def test_current_memory_docs_describe_sanitized_multi_object_promotion_contract():
    sections = {
        "README.md": _between("README.md", "## Memory and Agent MCP", "## KG extraction trigger"),
        "README_zh.md": _between("README_zh.md", "## Memory 与 Agent MCP", "## KG 抽取触发"),
        "AGENTS.md": _read("AGENTS.md"),
        "architecture.md": _between("architecture.md", "### 3.4 Memory 与 Agent MCP", "### 3.5 KG 与索引维护"),
        "silicon_notebook_fangan.md": _between("silicon_notebook_fangan.md", "# 19. Agent Memory 系统"),
        "fangan_done.md": _between("fangan_done.md", "## 27. Agent Memory 与 MCP", "## 20. 当前边界"),
    }
    expected = {
        "README.md": (
            "sanitized extraction candidates and server-validated evidence",
            "revalidates the Memory's current confirmed status and creator access",
            "one or more Base KG objects",
            "`base_object_ids`",
        ),
        "README_zh.md": (
            "脱敏后的结构化提取候选与服务端验证过的 evidence",
            "重新校验 Memory 当前仍为 confirmed 且创建者仍有访问权",
            "一个或多个 Base KG 对象",
            "`base_object_ids`",
        ),
        "AGENTS.md": (
            "sanitized extraction candidates and server-validated evidence",
            "revalidates current confirmed status and creator access",
            "one or more Base KG objects",
            "`base_object_ids`",
        ),
        "architecture.md": (
            "脱敏后的结构化提取候选与服务端验证过的 evidence",
            "重新校验 Memory 当前仍为 confirmed 且创建者仍有访问权",
            "一个或多个 Base KG 对象",
            "`base_object_ids`",
        ),
        "silicon_notebook_fangan.md": (
            "脱敏后的结构化提取候选与服务端验证过的 evidence",
            "重新校验 Memory 当前仍为 confirmed 且创建者仍有访问权",
            "一个或多个 Base KG 对象",
            "`base_object_ids`",
        ),
        "fangan_done.md": (
            "脱敏后的结构化提取候选与服务端验证过的 evidence",
            "重新校验 Memory 当前仍为 confirmed 且创建者仍有访问权",
            "一个或多个 Base KG 对象",
            "`base_object_ids`",
        ),
    }
    for name, phrases in expected.items():
        compact_section = "".join(sections[name].split())
        for phrase in phrases:
            assert "".join(phrase.split()) in compact_section, (
                f"{name} is missing Memory promotion phrase: {phrase}"
            )

    for name, section in sections.items():
        compact_section = "".join(section.split())
        for stale in (
            "three tabs",
            "三个 tab",
            "审核 Memory revision 与经过验证的 provenance",
            "reviews the Memory revision and provenance",
            "create or merge a Base KG object",
            "create or merge a base object",
            "创建或合并 base object",
        ):
            assert "".join(stale.split()) not in compact_section, (
                f"{name} retains stale Memory wording: {stale}"
            )


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
    assert "更新日期：2026-07-20" in fangan_done
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


def test_projection_ownership_claim_matches_sql_and_application_boundaries():
    docs = (
        LIVE_REFERENCE_DOCS
        + ("fangan_done.md",)
        + COMPOSITION_HISTORY_DOCS
        + REMEDIATION_DOCS
    )
    for name in docs:
        text = _read(name)
        for overclaim in (
            "row-to-domain projections",
            "row-to-domain projection",
            "SQL/row projection 只在 SQLite stores",
            "SQL 与 row-to-domain projection 全部归",
            "独占 SQL 与 row-to-domain projection",
            "Stores own SQL and row-to-domain projection",
        ):
            assert overclaim not in text, f"{name} overstates projection ownership"

    _assert_phrases(
        {
            "README.md": (
                "Stores own product SQL and raw row selection; established "
                "application/query components may assemble domain/application projections"
            ),
            "AGENTS.md": (
                "Stores own product SQL and raw row selection; established "
                "application/query components may assemble domain/application projections"
            ),
            "README_zh.md": (
                "store 独占 product SQL 与 raw row selection；既定 application/query "
                "component 可组装 domain/application projection"
            ),
            "architecture.md": (
                "store 独占 product SQL 与 raw row selection；既定 application/query "
                "component 可组装 domain/application projection"
            ),
            "fangan_done.md": (
                "store 独占 product SQL 与 raw row selection；既定 application/query "
                "component 可组装 domain/application projection"
            ),
            COMPOSITION_HISTORY_DOCS[0]: (
                "store 独占 product SQL 与 raw row selection；既定 application/query "
                "component 可组装 domain/application projection"
            ),
            COMPOSITION_HISTORY_DOCS[1]: (
                "store 独占 product SQL 与 raw row selection；既定 application/query "
                "component 可组装 domain/application projection"
            ),
            REMEDIATION_DOCS[0]: (
                "Stores own product SQL and raw row selection; established "
                "application/query components may assemble domain/application projections"
            ),
        }
    )


def test_report_cancellation_is_the_documented_process_global_runtime_exception():
    assert report_engine.REPORT_CANCELLATIONS is report_execution.REPORT_CANCELLATIONS
    assert repository_runtime.REPORT_CANCELLATIONS is report_execution.REPORT_CANCELLATIONS
    init_source = inspect.getsource(repository_runtime.RepositoryRuntime.__init__)
    wire_source = inspect.getsource(
        repository_runtime.RepositoryRuntime.wire_report_execution
    )
    assert "self.report_cancellations = REPORT_CANCELLATIONS" in init_source
    assert "cancellations=self.report_cancellations" in wire_source

    _assert_phrases(
        {
            "README.md": (
                "`RepositoryRuntime` owns or references composed runtime state; "
                "`REPORT_CANCELLATIONS` remains the intentionally process-global canonical owner"
            ),
            "AGENTS.md": (
                "`RepositoryRuntime` owns or references composed runtime state; "
                "`REPORT_CANCELLATIONS` remains the intentionally process-global canonical owner"
            ),
            "README_zh.md": (
                "`RepositoryRuntime` 持有或引用组合后的运行态；`REPORT_CANCELLATIONS` "
                "刻意保持 process-global canonical owner"
            ),
            "architecture.md": (
                "`RepositoryRuntime` 持有或引用组合后的运行态；`REPORT_CANCELLATIONS` "
                "刻意保持 process-global canonical owner"
            ),
            "fangan_done.md": (
                "`RepositoryRuntime` 持有或引用组合后的运行态；`REPORT_CANCELLATIONS` "
                "刻意保持 process-global canonical owner"
            ),
            COMPOSITION_HISTORY_DOCS[0]: (
                "`RepositoryRuntime` 持有或引用组合后的运行态；`REPORT_CANCELLATIONS` "
                "刻意保持 process-global canonical owner"
            ),
            COMPOSITION_HISTORY_DOCS[1]: (
                "`RepositoryRuntime` 持有或引用组合后的运行态；`REPORT_CANCELLATIONS` "
                "刻意保持 process-global canonical owner"
            ),
            REMEDIATION_DOCS[0]: (
                "`RepositoryRuntime` owns or references composed runtime state; "
                "`REPORT_CANCELLATIONS` remains the intentionally process-global canonical owner"
            ),
        }
    )


def test_repository_schema_baseline_wording_is_exact_and_not_stale():
    english_current = (
        "The current schema version is 20. The committed v9 compatibility fixture\n"
        "upgrades through v10, the v11/v12 SQLite hot-path indexes, v13 Memory/Agent,\n"
        "v14/v15 Memory-derived source links/indexes, v16 Knowhow tables/assets, v17\n"
        "paper metadata, v18 Knowhow cell-code/role migration, v19 source-linked assets,\n"
        "and v20 durable KG build jobs, and remains readable."
    )
    chinese_current = (
        "当前 schema 版本为 20。已提交的 v9 兼容 fixture 会依次经过 v10、"
        "v11/v12 SQLite 热路径索引、v13 Memory/Agent、v14/v15 Memory 派生源 "
        "link/index、v16 Knowhow 表/资产、v17 论文元数据、v18 Knowhow 格子代码/"
        "角色迁移、v19 来源关联资产与 v20 持久化 KG 构建任务，并保持可读。"
    )
    historical_chinese = (
        "本次重构不改变其 master 基线已有的 schema 版本（`SCHEMA_VERSION = 10`）。"
        "已提交的 v9 兼容 fixture 会经由既有 v10 migration 升级，并保持可读。"
    )
    for name in ("README.md", "AGENTS.md"):
        assert english_current in _read(name), f"{name} is missing the exact schema statement"
    assert chinese_current in _read("README_zh.md")
    for name in ("architecture.md", "fangan_done.md") + COMPOSITION_HISTORY_DOCS:
        assert historical_chinese in _read(name), f"{name} is missing the historical schema statement"

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


def test_knowhow_documentation_matches_projection_isolation_and_agent_scopes():
    """Knowhow-table contract phrases stay synchronized across the live docs.

    Pins the three load-bearing claims: the cell-node projection (zero-LLM,
    object_type = column name, row-title column optional → retrieval-only),
    the code-attachment isolation invariant (stored, never executed or
    indexed anywhere retrieval-facing), and the dual-auth agent scopes
    (`knowledge:read` reads / `knowhow:code` code writes). Whitespace-
    insensitive like the Memory promotion contract test above, so doc
    reflows don't break the guard.
    """
    expected = {
        "README.md": (
            "every non-empty cell becomes a knowledge-graph node whose *type is its column name*",
            "never generated or executed by the notebook, and never embedded/chunked/indexed into any KG projection",
            "Reading code still only needs `knowledge:read` — only writing it",
        ),
        "README_zh.md": (
            "节点的类型就是所在列名",
            "格子照常切成 chunk 供问答使用，但不建任何图谱节点",
            "绝不自动触发",
        ),
        "AGENTS.md": (
            "Cell code attachments are stored per cell but never executed, indexed, embedded, "
            "FTS'd, projected into the KG, or included in Ask context",
            "The only scopes are `knowledge:read`, `memory:read`, `memory:read_candidates`, "
            "`memory:propose`, `ask:execute`, and `knowhow:code`",
            "plus the four knowhow tools `list_knowhow_tables`, `get_knowhow_discrimination`, "
            "`get_knowhow_row`, and `put_knowhow_cell_code`",
        ),
        "architecture.md": (
            "唯一零 LLM 的 KG 写入方",
            "代码只存不执行，永不进 element/chunk/embedding/FTS/KG",
            "读取需 `knowledge:read`、代码写入需 `knowhow:code`",
        ),
        "fangan_done.md": (
            "及 knowhow 四工具 `list_knowhow_tables`、`get_knowhow_discrimination`、"
            "`get_knowhow_row`、`put_knowhow_cell_code`",
            "`ask:execute`、`knowhow:code`",
        ),
    }
    for name, phrases in expected.items():
        compact_text = "".join(_read(name).split())
        for phrase in phrases:
            assert "".join(phrase.split()) in compact_text, (
                f"{name} is missing knowhow contract phrase: {phrase}"
            )

    # The pre-knowhow scope/tool lists must not resurface as current contract.
    assert "精确七工具" not in _read("fangan_done.md")
    for name in ("AGENTS.md",):
        compact_text = "".join(_read(name).split())
        assert "".join("`memory:propose`, and `ask:execute`.".split()) not in compact_text, (
            f"{name} retains the retired five-scope list as current"
        )


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


def test_user_facing_vocabulary_guard_is_documented_in_both_readmes():
    """界面词汇守卫是**开发约束**变更(新增硬门 + AGENTS.md 词汇契约),按仓库
    Documentation Sync 规则必须同步两份 README:守卫存在、怎么单独跑、契约在哪。

    review 阻塞 5:PR 把 check_ui_vocabulary.py 挂进了 check.sh 并在 AGENTS.md 立了
    强制词汇契约,却只改了 AGENTS.md,两份 README 的开发/验证说明没跟。
    """
    _assert_phrases(
        {
            "README.md": "PYTHONPATH=backend python scripts/check_ui_vocabulary.py",
            "README_zh.md": "PYTHONPATH=backend python scripts/check_ui_vocabulary.py",
        }
    )
    _assert_phrases(
        {
            "README.md": "`AGENTS.md`「界面词汇表」is its single source of truth",
            "README_zh.md": "真源是 `AGENTS.md`「界面词汇表」",
        }
    )
    # 守卫的两条独立检查都要在文档里露出,否则「兜底即原值」会被当成风格建议。
    _assert_phrases(
        {
            "README.md": "rejects raw enum fallbacks (`MAP[x] ?? x`, and `label(map, x, x)`",
            "README_zh.md": "拒绝「兜底即原值」（`MAP[x] ?? x`，以及通过正规 API 达成同一效果的 `label(map, x, x)`）",
        }
    )
    # 第二轮 review 阻塞 3:兜底检查改用 TS AST 并搬去前端。文档要指到新位置,
    # 否则「同一脚本里的第二条检查」这句会把人带到早已删掉的正则上。
    _assert_phrases(
        {
            "README.md": "`frontend/app/raw-enum-fallback.test.mjs`",
            "README_zh.md": "`frontend/app/raw-enum-fallback.test.mjs`",
        }
    )
    _assert_phrases(
        {
            "README.md": "runs on a real TypeScript AST rather than a regex",
            "README_zh.md": "跑在真正的 TypeScript AST 上而非正则",
        }
    )
    # 自测文件是「黑名单不得退化成词表子集」的执行者,文档要指向它。
    _assert_phrases(
        {
            "README.md": "backend/tests/test_ui_vocabulary_guard.py",
            "README_zh.md": "backend/tests/test_ui_vocabulary_guard.py",
        }
    )
    # 第二轮 review 阻塞 2:守卫作用域从「frontend/app 目录」改成「信任边界」——
    # 后端 user_error() 的文案会被前端原样上屏,所以同样受词表约束。这是开发者
    # 写后端错误文案时必须知道的约束,三份文档都要说明白。
    _assert_phrases(
        {
            "AGENTS.md": "作用域跟着信任边界走，不跟着目录走",
            "README.md": "scope follows the **trust boundary rather than the directory tree**",
            "README_zh.md": "作用域跟着信任边界走、不跟着目录树走",
        }
    )
    _assert_phrases(
        {
            "AGENTS.md": '后端 `user_error(status, "…")` 的消息字面量',
            "README.md": 'the message literals of every backend `user_error(status, "…")` call',
            "README_zh.md": '后端每处 `user_error(status, "…")` 的消息字面量',
        }
    )
    # 反向边界同样要写明,否则下一个人会顺手把 str(exc) 也纳进来。
    _assert_phrases(
        {
            "AGENTS.md": "裸 `HTTPException(detail=str(exc))` 刻意不在扫描面内",
            "README.md": "Bare `HTTPException(detail=str(exc))` stays outside the scan on purpose",
            "README_zh.md": "裸 `HTTPException(detail=str(exc))` 刻意不在扫描面内",
        }
    )


def test_default_notebook_name_is_documented_as_a_contract_not_copy():
    """`Untitled notebook` 是落库值,不是界面文案。review 阻塞 1:散文重写把它改成了
    中文,同时打破 5 份文档与后端 3 处。文档侧把「持久化值 ≠ 文案」写明,免得下一轮
    措辞调整又把它当英文散文扫掉。"""
    _assert_phrases(
        {
            "README.md": "are contracts, not copy, so they are never touched by a wording pass",
            "README_zh.md": "属于契约不属于文案，任何一轮措辞调整都不得顺手改动它们",
        }
    )
    for name in ("README.md", "README_zh.md", "AGENTS.md", "architecture.md", "fangan_done.md"):
        assert "Untitled notebook" in _read(name), (
            f"{name} 不再钉着默认库名 Untitled notebook——文档与代码已失配"
        )
