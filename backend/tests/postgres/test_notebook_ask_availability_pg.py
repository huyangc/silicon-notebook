"""``ask_available`` 参考库原文判据的 PostgreSQL 孪生(这一侧是规范定义)。

SQLite 侧的全量矩阵在 ``tests/test_notebook_ask_availability.py``;这里在真
``PostgresRepository``、真 psycopg 连接上钉同一组判定:空笔记本挂一个**无图**参考库,
参考库有可见来源的 chunk 才放行;没有 chunk、只有隐藏投影的 chunk、查看者读不到、
排在联邦参与上界之外、联邦通道关掉时一律仍拦;判据是一条与挂载数无关的语句。
"""
from __future__ import annotations

import pytest

from app.core.request_context import reset_request_user, set_request_user
from app.models.notebooks import NotebookCreate
from app.repositories.postgres._store_utils import normalize_timestamp


pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_notebook_ask_availability"),
]


@pytest.fixture
def repo(postgres_settings):
    from app.services.postgres_repository import PostgresRepository

    repository = PostgresRepository(postgres_settings)
    marker = set_request_user(repository.current_user())
    try:
        yield repository
    finally:
        reset_request_user(marker)
        repository.close()


def _graphless_library(repo, name, *, source_type="markdown", chunked=True):
    runtime = repo._runtime
    library = repo.create_notebook(NotebookCreate(name=name))
    now = normalize_timestamp(runtime.seams.now())
    with runtime.database.write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,"
            "created_at,updated_at) VALUES (%s,%s,%s,%s,'ready',%s,%s)",
            (f"s-{name}", library.id, name, source_type, now, now),
        )
        if chunked:
            db.execute(
                "INSERT INTO chunks (id,notebook_id,source_id,text,created_at) "
                "VALUES (%s,%s,%s,'some retrievable text',%s)",
                (f"c-{name}", library.id, f"s-{name}", now),
            )
    return library


def _empty_notebook_mounting(repo, libraries, name="mounts-graphless"):
    nb = repo.create_notebook(NotebookCreate(name=name))
    repo.replace_notebook_bases(
        nb.id, [library.id for library in libraries], repo.current_user().id
    )
    return nb


def test_graphless_library_with_visible_chunks_is_ask_available(repo):
    nb = _empty_notebook_mounting(repo, [_graphless_library(repo, "ref-text")])
    summary = repo.get_notebook(nb.id)
    assert summary.base_kg_available is False
    assert summary.local_evidence_available is False
    assert summary.ask_available is True


@pytest.mark.parametrize(
    ("source_type", "chunked"),
    [
        pytest.param("markdown", False, id="no-chunk"),
        pytest.param("knowhow", True, id="hidden-projection-chunk-only"),
    ],
)
def test_library_without_visible_text_is_not_ask_available(repo, source_type, chunked):
    library = _graphless_library(
        repo, "ref-none", source_type=source_type, chunked=chunked
    )
    nb = _empty_notebook_mounting(repo, [library])
    assert repo.get_notebook(nb.id).ask_available is False


def test_library_the_viewer_cannot_read_does_not_make_ask_available(repo):
    library = _graphless_library(repo, "ref-private")
    nb = _empty_notebook_mounting(repo, [library], name="shared-a")
    bob = repo._runtime.identity.create_user("c00140002", "pw")
    repo._runtime.sharing.add_member(nb.id, bob.id)
    assert repo.get_notebook(nb.id).ask_available is True   # 挂载人自己:正对照

    marker = set_request_user(bob)
    try:
        summary = repo.get_notebook(nb.id)
        assert summary.base_notebooks == []
        assert summary.ask_available is False
    finally:
        reset_request_user(marker)


def test_federation_switch_and_bound_gate_the_library_text(repo):
    first = _graphless_library(repo, "a-first", chunked=False)
    second = _graphless_library(repo, "b-second")
    nb = _empty_notebook_mounting(repo, [second, first])
    assert repo.get_notebook(nb.id).ask_available is True
    repo.settings.chunk_federation_max_participants = 2
    assert repo.get_notebook(nb.id).ask_available is False   # 第二本在上界之外
    repo.settings.chunk_federation_max_participants = 8
    repo.settings.chunk_federation_enabled = False
    assert repo.get_notebook(nb.id).ask_available is False


