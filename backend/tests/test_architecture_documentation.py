import ast
import inspect
import re
from pathlib import Path

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
PRODUCT_DOCS = ("docs/product-and-api.md", "docs/product-and-api_zh.md")
DEVELOPMENT_DOCS = ("docs/development.md", "docs/development_zh.md")
LIVE_REFERENCE_DOCS = PRODUCT_DOCS + ("architecture.md",)
COMPOSITION_HISTORY_DOCS = (
    "docs/superpowers/plans/2026-07-10-repository-composition-refactor.md",
    "docs/superpowers/specs/2026-07-10-repository-composition-refactor-design.md",
)


def _read(name: str) -> str:
    """Read exactly the named owner; a copy in another document cannot satisfy it."""
    return (ROOT / name).read_text(encoding="utf-8")


def _read_file(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _between(name: str, start: str, end: str | None = None) -> str:
    text = _read(name)
    section = text.split(start, 1)[1]
    return section.split(end, 1)[0] if end else section


def _assert_phrases(expected: dict[str, str]) -> None:
    for name, phrase in expected.items():
        assert phrase in _read(name), f"{name} is missing contract phrase: {phrase}"


def _assert_contract(name: str, phrases: tuple[str, ...]) -> None:
    """Allow prose reflow while retaining each owner's substantive contract."""
    compact = "".join(_read(name).split())
    for phrase in phrases:
        assert "".join(phrase.split()) in compact, (
            f"{name} is missing contract phrase: {phrase}"
        )


def _markdown_link_targets(section: str, document: str) -> set[str]:
    """Resolve ordinary inline/reference links without pinning labels or aliases."""
    references = {
        label.casefold(): target
        for label, target in re.findall(r"^\[([^\]]+)\]:\s+(\S+)", document, re.M)
    }
    targets = set(re.findall(r"\[[^\]\n]+\]\(([^\s)]+)\)", section))
    for label in re.findall(r"\[[^\]\n]+\]\[([^\]\n]+)\]", section):
        # Undefined reference-shaped text (including regex examples) is literal
        # Markdown, not a link. The required-target assertion detects a lost definition.
        if target := references.get(label.casefold()):
            targets.add(target)
    return {target.removeprefix("./") for target in targets}


def _assert_ledger_links(section: str, current_target: str) -> None:
    targets = _markdown_link_targets(section, _read("fangan_done.md"))
    assert current_target in targets, f"completion entry must link {current_target}"
    assert any(
        re.fullmatch(
            r"https://github\.com/huyangc/silicon-notebook/blob/[0-9a-f]{40}/"
            r"fangan_done\.md(?:#.*)?",
            target,
        )
        for target in targets
    ), "completion evidence must link an immutable historical ledger revision"


def _assert_ordered(section: str, phrases: tuple[str, ...]) -> None:
    positions = [section.index(phrase) for phrase in phrases]
    assert positions == sorted(positions), (
        f"contract phrases are out of order: {list(zip(phrases, positions))}"
    )


def test_postgres_integration_lane_is_separate_fail_closed_and_pg16_authoritative():
    from tests.postgres.lane import _pytest_command

    offline = _read("scripts/check.sh")
    assert "check_postgres.sh" not in offline
    assert "TEST_POSTGRES_URL" not in offline
    assert "postgres_integration" not in offline

    postgres = _read("scripts/check_postgres.sh")
    launcher = _read("backend/tests/postgres/lane.py")
    catalog_helpers = _read("backend/tests/postgres/conftest.py")
    assert 'TEST_POSTGRES_URL:?TEST_POSTGRES_URL is required' in postgres
    command = _pytest_command()
    assert command[command.index("-m", command.index("pytest")) + 1] == (
        "postgres_integration or postgres_lane_contract"
    )
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
        "TEST_POSTGRES_TARGETS_JSON",
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
    _assert_contract("docs/development.md", (
        "SQLite schema changes append `_migration_N`",
        "SCHEMA_VERSION",
        "never modify a sealed migration",
        "gap-free numbered SQL file",
        "POSTGRES_SCHEMA_MANIFEST",
        "validates checksums",
        "never rewrite an applied SQL file",
        "Current version numbers and per-version DDL are owned by these executable sources",
    ))
    _assert_contract("docs/development_zh.md", (
        "SQLite schema 变更新增 `_migration_N`",
        "SCHEMA_VERSION",
        "不修改已封存迁移",
        "连续编号 SQL",
        "POSTGRES_SCHEMA_MANIFEST",
        "在迁移锁下验证 checksum 与 ledger",
        "不能改写已应用 SQL",
        "当前版本号与逐版本 DDL 以这些可执行源文件为准",
    ))
    for name in DEVELOPMENT_DOCS:
        text = _read(name)
        targets = _markdown_link_targets(text, text)
        for path in (
            "backend/app/repositories/sqlite/migrations.py",
            "backend/app/repositories/postgres/migrations/",
            "backend/app/repositories/postgres/schema_manifest.py",
            "backend/app/repositories/postgres/migrator.py",
        ):
            assert f"../{path}" in targets, f"{name} must link schema owner {path}"
            assert (ROOT / path).exists()


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
    """Runtime topology belongs to architecture; verification belongs to development."""
    _assert_contract("architecture.md", (
        "`backend/app/api/routes.py` composes the domain FastAPI routers",
        "aggregate 只负责组合顺序",
        "`backend/app/models/schemas.py` is a legacy compatibility facade",
        "`frontend/app/api-client.ts` is the shared transport",
    ))
    _assert_contract("docs/development.md", (
        "default 12 backend pytest workers",
        "`BACKEND_PYTEST_WORKERS`",
        "CI lane timings are observational only",
        "warm gate hard target is at most 60 seconds",
    ))
    _assert_contract("docs/development_zh.md", (
        "默认使用 12 个 backend pytest worker",
        "`BACKEND_PYTEST_WORKERS`",
        "CI lane 时长仅作观察",
        "warm gate 硬目标是不超过 60 秒",
    ))
    design = _read(
        "docs/superpowers/specs/2026-07-21-application-boundary-foundation-design.md"
    )
    assert "**Status:** Implemented" in design
    assert "`routes.py` retains that composition surface only" in design
    assert "it does not re-export endpoint" in design
    assert "Three composition hotspots were present at baseline" in design
    ledger = _between("fangan_done.md", "## 2. 技术架构基础", "## 3.")
    assert "已交付" in ledger
    assert "方案 §10" in ledger
    _assert_ledger_links(ledger, "architecture.md")


def test_ask_disconnect_documentation_matches_detached_worker_contract():
    _assert_phrases({
        "docs/product-and-api.md":
            "A transport disconnect stops delivery to that client only",
        "docs/product-and-api_zh.md":
            "transport 断连只停止向当前客户端继续推送",
    })
    runtime = _between("architecture.md", "### 3.2 Ask 与 detached job", "### 3.2.1")
    _assert_ordered(runtime, (
        "transport disconnect / navigation / refresh",
        "停止向该客户端继续推送",
        "不设置 cancellation event",
        "detached worker 继续并可保存结果",
    ))
    for name in LIVE_REFERENCE_DOCS:
        text = _read(name)
        assert "frontend abort/client disconnect" not in text
        assert "Client disconnect / abort must propagate" not in text


def test_retrieval_documentation_scopes_federation_and_tier_tie_break_by_path():
    # Historical plans retain their contemporaneous model, not today's federation contract.
    _assert_contract("docs/product-and-api.md", (
        "reads the **participant set**",
        "`CHUNK_FEDERATION_ENABLED` is its switch",
        "The exact-score `base` tie-break applies only to knowledge-object hits",
        "`federated_retrieve_relations()` remains score-only",
    ))
    _assert_contract("docs/product-and-api_zh.md", (
        "按**参与集**读取 chunk",
        "开关是 `CHUNK_FEDERATION_ENABLED`",
        "exact-score 的 `base` 次序只适用于知识对象命中",
        "`federated_retrieve_relations()` 的关系命中仍只按 score 排序",
    ))
    for name in LIVE_REFERENCE_DOCS:
        for stale in (
            "base `1.20`", "base 1.20", "Every mode federates retrieval",
            "所有模式都跨 `tier=base`",
        ):
            assert stale not in _read(name), f"{name} retains {stale!r}"


def test_mount_documentation_describes_explicit_reference_library_model_and_zero_mount_cutover():
    """Explicit mounts, including the original no-backfill transition, stay user-visible."""
    _assert_contract("docs/product-and-api.md", (
        "participate only after an explicit mount",
        "Legacy schema-20 upgrades create no mounts automatically",
    ))
    _assert_contract("docs/product-and-api_zh.md", (
        "显式挂载后才参与检索",
        "旧 schema-20 升级不自动回填挂载",
    ))
    assert "`notebook_bases`" in _read("architecture.md")
    assert "跨 active + base 收集" not in _read("architecture.md"), (
        "federation must use explicit mounts, not a global implicit base"
    )


def test_workspace_documentation_names_four_tabs_and_actual_toolbar_actions():
    _assert_contract("docs/product-and-api.md", (
        "four tabs — **问答** (Ask), **知识库** (Knowledge), **记忆** (Memory), and **深度报告** (Deep Report)",
        "The Analysis menu itself contains only the promotion queue",
    ))
    _assert_contract("docs/product-and-api_zh.md", (
        "四个 tab——**问答**（Ask）、**知识库**（Knowledge）、**记忆**（Memory）、**深度报告**（Deep Report）",
        "「分析」菜单本身只包含晋升队列",
    ))
    # These snapshots predate Memory and must not be silently rewritten as current UI.
    for name in (
        "docs/superpowers/specs/2026-07-10-architecture-remediation-design.md",
        "docs/superpowers/plans/2026-07-10-architecture-contract-alignment.md",
    ):
        assert "问答 / 知识库 / 深度报告三个 tab" in _read(name)
    for name in LIVE_REFERENCE_DOCS:
        for retired in (
            "two tabs", "两个 tab", "Ask/Knowledge 主区域",
            "Studio-style article research", "Studio 类文章研究",
            "Mind Map", "Infographic", "派生规则审核",
        ):
            assert retired not in _read(name), f"{name} retains {retired!r}"


def test_live_workspace_docs_have_no_memory_omitting_tab_contracts():
    """Current UI references must not retain the pre-Memory tab list."""
    for name in LIVE_REFERENCE_DOCS:
        current = _read(name)
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
        "docs/product-and-api.md": _between(
            "docs/product-and-api.md", "## Memory and Agent MCP", "## KG extraction trigger"
        ),
        "docs/product-and-api_zh.md": _between(
            "docs/product-and-api_zh.md", "## Memory 与 Agent MCP", "## KG 抽取触发"
        ),
    }
    expected = {
        "docs/product-and-api.md": (
            "sanitized extraction candidates and server-validated evidence",
            "revalidates the Memory's current confirmed status and creator access",
            "one or more Base KG objects",
            "`base_object_ids`",
        ),
        "docs/product-and-api_zh.md": (
            "脱敏后的结构化提取候选与服务端验证过的 evidence",
            "重新校验 Memory 当前仍为 confirmed 且创建者仍有访问权",
            "一个或多个 Base KG 对象",
            "`base_object_ids`",
        ),
    }
    for name, section in sections.items():
        compact = "".join(section.split())
        for phrase in expected[name]:
            assert "".join(phrase.split()) in compact, (
                f"{name} is missing Memory promotion phrase: {phrase}"
            )
        for stale in (
            "审核 Memory revision 与经过验证的 provenance",
            "reviews the Memory revision and provenance",
            "create or merge a Base KG object", "create or merge a base object",
            "创建或合并 base object",
        ):
            assert "".join(stale.split()) not in compact, (
                f"{name} retains stale Memory wording: {stale}"
            )
    ledger = _between("fangan_done.md", "## 27. Agent Memory 与 MCP", "## 28.")
    assert "已交付" in ledger and "方案 §19" in ledger
    _assert_ledger_links(ledger, "docs/product-and-api_zh.md#memory-与-agent-mcp")


