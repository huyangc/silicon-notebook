"""PG 侧 `community_graph_rows` 的流在**异常路径**上不能卡死连接释放。

背景:`cursor().stream()` 的生成器挂起期间连接是 ACTIVE 状态。消费循环中途抛异常
(`MemoryError`、`KeyboardInterrupt` ……)时,生成器仍被栈帧里的局部变量引用着、不会被
回收,于是调用方 `with self._connect()` 离开时的 `rollback()` 在 psycopg 3.3 里**永久
阻塞** —— 异常出不去,重建作业挂死。`rebuild_communities` 经 `_fold_canonical_edges`
的 `closing(...)` 兜住这一条。

这条用例在**真实 PostgreSQL** 上走一遍同样的路径(真的 `community_graph_rows`、真的
`PostgresDatabase.connect()`、折叠中途抛异常),但放进**子进程**里跑并设超时:少了
`closing` 时子进程会挂在 `rollback()` 上,这里按超时判红(并杀掉子进程),而不是把整条
PG 泳道挂住。纯 Python 的对应用例(不依赖 PG)在 `tests/test_fold_canonical_edges.py`。
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from app.repositories.postgres.knowledge_store import KnowledgeStore as PgKnowledgeStore
from app.repositories.postgres.migrator import PostgresMigrator
from app.repositories.postgres.unified_kg_store import UnifiedKgStore as PgUnifiedKgStore
from tests.postgres.test_community_graph_rows_twin_pg import NB, NOW, _seams, _seed

pytestmark = pytest.mark.postgres_integration

_CHILD = textwrap.dedent(
    """
    import sys
    from pathlib import Path

    from app.core.config import Settings
    from app.repositories.postgres.database import PostgresDatabase
    from app.repositories.postgres.unified_kg_store import UnifiedKgStore
    from app.services.knowledge_lifecycle import _fold_canonical_edges


    class Boom:
        def add(self, left, right):
            raise RuntimeError("boom")


    database = PostgresDatabase(Settings(_env_file=None), Path(sys.argv[1]))
    store = UnifiedKgStore(database)
    try:
        with database.connect() as connection:
            _names, rows = store.community_graph_rows(connection, sys.argv[2])
            _fold_canonical_edges(rows, {}, Boom())
    except RuntimeError as exc:
        assert str(exc) == "boom", exc
    else:
        raise SystemExit("the fold did not raise")
    with database.connect() as connection:
        assert connection.execute("SELECT 1 AS x").fetchone()["x"] == 1
    print("RELEASED")
    database.close()
    """
)


def test_an_exception_mid_fold_still_releases_the_connection(postgres_database):
    PostgresMigrator(postgres_database).migrate()
    knowledge = PgKnowledgeStore(postgres_database, _seams())
    unified = PgUnifiedKgStore(postgres_database, now=lambda: NOW)
    with postgres_database.write() as connection:
        _seed(connection, "%s", knowledge, unified)

    backend_root = Path(__file__).resolve().parents[2]
    env = {
        **os.environ,
        "DATABASE_URL": postgres_database.settings.database_url,
        "PYTHONPATH": str(backend_root),
        "LLM_LOG_ENABLED": "false",
    }
    try:
        result = subprocess.run(
            [sys.executable, "-c", _CHILD, str(backend_root.parent), NB],
            env=env, capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            "the connection release wedged after an exception mid-fold: an "
            "abandoned cursor().stream() leaves the connection ACTIVE and "
            "rollback() never returns. The fold must close() the row stream."
        )

    assert result.returncode == 0, result.stderr[-2000:]
    assert "RELEASED" in result.stdout
