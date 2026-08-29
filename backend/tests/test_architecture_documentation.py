import ast
import inspect
import re
from pathlib import Path

from app.repositories.sqlite.migrations import SCHEMA_VERSION
from app.services import report_engine, report_execution, repository_runtime


ROOT = Path(__file__).resolve().parents[2]
DOCUMENTATION_BUNDLES = {
    "README.md": (
        "README.md",
        "docs/product-and-api.md",
        "docs/deployment-and-configuration.md",
        "docs/operations.md",
        "docs/development.md",
    ),
    "README_zh.md": (
        "README_zh.md",
        "docs/product-and-api_zh.md",
        "docs/deployment-and-configuration_zh.md",
        "docs/operations_zh.md",
        "docs/development_zh.md",
    ),
}
CONTRACT_DOCS = (
    "README.md",
    "README_zh.md",
    "architecture.md",
    "fangan_done.md",
    "docs/superpowers/specs/2026-07-10-architecture-remediation-design.md",
    "docs/superpowers/plans/2026-07-10-architecture-contract-alignment.md",
)
LIVE_REFERENCE_DOCS = ("README.md", "README_zh.md", "architecture.md")
COMPOSITION_HISTORY_DOCS = (
    "docs/superpowers/plans/2026-07-10-repository-composition-refactor.md",
    "docs/superpowers/specs/2026-07-10-repository-composition-refactor-design.md",
)
REMEDIATION_DOCS = (
    "docs/superpowers/specs/2026-07-11-repository-review-remediation-design.md",
    "docs/superpowers/plans/2026-07-11-repository-review-remediation.md",
)


def _read(name: str) -> str:
    paths = DOCUMENTATION_BUNDLES.get(name, (name,))
    return "\n\n".join(
        (ROOT / path).read_text(encoding="utf-8") for path in paths
    )