def test_source_cleanup_documentation_matches_reparse_and_delete_boundaries():
    _assert_contract("docs/product-and-api.md", (
        "Reparse preserves the source row and original file",
        "deletes the source row",
    ))
    _assert_contract("docs/product-and-api_zh.md", (
        "重新解析保留 source 行与原始文件",
        "删除 source 行",
    ))
    for name in LIVE_REFERENCE_DOCS:
        assert "article research artifacts" not in _read(name)
        assert "文章研究产物" not in _read(name)


def test_current_docs_describe_reports_and_sharing_without_retired_article_contracts():
    for name in LIVE_REFERENCE_DOCS:
        for obsolete in (
            "/articles", "/derived-rules", "article_claims",
            "derived_rule_candidates", "Article Studio", "article research",
        ):
            assert obsolete not in _read(name), f"{name} presents {obsolete!r} as current"
    _assert_phrases({
        "docs/product-and-api.md": "`reports` table and `/reports` APIs",
        "docs/product-and-api_zh.md": "`reports` 表与 `/reports` API",
    })
    for name, header, self_change, reset, protected in (
        (
            PRODUCT_DOCS[0], "| Account operation | Contract |",
            "Success retains the requesting session and revokes the user's other browser sessions",
            "revokes all target browser sessions",
            "Password change/reset for built-in `admin`",
        ),
        (
            PRODUCT_DOCS[1], "| 账号操作 | 契约 |",
            "成功保留当前会话并撤销该用户其他浏览器会话",
            "撤销目标用户全部浏览器会话",
            "修改／重置内置 `admin` 密码",
        ),
    ):
        rows = _markdown_table_rows(_read(name), header)
        contracts = {row[0]: row[1] for row in rows}
        password_row = next(
            value for key, value in contracts.items() if "PATCH /api/me/password" in key
        )
        reset_row = next(
            value for key, value in contracts.items() if "/reset-password" in key
        )
        assert self_change in password_row, name
        assert reset in reset_row, name
        assert "409" in contracts[protected], name
    product = _read("docs/product-and-api.md")
    assert "or change-password flow" not in product
    assert "no change-password / sharing / collaboration" not in product
    ledger = _between("fangan_done.md", "## 13. 历史记录：Article Studio", "## 14.")
    assert "已退役" in ledger
    assert "derived-rule candidates" in ledger
    _assert_ledger_links(ledger, "docs/product-and-api_zh.md#深度报告可信度与综合")


