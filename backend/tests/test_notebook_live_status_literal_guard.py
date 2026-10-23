"""守卫:`notebooks.status` 的三个词法类不得漂移(批 3·W1 T-1,摸底 5)。

⚠ **地位声明(codex PR#653 第 1 轮 P2)**:本文件是**文本棘轮**,不是这条不变量
的权威判据——权威判据是行为面守卫
``test_notebook_lifecycle_visibility.py``(SQLite 侧全量,PG 侧代表性抽查见
``tests/postgres/test_notebook_lifecycle_visibility_pg.py``),它直接播种
``status='deleting'``/``'copying'`` 的真实数据,断言 40 处收敛所覆盖的每个读
方法在**可观察行为**上把这些行当不存在——不管背后是常量、拼接、还是别的什么
写法都成立。本文件的 ``ast.Constant`` 正则扫描天然与拼写耦合:语义等价的改写
(比如把 ``!=`` 换成某种双重否定)可能漏检,对无害的合法重构也可能假红,单独
拿它当判据违反 AGENTS.md「测可观察行为与语义恒等,不测抄写的实现」。它仍然
有存在的价值——防「常量之外又长出一份新拼写」这一类具体且常见的回归,能在
CI 里比行为测试更快、更精确地指出出事的那一行——但角色是**辅助**,已知盲区
(写侧模块级常量提升绕过、诊断脚本 Python 布尔表达式够不到)登记在下面第②、
③条与 ``scripts/`` 那段里,不再假装是完备的判据。

三个词法类,语义各不相同,绝不能互相混淆:

1. **读侧「这行还不算存在」谓词**——单点收敛进
   ``postgres/access_sql.NOTEBOOK_LIVE_SQL`` /
   ``sqlite/access_sql.NOTEBOOK_LIVE_SQL``(均为
   ``"status NOT IN ('copying','deleting','importing','pending_delete')"``——
   ``pending_delete`` 是删除整本笔记本的一天导出窗口,窗口期内的五个例外入口
   各自走 ``access_sql`` 的窗口谓词、不经这条常量,见下面那条守卫;``importing`` 是
   跨环境导入器的在途哨兵,PR-2 加入;它与 ``copying`` 在**读侧**同义,在写侧
   不同义,理由见 ``access_sql.py`` 常量上方的注释)。折的是 40 处站点
   (``postgres/`` 20 + ``sqlite/`` 20,本次逐行枚举见规格 §T-1/摸底 5)。
   任何非常量站点再出现裸的 ``status != 'copying'`` / ``status <> 'copying'``
   (不论空格、``!=``/``<>``)都是回归——常量之外不该再有第二份拼写。
2. **写侧 8 处 copying 哨兵**——``postgres/sharing_store.py`` 与
   ``sqlite/sharing_store.py`` 的 ``compensate_copy``(各 1 处
   ``DELETE ... WHERE status='copying'``)与 ``sweep_stale_copies``(各 3 处:
   2 处只读连接上的候选选取 ``SELECT``——PR-4 逐本事务化后**不再带
   ``FOR UPDATE``**——加 1 处写事务内重验 ``SELECT``,PG 侧重验带
   ``FOR UPDATE``)。语义是「专指半拷贝,去物理删掉它」,与①的「还不算存在」
   不同义,**绝不折入** ``NOTEBOOK_LIVE_SQL``。
   ``status='copying'`` 等值形只许出现在这 8 处;白名单内出现 ``'deleting'``
   (哪怕只是把两值都塞进同一个 ``IN (...)``)同样失败——``sweep_stale_copies``
   一旦把 ``deleting`` 当半拷贝,会在逐本写事务里把正在后台清理的库整删,
   绕过分批清理器与归档(摸底 5;PR-4 之前这还是一条 ``id=ANY(%s)`` 的
   无界整删,风险同源)。
   ⚠ **纯源码字面量扫描本身有个可绕过的洞**(codex 评审必修 2):把哨兵谓词提成
   模块级常量、再用 f-string 在函数体里引用,该常量的 ``ast.Constant`` 就落在
   模块作用域(``enclosing_function_name is None``),per-function 扫描直接跳过
   它。真正堵死这条路的是 ``test_write_side_sentinel_actually_executed_never_
   folds_deleting``——它**真调用** ``compensate_copy``/``sweep_stale_copies``
   (两后端,sweep 的两个选取分支都走到),用一个记录型假连接拦下解释器实际会
   执行的 SQL,断言其中含 ``status='copying'`` 且全程不含 ``'deleting'``,不受
   「常量定义在哪个作用域」影响。源码字面量扫描保留作为更早、更精确的定位辅助,
   不是唯一防线。
3. **生产者 1 处**——``services/notebook_copy_writer.py`` 里
   ``_reset_copied_notebook_row`` 的 ``notebook_row.update(... status=status ...)``,
   取值是 ``CopyProfile.sentinel_status``(模块常量 ``COPYING`` / ``IMPORTING``):这是
   Python 关键字参数,不是 SQL 字符串,两条守卫的正则天然够不到它。写在这里点名,
   防止后来者以为它是漏扫的第 41 处。
4. **写侧 importing 哨兵(笔记本导入,``notebook_transfer.import_``)**——导入在
   ``status='importing'`` 且 ``sync_origin=''`` 的隐藏行后面写入;跨环境同步导入器
   (``app.migration.sync.import_``)的镜像也用 ``importing``,但 ``sync_origin`` 恒非空,
   它是**可续跑**的半成品,不是垃圾。所以导入的每一处写侧哨兵 SQL(心跳、发布、
   补偿、收割候选、转删除作业)都必须同时写明 ``sync_origin=''``:
   ``status='importing'`` 等值形只许出现在 ``_IMPORT_WRITE_SIDE_WHITELIST`` 的函数里,
   且 ``test_import_write_side_sentinel_never_touches_a_sync_mirror`` **真调用**这些
   方法(两后端),断言实际执行的每条含 ``status='importing'`` 的语句都带
   ``sync_origin=''``。

**规则式豁免(定稿,零豁免清单)**:``ast.walk`` 收集 ``ast.Constant(str)``,跳过
Module/ClassDef/FunctionDef 的首语句(docstring)。注释根本不进 AST,天然免扫。
本仓库的注释/docstring 是长篇散文体,枚举式清单在每次注释改动时都会假红,维护
成本高于它买到的确定性——所以不维护站点清单,只维护规则本身。

**扫描范围**:``backend/app/**.py`` + ``scripts/**.py``。``backend/tests/**``
刻意不扫——测试里出现这些字面量,恰恰是它在断言这条口径本身(如
``test_admin_user_notebooks.py`` 的 docstring),扫它只会制造噪音。

**``scripts/`` 侧有三处、不是两处真谓词消费者**(codex 评审顺手项 1 补齐):
``scripts/diag_db.py`` 的基线库枚举查询与样本库选取查询(SQL 文本)引用
``scripts/diag_common.NOTEBOOK_LIVE_SQL``;第三处是 base_recall 的挂载有效性
判定,那是 **Python 布尔表达式**(``status != "copying"``,双引号、不是 SQL 文本),
两条正则谓词守卫都够不到它,引用 ``scripts/diag_common.NOTEBOOK_HIDDEN_STATUSES``
(``frozenset({"copying", "deleting"})``)。这份 app-free 副本存在的原因是 README
明文 ``diag_db.py`` 「离线、纯 stdlib、app-free」,不能 import ``app.*``。两份副本
(SQL 常量字符串 + frozenset)都必须与两个后端的 ``access_sql.NOTEBOOK_LIVE_SQL``
逐字/逐值相等——由 ``test_diag_db_notebook_live_predicate_matches_access_sql``
钉住,不靠约定漂移。``scripts/diag_base_report.py`` 里 ``inactive_reason`` 的
白名单登记给 PR-3(那时 ``deleting`` 才会真的产出这个 reason 值),本文件不管它。
"""
from __future__ import annotations

