"""守卫: 同步层各表的「列类型契约」与真实建库结果一致。

``app.migration.sync.export`` 不从库里嗅探列类型 —— 它按
``app.migration.shadow.postgres_catalog.EXPECTED_COLUMNS``(从 PG 迁移 DDL 解析
出的每表每列契约)决定哪些列是 ``jsonb``、哪些是 ``timestamp with time zone``,
并且**对 SQLite 源也用同一份契约**(SQLite 是动态类型, 自己没有可信的类型信息)。

所以这份契约漏一列、或者把可空读成非空, 导出器就会悄悄走错分支: 漏掉的
timestamptz 列会被当普通列交给 ``encode_value``, 于是 PG 源上一个非 NULL 的
``datetime`` 会直接把 ``json.dumps`` 打崩 —— 而这只在那一列恰好有值时才发生。
本文件把「契约 == 真实 schema」钉成默认泳道的守卫, 而不是等某个生产笔记本先崩。

真实触发过的偏差(已修): ``_expected_columns`` 不拆
``ALTER TABLE t ADD COLUMN a ..., ADD COLUMN b ...`` 里的多个列, 只记第一列并把
整条语句尾巴当成它的定义 —— 同步层因此少了 10 列, 还把
``notebooks.indexing_pipeline`` 读成 NOT NULL(那个 NOT NULL 其实属于后一列)。
"""

from __future__ import annotations

import sqlite3

import pytest

from app.migration.shadow.postgres_catalog import EXPECTED_COLUMNS
from app.migration.sync.manifest import synced_tables
from app.repositories.postgres.schema_manifest import POSTGRES_ROWID_ORDINAL_TABLES


def _sqlite_columns(template_path, table: str) -> set[str]:
    connection = sqlite3.connect(template_path)
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    finally:
        connection.close()
    return {str(row["name"]) for row in rows}


@pytest.mark.parametrize("table", synced_tables())
def test_the_column_contract_matches_the_real_sqlite_schema(
    table, _sqlite_schema_template
):
    """Column NAMES, on the backend the exporter cannot ask for types. The
    PostgreSQL lane (tests/postgres/test_sync_export_pg.py) additionally pins
    the type and nullability of each one against information_schema."""
    contracted = set(EXPECTED_COLUMNS.get(table, {}))
    if table in POSTGRES_ROWID_ORDINAL_TABLES:
        # The one legitimate difference: ``ordinal`` is the PostgreSQL-side
        # identity surrogate for SQLite's implicit rowid, so it exists on one
        # backend only -- and the exporter drops it from the package anyway.
        contracted.discard("ordinal")
    live = _sqlite_columns(_sqlite_schema_template, table)

    assert contracted, f"{table}: no column contract parsed out of the migration DDL"
    assert contracted == live, (
        f"{table}: the migration-DDL column contract and the SQLite schema "
        f"disagree. Only in the contract: {sorted(contracted - live)}; only in "
        f"SQLite: {sorted(live - contracted)}. A column missing from the "
        "contract is exported through the wrong encoder."
    )