def test_architecture_document_keeps_other_current_runtime_boundaries():
    _assert_contract("architecture.md", (
        "chat、embedding 与 reranker 仍只通过 URL 服务访问",
        "`MINERU_MODE=http`",
        "`MINERU_MODE=cli`",
        "`MINERU_MODE=off`",
        "`ask_jobs` 行持久化",
        "cancellation event 注册在进程内",
        "服务重启后仍为 `running` 的 job 会转为 `interrupted`",
        "`status`、`trace`、`answer_id`",
        "不直接返回 `AskResponse`",
    ))


def test_repository_v9_compatibility_guards_remain_documented():
    for name in DEVELOPMENT_DOCS:
        _assert_contract(name, (
            "verify_repository_snapshot.py",
            "../backend/tests/fixtures/repository_v9/",
        ))
    _assert_contract("docs/development.md", (
        "constructs repositories only on a temporary backup",
        "per-version migrations/stable seeds",
    ))
    _assert_contract("docs/development_zh.md", (
        "只在临时 backup 上构造 repository",
        "逐版本迁移与稳定 seed",
    ))


def test_completed_repository_boundary_claims_remain_documented():
    """Dedicated contract suites own production scans; architecture owns boundaries."""
    _assert_contract("architecture.md", (
        "application service 不拼装主业务库 SQL",
        "单跳委托",
    ))