def _read_file(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _between(name: str, start: str, end: str | None = None) -> str:
    text = _read(name)
    section = text.split(start, 1)[1]
    return section.split(end, 1)[0] if end else section


def _assert_phrases(expected: dict[str, str]) -> None:
    for name, phrase in expected.items():
        assert phrase in _read(name), f"{name} is missing contract phrase: {phrase}"


def _assert_ordered(section: str, phrases: tuple[str, ...]) -> None:
    positions = [section.index(phrase) for phrase in phrases]
    assert positions == sorted(positions), (
        f"contract phrases are out of order: {list(zip(phrases, positions))}"
    )


def test_postgres_integration_lane_is_separate_fail_closed_and_pg16_authoritative():
    offline = _read("scripts/check.sh")
    assert "check_postgres.sh" not in offline
    assert "TEST_POSTGRES_URL" not in offline
    assert "postgres_integration" not in offline

    postgres = _read("scripts/check_postgres.sh")
    launcher = _read("backend/tests/postgres/lane.py")
    catalog_helpers = _read("backend/tests/postgres/conftest.py")
    assert 'TEST_POSTGRES_URL:?TEST_POSTGRES_URL is required' in postgres
    assert "-m postgres_integration" in postgres
    assert "POSTGRES_CI_AUXILIARY_TARGETS_REQUIRED" in postgres
    assert "TEST_POSTGRES_NON_C_URL" in postgres
    assert "TEST_POSTGRES_NON_UTF_URL" in postgres
    assert "database_status" in postgres
    assert "redact_database_url" in postgres
    for phrase in (
        "to_jsonb(d) AS catalog",
        "PGPASSFILE",
        "os.chmod(path, 0o600)",
        "_CHILD_ENV_ALLOWLIST",
        "conninfo_to_dict",
        "_run_isolated_gate",
        "subprocess.Popen",
        "start_new_session",
        "_LauncherResources",
        "pending_signum",
        "register_pgpass",
        "register_child",
        "_launcher_signal_handlers",
        "_terminate_and_reap",
        "128 + interruption.signum",
        '"--preflight"',
        "target.sanitized_url",
        '"--tb=short"',
        '"--maxfail=1"',
    ):
        assert phrase in launcher
    assert "datlocale" in catalog_helpers
    assert "daticulocale" in catalog_helpers
    assert "os.environ.copy()" not in launcher
    assert launcher.index("generated_bytes + existing_bytes") > launcher.index(
        "generated_bytes ="
    )
    assert '_SAFE_CONNECTION_QUERY_KEYS = {"sslmode"}' in catalog_helpers

    workflow = _read(".github/workflows/ci.yml")
    for phrase in (
        "postgres-integration:",
        "postgres:16",
        "--health-cmd",
        "NOSUPERUSER",
        "NOCREATEDB",
        "NOCREATEROLE",
        "LOCALE_PROVIDER icu",
        "SQL_ASCII",
        "POSTGRES_CI_AUXILIARY_TARGETS_REQUIRED: \"1\"",
        "TEST_POSTGRES_URL",
        "bash scripts/check_postgres.sh",
    ):
        assert phrase in workflow
    assert "scripts/check_postgres.sh" not in _between(
        ".github/workflows/ci.yml", "standard-gate:", "postgres-integration:"
    )


def _module_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def test_root_readmes_are_entrypoints_for_complete_language_doc_bundles():
    expected_entry_sections = {
        "README.md": ("## Quick start", "## Documentation"),
        "README_zh.md": ("## 快速开始", "## 文档导航"),
    }
    retired_detail_sections = {
        "README.md": ("## Architecture Boundaries", "## Memory and Agent MCP"),
        "README_zh.md": ("## 架构边界", "## Memory 与 Agent MCP"),
    }

    for root_name, bundle in DOCUMENTATION_BUNDLES.items():
        root_text = _read_file(root_name)
        for heading in expected_entry_sections[root_name]:
            assert heading in root_text
        for heading in retired_detail_sections[root_name]:
            assert heading not in root_text
        for detail_path in bundle[1:]:
            assert f"./{detail_path}" in root_text, (
                f"{root_name} does not link canonical detail document {detail_path}"
            )
            assert (ROOT / detail_path).is_file()


def test_agents_entrypoint_stays_concise_and_routes_canonical_documents():
    """AGENTS.md is an operating index, not a second product/architecture manual."""
    text = _read_file("AGENTS.md")
    assert len(text.encode("utf-8")) <= 20_000, (
        "AGENTS.md exceeded its entrypoint budget; move detailed contracts to "
        "their canonical documents and keep a route here"
    )
    for retired_detail_heading in (
        "## Product Flow",
        "## MVP Scope",
        "## Architecture Baseline",
        "## Frontend/UI",
        "## LLM Configuration",
        "## Logging / Observability",
        "## 界面词汇表",
    ):
        assert retired_detail_heading not in text
    for path in (
        "docs/product-and-api.md",
        "architecture.md",
        "docs/deployment-and-configuration.md",
        "docs/operations.md",
        "docs/development.md",
        "docs/ui-vocabulary.md",
        "docs/agent-mcp-memory-sop.md",
        "silicon_notebook_fangan.md",
        "fangan_done.md",
    ):
        assert path in text, f"AGENTS.md does not route agents to {path}"
        assert (ROOT / path).is_file(), f"AGENTS.md links missing document {path}"


def test_claude_entrypoint_stays_claude_specific_and_routes_shared_contracts():
    """CLAUDE.md retains transport-specific rules without copying product contracts."""
    text = _read_file("CLAUDE.md")
    assert len(text.encode("utf-8")) <= 25_000, (
        "CLAUDE.md exceeded its resident-instruction budget; move shared product, "
        "architecture, and development contracts to their canonical documents"
    )
    for retired_detail_heading in (
        "## Extension UI Kit 与部署问答引擎",
        "### 交付完整性",
        "## 四、深度报告与逐步推理准确性契约",
        "## KG 探活响应合同",
        "## KG 检索查询材料",
    ):
        assert retired_detail_heading not in text
    for phrase in (
        "AGENTS.md",
        "docs/development.md",
        ".claude/hooks/require-subagent-model.py",
        "codex exec review --base <base>",
        "gh pr checks <PR号>",
        "gh pr merge <PR号> --rebase",
    ):
        assert phrase in text


def test_development_docs_require_guard_mutation_verification():
    english = _read_file("docs/development.md")
    chinese = _read_file("docs/development_zh.md")
    for phrase in ("mutation-verified", "deleting", "moving", "actually changed"):
        assert phrase in english
    for phrase in ("变异验证", "删除", "移动", "确实命中"):
        assert phrase in chinese


def test_development_docs_keep_efficiency_as_a_first_class_constraint():
    english = _read_file("docs/development.md")
    chinese = _read_file("docs/development_zh.md")
    for phrase in ("first-class engineering constraint", "LLM", "cached", "gated", "low-overhead"):
        assert phrase in english
    for phrase in ("一等工程约束", "LLM", "缓存", "门控", "低开销"):
        assert phrase in chinese


def test_development_docs_keep_cross_cutting_frontend_guardrails():
    english = _read_file("docs/development.md")
    chinese = _read_file("docs/development_zh.md")
    for phrase in (
        "long-running action",
        "disabled immediately",
        "FloatingModalCard",
        "AnomalyBadge",
        "sourceAnomalies()",
        "effort-picker.tsx::EffortPicker",
    ):
        assert phrase in english
    for phrase in (
        "长任务",
        "立即禁用",
        "FloatingModalCard",
        "AnomalyBadge",
        "sourceAnomalies()",
        "effort-picker.tsx::EffortPicker",
    ):
        assert phrase in chinese


def test_development_docs_own_current_schema_migration_rules():
    english = _read_file("docs/development.md")
    chinese = _read_file("docs/development_zh.md")
    agents = _read_file("AGENTS.md")
    assert (
        "| Operations, diagnostics, ingestion, migration execution, backfills | "
        "`docs/operations.md` and `_zh.md` | Paired operations reference |"
    ) in agents
    assert (
        "| Development workflow, architecture guardrails, schema/migration "
        "authoring, tests, CI, PR policy | `docs/development.md` and `_zh.md` | "
        "Paired development reference |"
    ) in agents
    for phrase in (
        f"The current schema version is {SCHEMA_VERSION}",
        "_migration_N",
        "SCHEMA_VERSION",
        "SQLite v59 / PostgreSQL v37",
        "indexing_pipeline_stages",
        "PostgreSQL 38",
    ):
        assert phrase in english
    for phrase in (
        f"当前 schema 版本为 {SCHEMA_VERSION}",
        "_migration_N",
        "SCHEMA_VERSION",
        "SQLite v59 / PostgreSQL v37",
        "indexing_pipeline_stages",
        "PostgreSQL v38",
    ):
        assert phrase in chinese


def test_migration_runbook_is_reachable_from_both_languages_and_declares_its_own():
    """The cutover runbook follows the single-Chinese-file runbook precedent
    (docs/runtime-dim-truncation-runbook.md) rather than the paired bundles in
    DOCUMENTATION_BUNDLES, which enumerate the five narrative documents only.

    codex review round 7 read that as breaking the bilingual contract. It does
    not — but the concern underneath is real: an English reader must never be
    silently handed a Chinese-only page, and must never lose access to any
    instruction. So pin both properties instead of duplicating the runbook into
    two files that would drift apart, which is exactly what the runbook's own
    "not a second source of truth" rule exists to prevent.
    """
    runbook = "docs/postgres-migration-runbook.md"
    assert (ROOT / runbook).is_file()
    assert runbook not in {
        path for bundle in DOCUMENTATION_BUNDLES.values() for path in bundle
    }
    for entry in ("docs/operations.md", "docs/operations_zh.md"):
        assert "postgres-migration-runbook.md" in _read_file(entry), (
            f"{entry} must link the execution runbook"
        )
    english = _read_file("docs/operations.md")
    assert "written in Chinese" in english and "complete English reference" in english, (
        "the English entry point must declare the runbook's language and that "
        "no instruction is available only there"
    )
    # Claiming completeness is only honest while the operational requirements
    # the runbook adds also exist here (codex review round 8 P2): the 2x work
    # directory rule, whose absence fails an activation after hours of copying,
    # and the login-versus-canary rollback boundary.
    for name, sizing in (
        ("docs/operations.md", "twice the source file"),
        ("docs/operations_zh.md", "源库大小的两倍"),
    ):
        text = _read_file(name)
        assert sizing in text, f"{name} must state the 2x work-directory rule"
        # Startup writers are pinned by
        # test_startup_writes_are_documented_as_unavoidable; here we only keep
        # the login boundary, which closes the last cheap-rollback window.
        assert "auth_sessions" in text, (
            f"{name} must say login writes PostgreSQL too, so rolling back "
            "after it costs the sessions"
        )


def test_startup_writes_are_documented_as_unavoidable():
    """There is no "started but provably untouched" PostgreSQL state.

    An earlier revision shipped a probe that counted in-progress rows and told
    operators a zero result proved no startup writes. That premise was simply
    false: `_initialize()` re-salts the admin password hash and updates the
    built-in row on *every* start (codex review round 11 P2), so the probe was
    guarding a property that cannot hold. It was removed rather than extended.
    What remains worth pinning is that all three startup writers stay named,
    including the post-readiness reprojection that rewrites KG objects — the
    one that is business data rather than harmless bookkeeping.
    """
    bootstrap = _read_file("backend/app/repositories/postgres/bundle.py")
    assert "WHERE id='user-local'" in bootstrap, (
        "the unconditional admin bootstrap update moved; re-check the "
        "rollback boundary the docs describe"
    )
    warmup = _read_file("backend/app/services/startup_warmup.py")
    assert "_reproject_legacy_knowhow_tables" in warmup

    runbook = _read_file("docs/postgres-migration-runbook.md")
    assert "SELECT (SELECT COUNT(*)" not in runbook, (
        "the zero-write probe is back; no probe can establish that claim while "
        "bootstrap writes unconditionally"
    )
    for name in (
        "docs/postgres-migration-runbook.md",
        "docs/operations.md",
        "docs/operations_zh.md",
    ):
        text = _read_file(name)
        for writer in (
            "user-local",
            "recover_interrupted_jobs",
            "_reproject_legacy_knowhow_tables",
        ):
            assert writer in text, f"{name} does not name startup writer {writer}"


def test_application_boundary_docs_name_actual_facades_clients_and_gate_contract():
    """Documentation records stable ownership, not source layout or totals."""
    _assert_phrases(
        {
            "README.md": "domain FastAPI routers composed by `backend/app/api/routes.py`",
            "README_zh.md": "由 `backend/app/api/routes.py` 组合的领域 FastAPI router",
            "architecture.md": "`backend/app/api/routes.py` composes the domain FastAPI routers",
        }
    )
    _assert_phrases(
        {
            "README.md": "aggregate is composition-only",
            "README_zh.md": "聚合层只负责 composition/order",
            "architecture.md": "aggregate 只负责组合顺序",
        }
    )
    design = _read(
        "docs/superpowers/specs/2026-07-21-application-boundary-foundation-design.md"
    )
    assert "**Status:** Implemented" in design
    assert "`routes.py` retains that composition surface only" in design
    assert "it does not re-export endpoint" in design
    assert "Three composition hotspots were present at baseline" in design
    assert "剩余架构计划仅为阶段 5" in _read("fangan_done.md")
    _assert_phrases(
        {
            "README.md": "`backend/app/models/schemas.py` is a legacy compatibility facade",
            "README_zh.md": "`backend/app/models/schemas.py` 是旧导入的兼容 facade",
            "architecture.md": "`backend/app/models/schemas.py` is a legacy compatibility facade",
        }
    )
    _assert_phrases(
        {
            "README.md": "shared `frontend/app/api-client.ts` transport",
            "README_zh.md": "共享 `frontend/app/api-client.ts` transport",
            "architecture.md": "`frontend/app/api-client.ts` is the shared transport",
        }
    )
    _assert_phrases(
        {
            "README.md": "default 12 backend pytest workers",
            "README_zh.md": "默认使用 12 个 backend pytest worker",
            "architecture.md": "默认使用 12 个 backend pytest worker",
        }
    )
    for name in ("README.md", "README_zh.md", "architecture.md"):
        assert "`BACKEND_PYTEST_WORKERS`" in _read(name)
    _assert_phrases(
        {
            "README.md": "CI lane timings are observational only",
            "README_zh.md": "CI 各 lane 时长仅作观察",
            "architecture.md": "CI 各 lane 时长仅作观察",
        }
    )
    _assert_phrases(
        {
            "README.md": "warm gate hard target is at most 60 seconds",
            "README_zh.md": "warm gate 硬目标是不超过 60 秒",
            "architecture.md": "warm gate 硬目标是不超过 60 秒",
        }
    )


def test_ask_disconnect_documentation_matches_detached_worker_contract():
    _assert_phrases(
        {
            "README.md": "A transport disconnect stops delivery to that client only",
            "README_zh.md": "transport 断连只停止向当前客户端继续推送",
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


def test_mount_documentation_describes_explicit_reference_library_model_and_zero_mount_cutover():
    """多领域基准库(2026-07-18/19)最终整支审查必办 5:notebook_bases 挂载集合
    取代了全局唯一 tier='base' 之后,最大的行为变化——升级到 schema 20 不回填
    挂载,所有既有笔记本挂载数清零,联邦检索对所有人停止直到用户自己去挂——
    此前只写在设计规格里(docs/superpowers/specs/2026-07-18-multi-domain-base-
    libraries-design.md §7),四份活文档(README/README_zh/AGENTS/architecture)
    零处提及。钉住四份文档都描述了这个升级断层,并且 architecture.md 不再用
    「跨 active + base」这个在多参考库模型下已不成立的措辞(暗示 base 仍是
    全局唯一、隐式参与)。"""
    _assert_phrases(
        {
            "README.md": "every pre-existing notebook starts with zero mounted reference libraries",
            "README_zh.md": "所有既有笔记本挂载数清零",
            "architecture.md": "所有既有笔记本挂载数清零",
        }
    )
    assert "跨 active + base 收集" not in _read("architecture.md"), (
        "「跨 active + base」暗示 base 仍是全局唯一隐式参与 —— 多参考库模型下"
        "已不成立,应改为按显式挂载集合描述(notebook_bases)"
    )


def test_workspace_documentation_names_four_tabs_and_actual_toolbar_actions():
    _assert_phrases(
        {
            "README.md": "four tabs — **问答** (Ask), **知识库** (Knowledge), **记忆** (Memory), and **深度报告** (Deep Report)",
            "README_zh.md": "四个 tab——**问答**（Ask）、**知识库**（Knowledge）、**记忆**（Memory）、**深度报告**（Deep Report）",
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
    fangan_done = _read("fangan_done.md")
    assert "`reports` table and `/reports` APIs" in readme
    assert "`reports` 表与 `/reports` API" in readme_zh
    # 改密流程已上线:旧的「no ... change-password flow」反向陈述必须消失,
    # 且密码合同(内置管理员拒绝 + 会话吊销范围)必须在权威产品/开发文档登记。
    product = _read_file("docs/product-and-api.md")
    product_zh = _read_file("docs/product-and-api_zh.md")
    assert "or change-password flow" not in product
    assert "no change-password / sharing / collaboration" not in product
    assert "The built-in `admin` account is rejected by both paths (409)" in product
    assert "every other browser session of that user is revoked" in product
    assert "内置 `admin` 账号在两条路径都被拒绝（409）" in product_zh
    assert "该用户其他浏览器会话全部吊销" in product_zh
    assert "更新日期：2026-08-04" in fangan_done
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


def test_repository_v9_compatibility_guards_remain_documented():
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


def test_completed_repository_boundary_claims_remain_documented():
    """Pin the prose; dedicated contract suites own production-source scans."""
    _assert_phrases(
        {
            "README.md": "Application services do not assemble product SQL",
            "README_zh.md": "application service 不拼装主业务库 SQL",
            "architecture.md": "application service 不拼装主业务库 SQL",
            "fangan_done.md": "application service 不再拼装主业务库 SQL",
        }
    )
    _assert_phrases(
        {
            "README.md": "one-hop delegates",
            "README_zh.md": "单跳委托",
            "architecture.md": "单跳委托",
            "fangan_done.md": "单跳委托",
        }
    )


def test_repository_runtime_and_verifier_completion_claims_are_synchronized():
    _assert_phrases(
        {
            "README.md": "Synchronous Ask/report submission failures",
            "README_zh.md": "Ask/report 同步提交失败",
            "architecture.md": "Ask/report 同步提交失败",
            "fangan_done.md": "Ask/report 同步提交失败",
        }
    )
    _assert_phrases(
        {
            "README.md": "only SHM mtime is exempt",
            "README_zh.md": "只豁免 SHM mtime",
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
    # B4:报告领域的构造搬进 ``_build_report_domain``,所以「引用同一个进程级
    # 注册表」这条链现在有两跳——builder 取全局、``__init__`` 挂到 runtime 上。
    # 两跳都钉住:少任一跳,runtime 都会拿到一个不是全局那份的 registry,而
    # 上面的 module 级 identity 断言仍然为真。
    report_source = inspect.getsource(repository_runtime._build_report_domain)
    init_source = inspect.getsource(repository_runtime.RepositoryRuntime.__init__)
    wire_source = inspect.getsource(
        repository_runtime.RepositoryRuntime.wire_report_execution
    )
    assert "report_cancellations=REPORT_CANCELLATIONS" in report_source
    assert "self.report_cancellations = report.report_cancellations" in init_source
    assert "cancellations=self.report_cancellations" in wire_source

    _assert_phrases(
        {
            "README.md": (
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


def test_repository_composition_history_keeps_v10_baseline():
    historical_chinese = (
        "本次重构不改变其 master 基线已有的 schema 版本（`SCHEMA_VERSION = 10`）。"
        "已提交的 v9 兼容 fixture 会经由既有 v10 migration 升级，并保持可读。"
    )
    for name in ("architecture.md", "fangan_done.md") + COMPOSITION_HISTORY_DOCS:
        assert historical_chinese in _read(name), (
            f"{name} is missing the historical schema statement"
        )

    for name in COMPOSITION_HISTORY_DOCS:
        text = _read(name)
        for stale in (
            "SCHEMA_VERSION=9",
            "SCHEMA_VERSION = 9",
            "SCHEMA_VERSION 保持 9",
            "SCHEMA_VERSION remains 9",
            "schema v9 and frozen-master",
        ):
            assert stale not in text, (
                f"{name} retains stale schema wording: {stale}"
            )


def test_ask_mode_documentation_keeps_chunk_default_and_alias_only_retirement():
    """`chunk` (default) / `reasoning` / `graph` are the modes; persisted
    `fast`/`global` ids survive only as aliases to `chunk`.  The older
    fast/global product description must not resurface."""
    _assert_phrases(
        {
            "README.md": "Retired ids `fast` and `global` are transparently remapped to `chunk`",
            "README_zh.md": "退役 id `fast`、`global` 透明映射到 `chunk`",
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


def _chinese_number(value: int) -> str:
    """Render 1..99 the way the Chinese docs actually spell a count."""
    digits = "零一二三四五六七八九"
    assert 1 <= value <= 99, f"unsupported documentation count: {value}"
    if value < 10:
        return digits[value]
    tens, ones = divmod(value, 10)
    head = "十" if tens == 1 else digits[tens] + "十"
    return head if ones == 0 else head + digits[ones]


def _where(name: str) -> str:
    """Name the files an assertion actually covers.

    `_read` silently expands `README.md` into its whole documentation bundle,
    so a bare failure reading "README.md is missing X" invites exactly the
    wrong fix: writing X into the lean root README. That is forbidden by the
    repository's documentation rules — and it WOULD turn the guard green,
    which is the worst possible combination. Spell the bundle out instead.
    """
    paths = DOCUMENTATION_BUNDLES.get(name, (name,))
    if paths == (name,):
        return name
    return f"the {name} bundle (any of: {', '.join(paths)})"


def _markdown_table_rows(text: str, header_prefix: str) -> list[list[str]]:
    """Return the body cells of the first Markdown table with this header."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip().startswith(header_prefix):
            break
    else:
        raise AssertionError(f"no Markdown table headed {header_prefix!r}")
    rows = []
    for line in lines[index + 2:]:  # skip the header and its separator row
        stripped = line.strip()
        if not stripped.startswith("|"):
            break
        rows.append([cell.strip() for cell in stripped.strip("|").split("|")])
    return rows


def _scope_names(text: str) -> set[str]:
    """Every `scope:name`-shaped backticked token in a fragment."""
    return set(re.findall(r"`([a-z_]+:[a-z_]+)`", text))


def test_current_mcp_docs_pin_the_complete_public_mcp_tool_surface():
    """Live docs must list every tool `/mcp` actually publishes.

    The expectation is DERIVED from `mcp_server.PUBLIC_TOOLS`, not a second
    copy of it. A hand-maintained tuple here pins the docs to whatever this
    test happened to believe, so shipping a new tool without documenting it
    stays green — the guard ends up guarding a stale document rather than
    the surface. Reading the runtime manifest makes the docs fail instead,
    and the prose count word is derived too so "二十个工具" cannot rot into
    a number nobody re-checked.

    `README.md` / `README_zh.md` are read as their documentation BUNDLES
    (see `DOCUMENTATION_BUNDLES`), so the per-language product/API reference
    and development guide are covered without forcing the lean root README
    to carry a tool list.
    """
    from app.api.mcp_server import PUBLIC_TOOLS
    from app.services.memory_service import AGENT_SCOPES

    expected_tools = set(PUBLIC_TOOLS)
    count_word = _chinese_number(len(PUBLIC_TOOLS))
    chinese_docs = ("architecture.md", "README_zh.md")
    english_docs = ("README.md",)

    # Existence check across every doc that claims to describe the surface.
    for name in chinese_docs + english_docs:
        text = _read(name)
        for tool in sorted(expected_tools):
            assert f"`{tool}`" in text, (
                f"{_where(name)} is missing current MCP tool `{tool}`"
            )

    # The full scope VOCABULARY is only owned by the developer contract and
    # the two per-language product references. `architecture.md` deliberately
    # discusses the write boundary rather than enumerating auth scopes, so
    # demanding the whole set there would push reference material into an
    # architecture document to satisfy a test.
    for name in ("README.md", "README_zh.md"):
        text = _read(name)
        for scope in sorted(AGENT_SCOPES):
            assert f"`{scope}`" in text, (
                f"{_where(name)} is missing Agent scope `{scope}`"
            )

    # SET EQUALITY on the authoritative per-language tool table. Existence
    # alone is one-directional: it cannot see a table that invents a tool the
    # server does not publish, and it cannot see the table being lifted out of
    # the product reference into some other file (the bundle would still
    # contain the names, from the prose around it). The table is the thing an
    # integrator reads as "the list", so it must equal the manifest exactly.
    for name, header in (
        ("docs/product-and-api.md", "| Group | Tools | Scope |"),
        ("docs/product-and-api_zh.md", "| 分组 | 工具 | Scope |"),
    ):
        rows = _markdown_table_rows(_read_file(name), header)
        documented = {
            tool
            for row in rows
            for tool in re.findall(r"`([a-z_]+)`", row[1])
        }
        assert documented == expected_tools, (
            f"{name}'s MCP tool table does not equal PUBLIC_TOOLS; "
            f"undocumented={sorted(expected_tools - documented)}, "
            f"invented={sorted(documented - expected_tools)}"
        )

    for name in chinese_docs:
        compact_text = "".join(_read(name).split())
        assert f"{count_word}个工具" in compact_text, (
            f"{_where(name)} must describe the complete "
            f"{len(PUBLIC_TOOLS)}-tool MCP surface"
        )
    for name in english_docs:
        # Tolerant on purpose: "these 20 tools", "exactly the 20 published
        # tools" and "the 20 MCP tools" are all correct English for the same
        # claim, and a bare f"{n} tools" literal would red-flag the prose
        # rather than the fact.
        pattern = rf"\b{len(PUBLIC_TOOLS)}\b[\w ]{{0,24}}\btools\b"
        assert re.search(pattern, _read(name)), (
            f"{_where(name)} must describe the complete "
            f"{len(PUBLIC_TOOLS)}-tool MCP surface"
        )

    # `fangan_done.md` / `silicon_notebook_fangan.md` are dated accounting:
    # "十一个工具" is a true statement about what the 2026-07-16 feature
    # delivered and is deliberately NOT rewritten. What must not survive is
    # the impression that eleven is still the whole surface, so each carries
    # a forward pointer whose count is derived here as well.
    for name in ("fangan_done.md", "silicon_notebook_fangan.md"):
        compact_text = "".join(_read(name).split())
        assert f"后续已扩展至{count_word}个工具" in compact_text, (
            f"{_where(name)} keeps a historical eleven-tool claim without a "
            f"forward pointer to the current {len(PUBLIC_TOOLS)}-tool surface"
        )

    stale_claims = (
        r"mcp_server\.py` 提供七个 scoped",
        r"(?:离线 )?smoke[：，][^。\n]{0,30}七工具契约",
    )
    for name in ("architecture.md", "fangan_done.md", "silicon_notebook_fangan.md"):
        text = _read(name)
        for pattern in stale_claims:
            assert re.search(pattern, text, flags=re.IGNORECASE) is None, (
                f"{name} retains obsolete seven-tool MCP wording: {pattern}"
            )


def test_agent_onboarding_sop_scope_tables_stay_bilingually_identical():
    """The two SOPs' scope tables must describe the same scope vocabulary.

    `docs/agent-mcp-memory-sop.md` and `_zh.md` are maintained as a pair, and
    the one thing a reader acts on directly is the "purpose → required scope"
    table they use to pick a least-privilege token. Updating one language and
    forgetting the other is the cheap, likely mistake, and it is invisible to
    anyone who reads only their own language.

    Comparing the SCOPE SETS (not the prose, and not row-by-row: the two
    tables legitimately word their purposes differently) is enough to catch
    it, and anchoring both to `AGENT_SCOPES` additionally makes a newly added
    scope fail here until BOTH SOPs offer it.
    """
    from app.services.memory_service import AGENT_SCOPES

    tables = {}
    for name, header in (
        ("docs/agent-mcp-memory-sop.md", "| Purpose | Required scope |"),
        ("docs/agent-mcp-memory-sop_zh.md", "| 用途 | 必需 scope |"),
    ):
        rows = _markdown_table_rows(_read_file(name), header)
        tables[name] = _scope_names("\n".join("|".join(row) for row in rows))

    (en_name, en_scopes), (zh_name, zh_scopes) = tables.items()
    assert en_scopes == zh_scopes, (
        f"{en_name} and {zh_name} scope tables diverged; "
        f"only in {en_name}: {sorted(en_scopes - zh_scopes)}, "
        f"only in {zh_name}: {sorted(zh_scopes - en_scopes)}"
    )
    for name, scopes in tables.items():
        assert scopes == set(AGENT_SCOPES), (
            f"{name}'s scope table does not equal AGENT_SCOPES; "
            f"missing={sorted(set(AGENT_SCOPES) - scopes)}, "
            f"unknown={sorted(scopes - set(AGENT_SCOPES))}"
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


def test_deployment_extension_boundary_is_in_canonical_deployment_docs():
    """Deployment details live in the paired deployment references, not agent entry files."""

    for name in (
        "docs/deployment-and-configuration.md",
        "docs/deployment-and-configuration_zh.md",
    ):
        normalized = (
            (ROOT / name)
            .read_text(encoding="utf-8")
            .casefold()
            .replace("_", " ")
            .replace("-", " ")
        )
        assert "extensions config" in normalized, name
        assert "deployment" in normalized, name
        assert "restart" in normalized or "重启" in normalized, name
    for name in (
        "docs/deployment-extensions-sop.md",
        "docs/deployment-extensions-sop_zh.md",
    ):
        assert "/api/extensions/{plugin_id}" in _read_file(name), name


def test_plugin_admission_degradation_timing_lives_in_product_docs():
    english = _read_file("docs/product-and-api.md")
    chinese = _read_file("docs/product-and-api_zh.md")
    for phrase in (
        "same core-owned trace callback",
        "without `duration_ms`",
        "immediately before the timed terminal step",
        "durable rows",
    ):
        assert phrase in english
    for phrase in (
        "同一个 core-owned 轨迹回调",
        "不带",
        "`duration_ms`",
        "终止步骤之前",
        "durable 轨迹行",
    ):
        assert phrase in chinese


def test_extension_runtime_toggle_contract_is_documented_bilingually():
    """The runtime-toggle contract's four load-bearing points must survive in both languages.

    This pins docs/deployment-and-configuration.md's registration of the
    admin runtime-toggle feature (architecture.md and
    docs/deployment-extensions-sop*.md carry the rest of the contract): the
    env var that governs cross-process convergence, the table that is its
    database source of truth, the "no row means enabled" default, and the
    offline-CLI/batch exception to convergence. It deliberately does not
    assert the numeric default/bounds — those may change freely; only their
    single canonical registration point is pinned here.
    """

    english = _read_file("docs/deployment-and-configuration.md")
    chinese = _read_file("docs/deployment-and-configuration_zh.md")
    for phrase in (
        "EXTENSION_ADMISSION_REFRESH_SECONDS",
        "extension_runtime_toggles",
        "no row meaning enabled",
        "prime this snapshot once, at startup composition, and never refresh it again",
    ):
        assert phrase in english, phrase
    for phrase in (
        "EXTENSION_ADMISSION_REFRESH_SECONDS",
        "extension_runtime_toggles",
        "无行即启用",
        "只在启动组合那一刻 prime 一次这份快照，运行期间不会再刷新",
    ):
        assert phrase in chinese, phrase


def test_user_facing_vocabulary_guard_is_documented_in_both_product_bundles():
    """The README bundles link the dedicated vocabulary source and explain its guard."""
    _assert_phrases(
        {
            "README.md": "PYTHONPATH=backend python scripts/check_ui_vocabulary.py",
            "README_zh.md": "PYTHONPATH=backend python scripts/check_ui_vocabulary.py",
        }
    )
    _assert_phrases(
        {
            "README.md": "[the UI vocabulary](./ui-vocabulary.md) is its single source of truth",
            "README_zh.md": "真源是[界面词汇约定](./ui-vocabulary.md)",
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
            "README.md": "`frontend/tests/guards/raw-enum-fallback.test.mjs`",
            "README_zh.md": "`frontend/tests/guards/raw-enum-fallback.test.mjs`",
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
    # 写后端错误文案时必须知道的约束,词汇真源与产品文档都要说明白。
    _assert_phrases(
        {
            "docs/ui-vocabulary.md": "守卫作用域跟着信任边界走",
            "README.md": "scope follows the **trust boundary rather than the directory tree**",
            "README_zh.md": "作用域跟着信任边界走、不跟着目录树走",
        }
    )
    _assert_phrases(
        {
            "docs/ui-vocabulary.md": '`user_error(status, "…")` 的消息字面量',
            "README.md": 'the message literals of every backend `user_error(status, "…")` call',
            "README_zh.md": '后端每处 `user_error(status, "…")` 的消息字面量',
        }
    )
    # 反向边界同样要写明,否则下一个人会顺手把 str(exc) 也纳进来。
    _assert_phrases(
        {
            "docs/ui-vocabulary.md": "裸 `HTTPException(detail=str(exc))` 刻意不在扫描面内",
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
    for name in ("README.md", "README_zh.md", "architecture.md", "fangan_done.md"):
        assert "Untitled notebook" in _read(name), (
            f"{name} 不再钉着默认库名 Untitled notebook——文档与代码已失配"
        )