def test_library_text_probe_is_one_statement_whatever_the_mount_count(
    repo, monkeypatch,
):
    notebooks = {}
    for count in (1, 5, 50):
        libraries = [
            _graphless_library(repo, f"r{count}-{i:02d}", chunked=False)
            for i in range(count)
        ]
        notebooks[count] = _empty_notebook_mounting(
            repo, libraries, name=f"mounts-{count}"
        )
        repo.get_notebook(notebooks[count].id)
    sql: list[str] = []
    database = repo._runtime.database
    orig_connect = database.connect

    class _SpyConn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, statement, *a, **kw):
            sql.append(str(statement))
            return self._inner.execute(statement, *a, **kw)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    class _SpyContext:
        def __init__(self, inner):
            self._inner = inner

        def __enter__(self):
            return _SpyConn(self._inner.__enter__())

        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)

    monkeypatch.setattr(database, "connect", lambda: _SpyContext(orig_connect()))
    traces = {}
    for count, nb in notebooks.items():
        sql.clear()
        assert repo.get_notebook(nb.id).ask_available is False
        traces[count] = list(sql)
    assert len([s for s in traces[5] if "FROM chunks c" in s]) == 1
    assert traces[1] == traces[5] == traces[50]


_FORBIDDEN_PLAN_SHAPES = ("idx_chunks_nb", "Merge Semi Join", "Seq Scan on chunks")


@pytest.mark.parametrize("analyzed", [False, True], ids=["no-stats", "analyzed"])
def test_library_text_probe_plan_never_walks_chunks_by_notebook(repo, analyzed):
    """计划钉(评审形状的缩小版):参考库 200 篇可见来源、一条 chunk 都没有,别处
    4000 条 chunk。内层若能按 notebook 走 chunk,无统计时会逐篇来源做
    ``BitmapAnd(idx_chunks_nb_created, idx_chunks_source)``,有统计时可能整表
    ``Merge Semi Join``(评审实测 27–42 s / 97–115 ms)。

    规模取「仍能让变异(恢复内层 notebook 相等)在无统计态现出禁用形状」的下限
    的两倍:本机实测 100 篇 / 2000 条仍现形、50 篇 / 1000 条已不现形。只跑普通
    ``EXPLAIN``(不带 ANALYZE,不执行语句),只断言计划形状。

    只钉**禁用的形状**,不钉某一个具体索引名或 Index Only Scan:后者会随可见性图
    翻转(见 memory_sql 计划钉的历史)。autovacuum 在插入前关掉,无统计态才名副
    其实;有统计态显式 ANALYZE。播种与 ANALYZE 都 ``SET LOCAL statement_timeout =
    '0'``,与其余计划钉的播种同款:那批插入每行都过 ``sources`` 外键检查,繁忙的
    CI 机器上会越过池子的语句超时(CI 曾在 chunk 插入的外键检查里 QueryCanceled)。"""
    runtime = repo._runtime
    library = repo.create_notebook(NotebookCreate(name="ref-unchunked"))
    other = repo.create_notebook(NotebookCreate(name="elsewhere"))
    now = normalize_timestamp(runtime.seams.now())
    with runtime.database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        db.execute("ALTER TABLE sources SET (autovacuum_enabled = false)")
        db.execute("ALTER TABLE chunks SET (autovacuum_enabled = false)")
        for prefix, notebook_id in (("sl", library.id), ("so", other.id)):
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,source_type,status,"
                "created_at,updated_at) SELECT %s || g, %s, 't', 'markdown', "
                "'ready', %s, %s FROM generate_series(1, 200) g",
                (f"{prefix}-", notebook_id, now, now),
            )
        db.execute(
            "INSERT INTO chunks (id,notebook_id,source_id,text,created_at) "
            "SELECT 'co-' || g, %s, 'so-' || (1 + g %% 200), 't', %s "
            "FROM generate_series(1, 4000) g",
            (other.id, now),
        )
    if analyzed:
        with runtime.database.write() as db:
            db.execute("SET LOCAL statement_timeout = '0'")
            db.execute("ANALYZE sources")
            db.execute("ANALYZE chunks")

    statements: list[tuple[str, tuple]] = []

    class _Recorder:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, statement, params=(), **kw):
            statements.append((str(statement), tuple(params)))
            return self._inner.execute(statement, params, **kw)

    with runtime.database.connect() as db:
        assert runtime.queries.notebooks_have_visible_chunk(
            _Recorder(db), [library.id]
        ) is False
        (statement, params), = statements
        rows = db.execute("EXPLAIN " + statement, params, prepare=False).fetchall()
    plan = "\n".join(str(next(iter(dict(row).values()))) for row in rows)
    for shape in _FORBIDDEN_PLAN_SHAPES:
        assert shape not in plan, plan