def test_repository_runtime_and_verifier_completion_claims_are_synchronized():
    _assert_contract("architecture.md", (
        "Ask/report 同步提交失败",
        "标记为 failed、注销 cancellation entry",
    ))
    _assert_contract("docs/development.md", (
        "preserves the original DB/WAL metadata",
        "SHM existence/size (only live-WAL SHM mtime may differ)",
    ))
    _assert_contract("docs/development_zh.md", (
        "保持原始 DB/WAL metadata",
        "SHM 存在性/大小不变（只有 live-WAL 的 SHM mtime 可不同）",
    ))


def test_projection_ownership_claim_matches_sql_and_application_boundaries():
    _assert_contract("architecture.md", (
        "store 独占 product SQL 与 raw row selection",
        "既定 application/query component 可组装 domain/application projection",
    ))
    for overclaim in (
        "row-to-domain projections", "row-to-domain projection",
        "SQL/row projection 只在 SQLite stores", "SQL 与 row-to-domain projection 全部归",
        "独占 SQL 与 row-to-domain projection", "Stores own SQL and row-to-domain projection",
    ):
        assert overclaim not in _read("architecture.md"), overclaim


def test_report_cancellation_is_the_documented_process_global_runtime_exception():
    assert report_engine.REPORT_CANCELLATIONS is report_execution.REPORT_CANCELLATIONS
    assert repository_runtime.REPORT_CANCELLATIONS is report_execution.REPORT_CANCELLATIONS
    # The domain builder and runtime wiring must reference the same process owner.
    report_source = inspect.getsource(repository_runtime._build_report_domain)
    init_source = inspect.getsource(repository_runtime.RepositoryRuntime.__init__)
    wire_source = inspect.getsource(
        repository_runtime.RepositoryRuntime.wire_report_execution
    )
    assert "report_cancellations=REPORT_CANCELLATIONS" in report_source
    assert "self.report_cancellations = report.report_cancellations" in init_source
    assert "cancellations=self.report_cancellations" in wire_source
    _assert_contract("architecture.md", (
        "`RepositoryRuntime` 持有或引用组合后的运行态",
        "`REPORT_CANCELLATIONS` 刻意保持 process-global canonical owner",
        "共享同一 identity reference",
    ))