import ast
import functools
import importlib.util
import os
import pathlib
import re
import sys
from collections import deque

import pytest

from tests.architecture.source_trees import read_source_tree

ROOT = pathlib.Path(__file__).resolve().parents[2]
APP_DIR = ROOT / "backend" / "app"
SCRIPTS_DIR = ROOT / "scripts"

# 读侧:常量之外不该再出现的两种不等形(空格、!=/<> 的写法都覆盖)。
_READ_SIDE_VIOLATION = re.compile(r"status\s*(?:!=|<>)\s*'copying'")
# 写侧:哨兵的等值形,只许出现在下面的白名单函数里。
_WRITE_SIDE_SENTINEL = re.compile(r"status\s*=\s*'copying'")

# (ROOT 相对路径, 函数名) —— 写侧 6 处哨兵的唯一合法住址(摸底 5)。
_WRITE_SIDE_WHITELIST: frozenset[tuple[str, str]] = frozenset({
    ("backend/app/repositories/postgres/sharing_store.py", "compensate_copy"),
    ("backend/app/repositories/postgres/sharing_store.py", "sweep_stale_copies"),
    ("backend/app/repositories/sqlite/sharing_store.py", "compensate_copy"),
    ("backend/app/repositories/sqlite/sharing_store.py", "sweep_stale_copies"),
})


