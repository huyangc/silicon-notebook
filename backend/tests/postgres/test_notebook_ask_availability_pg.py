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
    from app.repositories.postgres.repository import PostgresRepository

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
