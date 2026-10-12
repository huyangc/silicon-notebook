"""深拷贝把来源图片带进副本(K2)在真 PostgreSQL 上的镜像。

``tests/test_notebook_copy_image_assets.py``(SQLite)的孪生,吃同一份世界
(``tests/copy_image_asset_cases.py``)、走同一组真路由与同一个拷贝入口。PostgreSQL 的
metadata 是 jsonb:快照读出时由 ``sqlite_compatible_row`` 以默认分隔符编码成文本,
写回时再规范化,所以「原样」在这里按 jsonb 值相等判定,不按字节。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from psycopg import Cursor
from psycopg.types.json import Jsonb

from app.services import notebook_sharing
from tests import copy_image_asset_cases as cases

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_copy_image_assets"),
]


@pytest.fixture
def world(postgres_scope, monkeypatch, tmp_path):
    from app.api import deps
    from app.core.config import get_settings
    from app.main import app

    monkeypatch.setenv("DATABASE_URL", postgres_scope.url)
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    get_settings.cache_clear()
    deps.repository.cache_clear()
    repo = deps.repository()

    def insert(table, values):
        values = dict(values)
        if "metadata" in values:
            values["metadata"] = Jsonb(cases.decoded(values["metadata"]))
        columns = list(values)
        with repo._runtime.database.write() as db:
            db.execute(
                f"INSERT INTO {table} ({','.join(columns)}) "
                f"VALUES ({','.join('%s' for _ in columns)})",
                [values[c] for c in columns],
            )

    try:
        yield cases.build(repo, TestClient(app), insert)
    finally:
        deps.repository.cache_clear()
        get_settings.cache_clear()
        repo.close()


def _elements(world, notebook_id):
    with world.repo._runtime.database.connect() as db:
        rows = db.execute(
            "SELECT e.* FROM source_elements e JOIN sources s ON s.id = e.source_id "
            "WHERE s.notebook_id = %s",
            (notebook_id,),
        ).fetchall()
    return {row["text"]: dict(row) for row in rows}


def _assets(world, notebook_id):
    return cases.asset_rows(world, notebook_id)


def test_copy_remaps_element_asset_id_to_copied_asset(world):
    copy_id = cases.copy(world)
    cases.assert_images_point_into_the_copy(world, copy_id, _elements(world, copy_id))
    stale = world.client.get(
        f"/api/notebooks/{copy_id}/assets/{world.assets['img1']}", headers=world.recipient_h
    )
    assert stale.status_code == 404


def test_copy_asset_rows_follow_copied_source_lifecycle(world):
    copy_id = cases.copy(world)
    cases.assert_copy_asset_source_ids(world, copy_id)
    cases.assert_copy_assets_follow_copy_sources(world, copy_id)


def test_copy_keeps_asset_source_id_that_has_no_mapping(world):
    copy_id = cases.copy(world)
    assert cases.copy_asset_for(world, copy_id, "gone")["source_id"] == cases.GONE_SOURCE
    assert cases.copy_asset_for(world, copy_id, "pasted")["source_id"] is None


def test_copy_leaves_non_image_element_metadata_unchanged(world):
    copy_id = cases.copy(world)
    copied = _elements(world, copy_id)
    original = _elements(world, world.nb)
    for name in world.raw:
        if name in cases.REMAPPED:
            continue
        assert copied[name]["metadata"] == original[name]["metadata"], name
        assert copied[name]["metadata"] == cases.decoded(world.raw[name]), name
    assert copied["nested-asset"]["metadata"]["figure"]["asset_id"] == world.assets["img1"]


def test_copy_leaves_the_original_notebook_untouched(world):
    before = (_elements(world, world.nb), _assets(world, world.nb))
    cases.copy(world)
    assert (_elements(world, world.nb), _assets(world, world.nb)) == before


def _pool_housekeeping(query: str) -> bool:
    """连接池自己的借出探测与归还复位(``PostgresDatabase._restore_session_defaults``)。
    它们由池在借还时机(可能在池的后台线程)执行,条数随连接复用情况浮动,与拷贝无关。"""
    return query in ("", "RESET ALL") or query.startswith(
        "SELECT set_config('statement_timeout'"
    )


def _count_statements(monkeypatch):
    executed: list[str] = []
    real_execute, real_executemany = Cursor.execute, Cursor.executemany

    def execute(self, query, *args, **kwargs):
        if not _pool_housekeeping(str(query)):
            executed.append(str(query))
        return real_execute(self, query, *args, **kwargs)

    def executemany(self, query, *args, **kwargs):
        executed.append(str(query))
        return real_executemany(self, query, *args, **kwargs)

    monkeypatch.setattr(Cursor, "execute", execute)
    monkeypatch.setattr(Cursor, "executemany", executemany)
    return executed


def _legacy_asset_row(data, new_id, source_map):
    data["notebook_id"] = new_id


def test_remap_adds_no_statement_to_the_copy(world, monkeypatch):
    cases.copy(world)  # 预热
    executed = _count_statements(monkeypatch)
    with monkeypatch.context() as legacy:
        legacy.setattr(notebook_sharing, "_remap_element_asset_id", lambda raw, _m: raw)
        legacy.setattr(notebook_sharing, "_remap_copied_asset_row", _legacy_asset_row)
        legacy_copy = cases.copy(world)
    legacy_statements = list(executed)
    executed.clear()
    fixed_copy = cases.copy(world)
    assert executed and len(executed) == len(legacy_statements)
    normalise = lambda sqls: sorted(  # noqa: E731 - 只把新 id 从语句文本里抹掉
        s.replace(legacy_copy, "<nb>").replace(fixed_copy, "<nb>") for s in sqls
    )
    assert normalise(executed) == normalise(legacy_statements)
    legacy_img = _elements(world, legacy_copy)["img-1"]["metadata"]["asset_id"]
    fixed_img = _elements(world, fixed_copy)["img-1"]["metadata"]["asset_id"]
    assert legacy_img == world.assets["img1"] and fixed_img in _assets(world, fixed_copy)