# 写侧 importing 哨兵(模块 docstring 第④条):等值形只许出现在这些函数里。
_IMPORT_WRITE_SIDE_SENTINEL = re.compile(r"status\s*=\s*'importing'")
_IMPORT_WRITE_SIDE_WHITELIST: frozenset[tuple[str, str]] = frozenset(
    (f"backend/app/repositories/{backend}/{module}.py", function)
    for backend in ("sqlite", "postgres")
    for module, function in (
        ("sharing_store", "touch_import"),
        ("sharing_store", "publish_import"),
        ("sharing_store", "compensate_import"),
        ("sharing_store", "stale_import_ids"),
        ("notebook_delete_job_store", "request_delete_from_import"),
    )
)


def _scan_targets() -> list[pathlib.Path]:
    files = sorted(APP_DIR.rglob("*.py")) + sorted(SCRIPTS_DIR.rglob("*.py"))
    return [f for f in files if "__pycache__" not in f.parts]


@functools.lru_cache(maxsize=4096)
def _constants_with_scopes(tree: ast.Module) -> tuple[tuple[ast.Constant, str | None], ...]:
    """返回非 docstring 的 ``ast.Constant(str)`` 节点及最近函数名(不抽取
    ``.lineno``),不修改共享语法树。

    ⚠ 必修 1(codex 评审):``backend/tests/architecture/policy.py`` 的
    ``_lineno_is_identity`` line-number-identity 判据禁止把 ``.lineno`` 存进
    元组/列表/集合/字典/比较里
    (只放行「诊断具名 append 的 f-string 内联访问」这一种形态),曾经的
    ``out.append((node.lineno, node.value, ...))`` 正中这条判据,把
    ``check_backend_extended.sh`` 每日扩展门变红。返回节点本身,调用方在拼
    诊断 f-string 时才现取 ``.lineno``,且必须直接嵌进
    ``violations.append(f"...")`` 这一条语句,不经中间变量。
    """
    # Carry the nearest function alongside breadth-first traversal, preserving
    # ast.walk's diagnostic order without putting parent links on shared ASTs.
    pending: deque[tuple[ast.AST, str | None]] = deque([(tree, None)])
    skip_ids: set[int] = set()
    out: list[tuple[ast.Constant, str | None]] = []
    while pending:
        node, scope = pending.popleft()
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                    if isinstance(first.value.value, str):
                        skip_ids.add(id(first.value))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip_ids:
            out.append((node, scope))
        child_scope = node.name if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else scope
        pending.extend((child, child_scope) for child in ast.iter_child_nodes(node))
    return tuple(out)


