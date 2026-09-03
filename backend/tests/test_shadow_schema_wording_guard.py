"""守卫:forward shadow 迁移的措辞不得硬编码 schema 版本号。

为什么需要这条硬门(真实历史,不是假想):

  ``postgres_catalog.py`` / ``replicator.py`` 里约 28 条 raise 文案曾逐字写死
  "PostgreSQL v11 ...";v12、v13 两次 schema bump **都没有同步它**,文案在生产代码里
  谎报了两代。v14 那次 bump 修好了——办法是把 28 条字面量再手改一遍。也就是说,
  "改对了" 与 "下次还会错" 同时成立:这是一条跑步机,不是一次疏漏。

  更隐蔽的是 ``validate_postgres_v11_catalog`` 这个**函数名**:它一路活到 v14,
  连那次全量手改都漏掉了它(以及它在 caller_boundaries.json 里的守卫条目)。

所以真正的不变式不是 "版本号写对了",而是 **"版本号根本不出现在措辞里"**——
必须从 ``POSTGRES_SCHEMA_MANIFEST`` 派生。

刻意的例外只有一类:控制协议自己的代次(``shadow control v1``),它被哈希进 advisory
lock key 并逐字比对,跟着 schema bump 反而会**破坏**与在跑 worker 的互斥。它在
``ALLOWED_STRING_LITERALS`` 里显式登记,且登记项必须真实存在(见 exactness 测试),
否则白名单会烂成一张空头支票。

注意两条正则是**刻意不同**的:
  - 字符串/docstring 用 ``\\bv\\d+\\b``——散文里引用历史名字(``..._v11_catalog``)是
    合法的说明,不该报红。
  - 标识符用 ``v\\d+``(无词边界)——因为 ``validate_postgres_v11_catalog`` 里的 v11
    前面是下划线,``\\b`` **匹配不到**,当年正是这类形态漏网的。
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

from app.migration.shadow import postgres_catalog
from app.repositories.postgres.schema_manifest import POSTGRES_SCHEMA_MANIFEST


SHADOW_DIR = pathlib.Path(postgres_catalog.__file__).resolve().parent

# 散文可以引用历史版本名;字面量里独立成词的 v<数字> 不行。
_TEXT_VERSION = re.compile(r"\bv\d+\b")
# 标识符里任何位置的 v<数字> 都不行(含 `_v11_` 这种下划线包夹形态)。
_NAME_VERSION = re.compile(r"v\d+", re.I)

# (文件名, 完整字面量) -> 为什么它刻意保留。
ALLOWED_STRING_LITERALS: dict[tuple[str, str], str] = {
    (
        "control.py",
        "silicon-notebook shadow control v1 sha256:",
    ): "控制协议代次:哈希进 advisory lock key 并逐字比对,跟 schema bump 走会破坏互斥",
}


def _shadow_sources() -> list[pathlib.Path]:
    return sorted(SHADOW_DIR.rglob("*.py"))


def string_literal_violations(source: str, filename: str) -> list[tuple[int, str]]:
    """返回该源码里硬编码了 schema 版本的字符串字面量(已扣除白名单)。

    覆盖 raise 文案、模块/函数/类 docstring、以及 f-string 的常量段——凡是 AST 里
    的 str 常量都算,所以把字面量**挪到**同文件另一个函数里也照样报红。
    """
    violations: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if not _TEXT_VERSION.search(node.value):
            continue
        if (filename, node.value) in ALLOWED_STRING_LITERALS:
            continue
        violations.append((node.lineno, node.value))
    return violations


def symbol_violations(source: str, filename: str) -> list[tuple[int, str]]:
    """返回名字里带 schema 版本的函数/类/变量/参数。"""
    violations: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        name: str | None = None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            name = node.id
        elif isinstance(node, ast.arg):
            name = node.arg
        if name and _NAME_VERSION.search(name):
            violations.append((node.lineno, name))
    return violations


# --- 正向控制:守卫不是「永远失败」,也不是「永远通过」---------------------------


def test_live_shadow_package_has_no_hardcoded_schema_version_in_strings():
    offenders = {
        path.name: string_literal_violations(path.read_text(encoding="utf-8"), path.name)
        for path in _shadow_sources()
    }
    assert {name: hits for name, hits in offenders.items() if hits} == {}


def test_live_shadow_package_has_no_schema_version_in_symbol_names():
    offenders = {
        path.name: symbol_violations(path.read_text(encoding="utf-8"), path.name)
        for path in _shadow_sources()
    }
    assert {name: hits for name, hits in offenders.items() if hits} == {}


# --- 白名单必须是活的:登记项不存在就说明它已经烂成空头支票 -----------------------


def test_every_allowlisted_literal_still_exists_in_its_file():
    """白名单条目失配必须报红,否则它会静默变成一张放行任意文案的通行证。"""
    for (filename, literal), why in ALLOWED_STRING_LITERALS.items():
        path = SHADOW_DIR / filename
        assert path.exists(), f"白名单指向不存在的文件: {filename}"
        found = [
            node.value
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value == literal
        ]
        assert found, f"白名单条目已失效({why}): {filename} 里找不到 {literal!r}"


# --- 派生点本身:_SCHEMA_LABEL 必须来自 manifest,不能是字面量 --------------------


def test_schema_label_value_tracks_the_running_manifest():
    assert postgres_catalog._SCHEMA_LABEL == (
        f"PostgreSQL schema v{POSTGRES_SCHEMA_MANIFEST.postgres_version}"
    )


def test_schema_label_is_computed_from_the_manifest_not_a_literal():
    """值相等还不够——写死 "PostgreSQL schema v14" 也会让上面那条通过。

    所以这里检查 AST:赋值右侧必须真的引用 POSTGRES_SCHEMA_MANIFEST。
    """
    source = (SHADOW_DIR / "postgres_catalog.py").read_text(encoding="utf-8")
    assignments = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "_SCHEMA_LABEL"
            for target in node.targets
        )
    ]
    assert len(assignments) == 1, "_SCHEMA_LABEL 必须只有一处定义"
    referenced = {
        child.id for child in ast.walk(assignments[0].value) if isinstance(child, ast.Name)
    }
    assert "POSTGRES_SCHEMA_MANIFEST" in referenced


# --- 负向控制:把违规形态喂给**真实函数**,确认它真的报红 -------------------------
#
# 不重新实现解析逻辑——重新实现只能证明那份复制品会失败,证明不了这道门会失败。


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            'def f():\n    raise ValueError("PostgreSQL v14 column catalog drifted")\n',
            id="raise-literal",
        ),
        pytest.param(
            'def a():\n    pass\n\n\ndef b():\n'
            '    raise ValueError("PostgreSQL v14 column catalog drifted")\n',
            id="moved-to-another-function",
        ),
        pytest.param(
            'def f():\n    if x:\n        for i in y:\n'
            '            raise ValueError("PostgreSQL v11 index catalog drifted")\n',
            id="moved-into-nested-block",
        ),
        pytest.param(
            'def f(name):\n    raise ValueError(f"PostgreSQL v14 surface: {name}")\n',
            id="fstring-constant-part",
        ),
        pytest.param(
            '"""Migration-derived PostgreSQL v14 business-catalog validation."""\n',
            id="module-docstring",
        ),
        pytest.param(
            'def f():\n    """Reject same-name semantic v11 drift."""\n',
            id="function-docstring",
        ),
        pytest.param(
            '_SCHEMA_LABEL = "PostgreSQL schema v14"\n',
            id="label-regressed-to-a-literal",
        ),
    ],
)
def test_hardcoded_version_in_strings_is_detected(source):
    assert string_literal_violations(source, "postgres_catalog.py")


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            "def validate_postgres_v11_catalog():\n    pass\n", id="stale-function-name"
        ),
        pytest.param(
            "def validate_postgres_v14_catalog():\n    pass\n", id="current-but-pinned-name"
        ),
        pytest.param("class SchemaV14Contract:\n    pass\n", id="class-name"),
        pytest.param("_V14_TABLES = ()\n", id="module-constant-name"),
    ],
)
def test_schema_version_in_symbol_names_is_detected(source):
    assert symbol_violations(source, "postgres_catalog.py")


def test_allowlist_does_not_leak_across_files():
    """白名单按 (文件, 字面量) 生效——同一段文案换个文件出现,仍须报红。"""
    literal = next(iter(ALLOWED_STRING_LITERALS))[1]
    source = f'X = "{literal}"\n'
    assert not string_literal_violations(source, "control.py")
    assert string_literal_violations(source, "postgres_catalog.py")


def test_historical_prose_reference_stays_allowed():
    """散文里引用旧名字是刻意保留的说明,不该被这道门误伤。"""
    source = 'def f():\n    """不像 ``..._v11_catalog`` 那样会过期。"""\n'
    assert not string_literal_violations(source, "postgres_catalog.py")


def test_every_partial_unique_index_has_a_pinned_predicate():
    """A new partial unique index must be pinned in `_UNIQUE_PREDICATES`.

    `_build_unique_surfaces()` fails closed on an unpinned predicate, and it runs
    at replicator construction — so forgetting the pin does not break one row, it
    stops every forward-shadow run for that schema version from starting. Nothing
    else exercises this path, so a migration could ship the breakage silently.
    """
    from app.migration.shadow.manifest import MANIFEST
    from app.migration.shadow.replicator import (
        _UNIQUE_PREDICATES,
        _build_unique_surfaces,
    )

    # Construction is the assertion: it raises for any unpinned predicate.
    surfaces = _build_unique_surfaces(MANIFEST)
    assert surfaces

    # Spell out the same requirement directly, so the failure names the index
    # instead of only surfacing a generic "unpinned predicate" ValueError.
    from app.migration.shadow.replicator import EXPECTED_OPERATIONAL_INDEXES

    for name, index in EXPECTED_OPERATIONAL_INDEXES.items():
        if index.unique and index.predicate_tokens:
            assert name in _UNIQUE_PREDICATES, f"unpinned partial unique index: {name}"
            assert _UNIQUE_PREDICATES[name][1] == index.predicate_tokens, name


def test_agent_observations_park_strategies_are_the_pinned_shape():
    """Agentic Memory P3, T1: ``agent_observations`` has TWO unique surfaces
    that must resolve to two DIFFERENT park strategies, exactly as recorded
    in the T1 park-strategy argument (``_migration_55`` / 0033_agent_
    observations.sql):

    - ``pk_agent_observations`` is a single-column primary key equal, verbatim,
      to the manifest's declared replication key -- so it must resolve to
      REPLICATION_KEY (zero sentinel column, zero nullable column, zero leaf
      delete/reinsert).
    - ``idx_agent_observations_request`` is the add_observation idempotency
      index. Its first three columns (notebook_id, owner_id, agent_profile_id)
      are all NOT NULL, so the only column the resolver can pick to park on is
      the nullable fourth one, client_request_id -- it must resolve to NULL
      with park_column == "client_request_id", never a sentinel strategy.

    This is a direct assertion of the resolved shape (as opposed to
    `test_every_partial_unique_index_has_a_pinned_predicate` above, which
    only proves construction does not raise). Mutation-verified during T1's
    implementation: reverting client_request_id to `NOT NULL DEFAULT ''`
    flips this test's strategy assertion to SENTINEL_TEXT (red), and
    removing the `_UNIQUE_PREDICATES` pin makes `_build_unique_surfaces`
    raise at construction (red for the whole file, per the test directly
    above this one).
    """
    from app.migration.shadow.manifest import MANIFEST
    from app.migration.shadow.replicator import _ParkStrategy, _build_unique_surfaces

    surfaces = _build_unique_surfaces(MANIFEST)

    pk_surface = surfaces["pk_agent_observations"]
    assert pk_surface.table == "agent_observations"
    assert pk_surface.columns == ("id",)
    assert pk_surface.strategy is _ParkStrategy.REPLICATION_KEY
    assert pk_surface.park_column is None

    idx_surface = surfaces["idx_agent_observations_request"]
    assert idx_surface.table == "agent_observations"
    assert idx_surface.columns == (
        "notebook_id",
        "owner_id",
        "agent_profile_id",
        "client_request_id",
    )
    assert idx_surface.strategy is _ParkStrategy.NULL
    assert idx_surface.park_column == "client_request_id"
    assert idx_surface.predicate == "client_request_id IS NOT NULL"


def test_ask_jobs_client_request_surface_parks_on_the_nullable_key():
    """v70/0050: ``idx_ask_jobs_client_request`` is the Ask submission
    idempotency index. ``created_by`` is NOT NULL, so the only column the
    resolver can park on is the nullable ``client_request_id`` -- the same
    NULL-park shape as ``idx_agent_observations_request`` above. Reverting the
    column to ``NOT NULL DEFAULT ''`` or dropping the ``WHERE`` clause would
    flip this to a sentinel strategy and break the whole schema version's
    replicator construction (see the two tests above)."""
    from app.migration.shadow.manifest import MANIFEST
    from app.migration.shadow.replicator import _ParkStrategy, _build_unique_surfaces

    surface = _build_unique_surfaces(MANIFEST)["idx_ask_jobs_client_request"]
    assert surface.table == "ask_jobs"
    assert surface.columns == ("created_by", "client_request_id")
    assert surface.strategy is _ParkStrategy.NULL
    assert surface.park_column == "client_request_id"
    assert surface.predicate == "client_request_id IS NOT NULL"