def test_repository_composition_history_keeps_v10_baseline():
    """A dated refactor's baseline belongs to its history, not today's schema contract."""
    historical_chinese = (
        "本次重构不改变其 master 基线已有的 schema 版本（`SCHEMA_VERSION = 10`）。"
        "已提交的 v9 兼容 fixture 会经由既有 v10 migration 升级，并保持可读。"
    )
    for name in COMPOSITION_HISTORY_DOCS:
        text = _read(name)
        assert historical_chinese in text, f"{name} lost its historical baseline"
        for stale in (
            "SCHEMA_VERSION=9", "SCHEMA_VERSION = 9", "SCHEMA_VERSION 保持 9",
            "SCHEMA_VERSION remains 9", "schema v9 and frozen-master",
        ):
            assert stale not in text, f"{name} retains stale schema wording: {stale}"


def test_ask_mode_documentation_keeps_chunk_default_and_alias_only_retirement():
    """The product reference owns mode ids; the ledger must not revive Graph Ask."""
    _assert_phrases({
        "docs/product-and-api.md":
            "Retired ids `fast`, `global`, and `graph` are transparently remapped to `chunk`",
        "docs/product-and-api_zh.md":
            "退役 id `fast`、`global`、`graph` 透明映射到 `chunk`",
        "architecture.md": "退役 mode id 只保留兼容映射",
    })
    ledger = _read("fangan_done.md")
    assert "`chunk` / `reasoning`" in ledger
    assert "Graph Ask 也已退役为兼容别名" in ledger
    for name in LIVE_REFERENCE_DOCS + ("fangan_done.md",):
        assert "Global QA" not in _read(name)
        assert 'mode="global"' not in _read(name)
        assert 'mode="fast"' not in _read(name)


def test_direct_compatibility_followup_rewrite_error_copy_pins_to_the_original_question():
    """Un-`intent` `/ask`/`/ask/stream` calls only rewrite a follow-up after the
    deterministic clarification gate itself fires; the resulting 422 copy must
    always come from the original wording, never the rewritten one, in both
    product docs and the architecture contract that names the contextvar seam.
    """
    _assert_phrases(
        {
            "docs/product-and-api_zh.md": "文案恒取自原句（改写产物绝不进入错误文案",
            "docs/product-and-api.md": "whose message is always built from the original wording",
            "architecture.md": "其文案固定取自原句（改写产物绝不进入错误文案）",
        }
    )
    assert "followup_resolution_context" in _read("architecture.md")