def _all_scanned_constants() -> list[tuple[str, ast.Constant, str | None]]:
    """每次重读目录与内容;只复用不可变语法树的字符串/作用域投影。"""
    out: list[tuple[str, ast.Constant, str | None]] = []
    for path in _scan_targets():
        rel = path.relative_to(ROOT).as_posix()
        for node, scope in _constants_with_scopes(read_source_tree(path)):
            out.append((rel, node, scope))
    return out


def test_literal_projection_preserves_scopes_without_mutating_shared_trees(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text(
        '\"\"\"module documentation\"\"\"\n'
        'module_value = "module"\n'
        'class Holder:\n'
        '    "class documentation"\n'
        '    value = "class"\n'
        '    def outer(self):\n'
        '        "function documentation"\n'
        '        value = "outer"\n'
        '        class Local:\n'
        '            value = "local class"\n'
        '        async def inner():\n'
        '            "nested documentation"\n'
        '            return "inner"\n',
        encoding="utf-8",
    )
    tree = read_source_tree(sample)
    assert {(node.value, scope) for node, scope in _constants_with_scopes(tree)} == {
        ("module", None), ("class", None), ("outer", "outer"),
        ("local class", "outer"), ("inner", "inner"),
    }
    assert not any(hasattr(node, "parent") for node in ast.walk(tree))


def test_literal_scan_reads_changed_contents_and_discovers_new_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    monkeypatch.setattr(sys.modules[__name__], "APP_DIR", tmp_path / "app")
    monkeypatch.setattr(sys.modules[__name__], "SCRIPTS_DIR", tmp_path / "scripts")
    sample = tmp_path / "app" / "sample.py"
    sample.parent.mkdir()
    sample.write_text('value = "first"\n', encoding="utf-8")
    assert [(rel, node.value, scope) for rel, node, scope in _all_scanned_constants()] == [
        ("app/sample.py", "first", None)
    ]
    original_stat = sample.stat()
    sample.write_text('value = "other"\n', encoding="utf-8")
    os.utime(sample, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert [(rel, node.value, scope) for rel, node, scope in _all_scanned_constants()] == [
        ("app/sample.py", "other", None)
    ]
    sample.rename(sample.with_name("renamed.py"))
    assert [(rel, node.value, scope) for rel, node, scope in _all_scanned_constants()] == [
        ("app/renamed.py", "other", None)
    ]


# These scans share one immutable AST cache; loadgroup keeps that cache in
# one worker without changing which verification lane owns any assertion.
@pytest.mark.xdist_group(name="notebook_live_status_literals")
def test_no_raw_copying_read_side_literal_outside_the_constant():
    """常量之外不该再出现裸的 `status != 'copying'` / `status <> 'copying'`。"""
    violations: list[str] = []
    for rel, node, _scope in _all_scanned_constants():
        if _READ_SIDE_VIOLATION.search(node.value):
            violations.append(f"{rel}:{node.lineno}: {node.value!r}")
    assert not violations, (
        "读侧可见性谓词必须走 access_sql.NOTEBOOK_LIVE_SQL 单点,"
        "发现游离字面量:\n" + "\n".join(violations)
    )


@pytest.mark.xdist_group(name="notebook_live_status_literals")
def test_copying_write_side_sentinel_confined_to_sharing_store_whitelist():
    """`status='copying'` 等值形只许出现在两个 sharing_store.py 的两个方法里。"""
    violations: list[str] = []
    for rel, node, func in _all_scanned_constants():
        if not _WRITE_SIDE_SENTINEL.search(node.value):
            continue
        key = (rel, func or "")
        if key not in _WRITE_SIDE_WHITELIST:
            violations.append(f"{rel}:{node.lineno} (in {func!r}): {node.value!r}")
    assert not violations, (
        "写侧 copying 哨兵只许出现在 compensate_copy/sweep_stale_copies 内,"
        "发现白名单外站点:\n" + "\n".join(violations)
    )


@pytest.mark.xdist_group(name="notebook_live_status_literals")
def test_deleting_never_folds_into_the_copying_write_side_sentinel():
    """白名单函数体内的字面量绝不能出现 `'deleting'`。

    这是**辅助**扫描——只看白名单函数体内的 ``ast.Constant``,能精确定位一次
    显而易见的折叠(如把字面量直接改成 ``status IN ('copying','deleting')``)。
    它有一个已知盲区(把哨兵谓词提到模块级常量再用 f-string 引用,详见模块
    docstring 第②条与必修 2);权威防线是下面的
    ``test_write_side_sentinel_actually_executed_never_folds_deleting``。
    """
    violations: list[str] = []
    for rel, node, func in _all_scanned_constants():
        if func is None:
            continue
        if (rel, func) not in _WRITE_SIDE_WHITELIST:
            continue
        if "'deleting'" in node.value:
            violations.append(f"{rel}:{node.lineno} (in {func!r}): {node.value!r}")
    assert not violations, (
        "写侧 copying 哨兵绝不能折进两值谓词(会让 sweep_stale_copies 把 deleting "
        "中的库当半拷贝整删):\n" + "\n".join(violations)
    )


class _RecordingConnection:
    """记录每一次 ``execute(sql, params)`` 调用。``fetchall`` 固定返回一行假
    候选(非空)让 ``sweep_stale_copies`` 进入逐本循环;``fetchone`` 固定返回
    非 None 让逐本写事务的重验通过——两者合力推进到每本的 DELETE 分支,
    而不是在「无候选」或「重验不过就 continue」处提前退出。"""

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, sql: str, params=()) -> "_RecordingConnection":
        self.executed.append((sql, tuple(params) if params else ()))
        return self

    def fetchall(self) -> list[dict]:
        return [{"id": "nb-fake-stale-copy"}]

    def fetchone(self):
        return {"1": 1}


class _RecordingDatabase:
    """最小化 duck-typed ``database``:``with self.database.write() as connection``
    这一形态需要的 ``write()`` + 上下文管理器协议,别无其他。"""

    def __init__(self) -> None:
        self.connection = _RecordingConnection()

    def write(self) -> "_RecordingDatabase":
        return self

    def connect(self) -> "_RecordingDatabase":
        # PR-4:sweep 的候选选取挪到只读连接上;同一记录型连接照收其 SQL。
        return self

    def __enter__(self) -> _RecordingConnection:
        return self.connection

    def __exit__(self, *exc) -> bool:
        return False


class _FakeSettings:
    notebook_copy_stale_seconds = 3600


def _run_write_side_sentinel_queries() -> list[tuple[str, tuple]]:
    """必修 2(codex 评审):**真调用** 两后端的 ``compensate_copy`` 与
    ``sweep_stale_copies``(sweep 的 ``created_by=None`` 与有值两个选取分支都
    调用一次,外加它们各自的 DELETE 分支),用记录型假连接拦下解释器实际会
    执行的 SQL/参数。

    源码字面量扫描(见上面两条测试)只看某个函数体内的 ``ast.Constant``——把
    哨兵谓词提成模块级常量、函数体内只留一个 f-string 变量引用,该常量的
    ``ast.Constant`` 节点就落在模块作用域(``enclosing_function_name`` 返回
    ``None``),per-function 扫描会直接跳过它,即便常量里已经悄悄塞进了
    ``'deleting'``。真调用观察的是运行时**实际执行**的 SQL 文本,不受「哨兵字面量
    定义在源码哪个作用域」影响,堵死这条绕过路径。
    """
    from app.repositories.postgres.sharing_store import SharingStore as PgSharingStore
    from app.repositories.sqlite.sharing_store import (
        SharingStore as SqliteSharingStoreImpl,
    )

    executed: list[tuple[str, tuple]] = []
    for store_cls in (PgSharingStore, SqliteSharingStoreImpl):
        db = _RecordingDatabase()
        store = store_cls(
            database=db,
            settings=_FakeSettings(),
            now=lambda: "2020-01-01T00:00:00",
            insert_row=lambda *a, **k: None,
        )
        store.compensate_copy("nb-fake")
        store.sweep_stale_copies()
        store.sweep_stale_copies(created_by="u-fake")
        executed.extend(db.connection.executed)
    return executed


def test_write_side_sentinel_actually_executed_never_folds_deleting():
    """真调用两后端的写侧哨兵路径:执行的 SQL 必须含 `status='copying'`,且全程
    绝不出现 `'deleting'`——不受哨兵谓词在源码里定义在哪个作用域影响(必修 2)。"""
    executed = _run_write_side_sentinel_queries()
    assert executed, "两后端的 compensate_copy/sweep_stale_copies 都应至少执行一条语句"

    # 语义上最要紧的断言放最前:任何一条执行过的语句都不该出现 'deleting'——
    # 这是本测试要堵的洞本身,不该被下面的覆盖度断言先行遮住。
    leaked = [(sql, params) for sql, params in executed if "'deleting'" in sql]
    assert not leaked, f"写侧哨兵语句混入了 'deleting':\n{leaked}"

    # 覆盖度 sanity check(PR-4 逐本事务化后):compensate_copy 1 处 DELETE +
    # sweep_stale_copies 每次调用 1 处选取 SELECT + 1 处写事务内重验 SELECT
    # (假连接 fetchall 恒回一行、fetchone 恒回非 None → 每次 sweep 都推进到
    # 重验与逐本 DELETE)= 每后端 1+2×2=5 处、两后端合计 10 处——确认上面那条
    # 断言真的看过全部该看的语句,不是因为调用没打到目标分支才侥幸通过。
    sentinel_hits = [sql for sql, _params in executed if _WRITE_SIDE_SENTINEL.search(sql)]
    assert len(sentinel_hits) == 10, (sentinel_hits, executed)

    # DELETE 分支真的被推进了(内评 P2:fetchone 恒 None 的旧假连接让重验
    # 永远不过,逐本 DELETE 成了不可达代码,'deleting' 断言对它形同虚设):
    # 每次 sweep 调用对假候选执行 1 条 `DELETE FROM notebooks WHERE id=...`,
    # 2 后端×2 次调用 + 2 处 compensate_copy 的哨兵 DELETE = 6 条 notebooks DELETE。
    notebook_deletes = [
        sql for sql, _params in executed
        if "DELETE FROM notebooks" in sql
    ]
    assert len(notebook_deletes) == 6, (notebook_deletes, executed)


@pytest.mark.xdist_group(name="notebook_live_status_literals")
def test_importing_write_side_sentinel_confined_to_the_import_methods():
    """`status='importing'` 等值形只许出现在导入的写侧哨兵方法里(第④条)。"""
    violations: list[str] = []
    seen: set[tuple[str, str]] = set()
    for rel, node, func in _all_scanned_constants():
        if not _IMPORT_WRITE_SIDE_SENTINEL.search(node.value):
            continue
        key = (rel, func or "")
        seen.add(key)
        if key not in _IMPORT_WRITE_SIDE_WHITELIST:
            violations.append(f"{rel}:{node.lineno} (in {key[1]!r}): {node.value!r}")
    assert not violations, (
        "写侧 importing 哨兵只许出现在导入的哨兵方法内:\n" + "\n".join(violations)
    )
    # The whitelist names only places that still hold the sentinel (no rot).
    assert seen == _IMPORT_WRITE_SIDE_WHITELIST, sorted(_IMPORT_WRITE_SIDE_WHITELIST - seen)


class _ImportRecordingConnection(_RecordingConnection):
    rowcount = 1

    def fetchall(self) -> list[dict]:
        return [{"id": "nb-fake-import"}]


class _ImportRecordingDatabase(_RecordingDatabase):
    def __init__(self) -> None:
        self.connection = _ImportRecordingConnection()

    def write(self, operation: str = "") -> "_ImportRecordingDatabase":
        del operation
        return self


def test_import_write_side_sentinel_never_touches_a_sync_mirror():
    """真调用两后端导入的写侧哨兵方法(第④条):每条实际执行、含 `status='importing'`
    的语句都同时限定 `sync_origin=''`——同步镜像的 importing 行永远不会被导入的心跳、
    发布、补偿、收割或转删除作业碰到。"""
    from app.repositories.postgres.notebook_delete_job_store import (
        NotebookDeleteJobStore as PgDeleteJobs,
    )
    from app.repositories.postgres.sharing_store import SharingStore as PgSharingStore
    from app.repositories.sqlite.notebook_delete_job_store import (
        NotebookDeleteJobStore as SqliteDeleteJobs,
    )
    from app.repositories.sqlite.sharing_store import SharingStore as SqliteSharingStore

    executed: list[str] = []
    for sharing_cls, jobs_cls, now in (
        (PgSharingStore, PgDeleteJobs, "2020-01-01T00:00:00+00:00"),
        (SqliteSharingStore, SqliteDeleteJobs, "2020-01-01T00:00:00+00:00"),
    ):
        db = _ImportRecordingDatabase()
        store = sharing_cls(
            database=db, settings=_FakeSettings(), now=lambda: now,
            insert_row=lambda *a, **k: None,
        )
        store.touch_import("nb-fake")
        store.publish_import("nb-fake", "draft")
        store.compensate_import("nb-fake")
        store.stale_import_ids(older_than_seconds=60)
        jobs = jobs_cls(db, new_id=lambda prefix: f"{prefix}-fake", now=lambda: now)
        jobs.get = lambda job_id: {"id": job_id}  # the recording connection holds no row
        jobs.request_delete_from_import("nb-fake")
        jobs.request_delete_from_import("nb-fake", stale_seconds=60)
        executed.extend(sql for sql, _params in db.connection.executed)
    sentinel = [sql for sql in executed if _IMPORT_WRITE_SIDE_SENTINEL.search(sql)]
    # touch + publish + compensate (check + delete) + stale + 2 delete-job CAS, per backend.
    assert len(sentinel) == 2 * 7, sentinel
    # Both conditions in the statement's WHERE clause: a ``sync_origin=''`` moved
    # into SET (or anywhere else) would write the origin instead of guarding by it.
    unguarded = []
    for sql in sentinel:
        where = re.split(r"\bWHERE\b", sql, flags=re.IGNORECASE)
        clause = where[-1] if len(where) > 1 else ""
        if not (
            _IMPORT_WRITE_SIDE_SENTINEL.search(clause)
            and re.search(r"sync_origin\s*=\s*''", clause)
        ):
            unguarded.append(sql)
    assert not unguarded, unguarded


def _load_module(path: pathlib.Path, name: str):
    """临时以 ``name`` 执行 ``path`` 处的模块。``exec_module`` 需要模块先挂进
    ``sys.modules`` 才能正确处理内部的相对 import,但用完必须摘除——否则这个
    仅供本文件读常量用的假名字会常驻污染其余测试进程的 import 命名空间
    (顺手项 7)。"""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


def test_diag_db_notebook_live_predicate_matches_access_sql():
    """`scripts/diag_common.py` 的两份 app-free 副本(SQL 常量字符串 +
    Python 侧 frozenset)必须与两个后端的常量逐字/逐值相等。"""
    from app.repositories.postgres import access_sql as pg_access_sql
    from app.repositories.sqlite import access_sql as sqlite_access_sql

    diag_common = _load_module(SCRIPTS_DIR / "diag_common.py", "diag_common_guard_probe")

    assert pg_access_sql.NOTEBOOK_LIVE_SQL == sqlite_access_sql.NOTEBOOK_LIVE_SQL
    assert diag_common.NOTEBOOK_LIVE_SQL == pg_access_sql.NOTEBOOK_LIVE_SQL
    assert (
        diag_common.NOTEBOOK_LIVE_SQL
        == "status NOT IN ('copying','deleting','importing','pending_delete')"
    )

    # frozenset 的状态集合必须与 SQL 常量里 `NOT IN (...)` 的状态列表逐值一致,
    # 两份副本不能各说各话。
    sql_statuses = frozenset(
        re.findall(r"'([a-z_]+)'", pg_access_sql.NOTEBOOK_LIVE_SQL)
    )
    assert sql_statuses, "NOTEBOOK_LIVE_SQL 的 NOT IN 列表解析失败,正则需要复核"
    assert diag_common.NOTEBOOK_HIDDEN_STATUSES == sql_statuses


# 删除窗口的 SQL 状态字面量 `'pending_delete'`(带单引号的 SQL 形)只许出现在:两个
# 后端的 NOTEBOOK_LIVE_SQL 常量、诊断脚本的 app-free 副本、SQLite v100 迁移冻结的
# 部分索引 DDL(及快照校验器对它的逐字期望)。其余一切窗口判据都必须经 access_sql 的
# `notebook_delete_window_open_sql` / `notebook_delete_due_sql` /
# `export_access_exists_clause`(它们用 NOTEBOOK_PENDING_DELETE_STATUS 拼),否则
# 「窗口开着」的判据会长出第二份拼写——尤其是漏掉 delete_after 的那种。
_PENDING_DELETE_SQL_LITERAL = "'pending_delete'"
_PENDING_DELETE_LITERAL_FILES = frozenset({
    "backend/app/repositories/postgres/access_sql.py",
    "backend/app/repositories/sqlite/access_sql.py",
    "backend/app/repositories/sqlite/migrations.py",
    "scripts/diag_common.py",
    "scripts/verify_repository_snapshot.py",
})


@pytest.mark.xdist_group(name="notebook_live_status_literals")
def test_pending_delete_sql_literal_confined_to_the_constants():
    violations: list[str] = []
    for rel, node, _scope in _all_scanned_constants():
        if _PENDING_DELETE_SQL_LITERAL not in node.value:
            continue
        if rel not in _PENDING_DELETE_LITERAL_FILES:
            violations.append(f"{rel}:{node.lineno}: {node.value!r}")
    assert not violations, (
        "删除窗口的状态字面量只许出现在 access_sql 常量 / 诊断副本 / v100 迁移里,"
        "其余站点必须走窗口谓词函数:\n" + "\n".join(violations)
    )


def test_pending_delete_status_spelled_once_across_layers():
    """领域常量、两个后端的 NOTEBOOK_PENDING_DELETE_STATUS、NOTEBOOK_LIVE_SQL 的列表
    成员是同一个值:窗口状态在任何一层改名都会让另外几层静默失配。"""
    from app.domain.notebook_delete_grace import PENDING_DELETE_STATUS
    from app.repositories.postgres import access_sql as pg_access_sql
    from app.repositories.sqlite import access_sql as sqlite_access_sql

    assert PENDING_DELETE_STATUS == "pending_delete"
    assert pg_access_sql.NOTEBOOK_PENDING_DELETE_STATUS == PENDING_DELETE_STATUS
    assert sqlite_access_sql.NOTEBOOK_PENDING_DELETE_STATUS == PENDING_DELETE_STATUS
    assert f"'{PENDING_DELETE_STATUS}'" in pg_access_sql.NOTEBOOK_LIVE_SQL


def test_scan_targets_are_non_empty_sanity_check():
    """Sanity check only:确认扫描目标确实覆盖到了这两个锚点文件,不是判据
    本身——判据是上面几条基于规则(零豁免清单)的扫描,不是靠维护一份站点清单。"""
    targets = _scan_targets()
    assert any(p.name == "access_sql.py" for p in targets)
    assert any(p.name == "diag_db.py" for p in targets)