def test_knowhow_documentation_matches_projection_isolation_and_agent_scopes():
    """The public contract owns projection behavior and read/write Agent scopes."""
    _assert_contract("docs/product-and-api.md", (
        "every non-empty cell becomes a knowledge-graph node whose *type is its column name*",
        "never generated or executed by the notebook, and never embedded/chunked/indexed into any KG projection",
        "Reading code still only needs `knowledge:read` — only writing it",
        "`knowhow:code`",
    ))
    _assert_contract("docs/product-and-api_zh.md", (
        "节点的类型就是所在列名",
        "格子照常切成 chunk 供问答使用，但不建任何图谱节点",
        "绝不自动触发",
        "`knowledge:read`",
        "`knowhow:code`",
    ))
    _assert_contract("architecture.md", (
        "唯一零 LLM 的 KG 写入方",
        "代码只存不执行，永不进 element/chunk/embedding/FTS/KG",
    ))


def _chinese_number(value: int) -> str:
    """Render 1..99 the way the Chinese docs actually spell a count."""
    digits = "零一二三四五六七八九"
    assert 1 <= value <= 99, f"unsupported documentation count: {value}"
    if value < 10:
        return digits[value]
    tens, ones = divmod(value, 10)
    head = "十" if tens == 1 else digits[tens] + "十"
    return head if ones == 0 else head + digits[ones]


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
    """Each canonical tool table must equal the runtime manifest, in both languages.

    A tool mentioned elsewhere cannot rescue an incomplete table. Architecture and
    the completion ledger route to this owner rather than maintaining another catalog.
    """
    from app.api.mcp_server import PUBLIC_TOOLS
    from app.services.memory_service import AGENT_SCOPES

    expected_tools = set(PUBLIC_TOOLS)
    for name, header in (
        ("docs/product-and-api.md", "| Group | Tools | Scope |"),
        ("docs/product-and-api_zh.md", "| 分组 | 工具 | Scope |"),
    ):
        text = _read(name)
        rows = _markdown_table_rows(text, header)
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
        for scope in sorted(AGENT_SCOPES):
            assert f"`{scope}`" in text, f"{name} is missing Agent scope `{scope}`"

    english_count = rf"\b{len(PUBLIC_TOOLS)}\b[\w ]{{0,24}}\btools\b"
    assert re.search(english_count, _read(PRODUCT_DOCS[0]))
    assert f"{_chinese_number(len(PUBLIC_TOOLS))}个工具" in "".join(
        _read(PRODUCT_DOCS[1]).split()
    )
    architecture = _read("architecture.md")
    assert "docs/product-and-api_zh.md#memory-与-agent-mcp" in _markdown_link_targets(
        architecture, architecture
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
    # The deferral describes the historical remediation scope, not current completion.
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
    ledger = _read("fangan_done.md")
    assert "architecture.md#6-已知架构债务与整改顺序" in _markdown_link_targets(
        ledger, ledger
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
    """Product references link the vocabulary owner; guard internals live with its rules."""
    for name in PRODUCT_DOCS:
        text = _read(name)
        assert "ui-vocabulary.md" in _markdown_link_targets(text, text), name
        assert "`user_error(...)`" in text, name
    _assert_contract("docs/ui-vocabulary.md", (
        "`scripts/check_ui_vocabulary.py` 由 `scripts/check.sh` 执行",
        "守卫作用域跟着信任边界走",
        "`frontend/app` 与 `frontend/features`",
        '`user_error(status, "…")` 的消息字面量',
        "裸 `HTTPException(detail=str(exc))` 刻意不在扫描面内",
        "`frontend/tests/guards/raw-enum-fallback.test.mjs`",
        "禁止把未知枚举原值直接展示给用户",
        "`backend/tests/test_ui_vocabulary_guard.py`",
        "表中每个词条都被规则覆盖或有显式豁免理由",
    ))


def test_default_notebook_name_is_documented_as_a_contract_not_copy():
    """The persisted default name must not be translated by a wording-only pass."""
    _assert_contract("docs/product-and-api.md", (
        "creates an `Untitled notebook`",
        "Internal identifiers and persisted defaults remain unchanged",
    ))
    _assert_contract("docs/product-and-api_zh.md", (
        "立即创建 `Untitled notebook`",
        "内部标识符与已持久化默认值保持不变",
    ))
