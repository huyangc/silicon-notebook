"""深拷贝把来源图片带进副本(K2,SQLite 端)。

修复前:副本为每条 ``notebook_assets`` 铸了新 id,却没有改元素 ``metadata.asset_id``,图片
元素仍指向原库的资产——经资产代理请求是 404;资产行的 ``source_id`` 也仍是原库的来源,原库
一删那个来源,副本的图片行与文件被连带删掉,副本删自己的来源反而清不掉。

世界与断言见 ``tests/copy_image_asset_cases.py``;PostgreSQL 孪生在
``tests/postgres/test_notebook_copy_image_assets_pg.py``。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.repositories.sqlite import database as sqlite_database
from app.services import notebook_sharing
from tests import copy_image_asset_cases as cases


@pytest.fixture
def world(tmp_path, monkeypatch):
    from app.api import deps
    from app.core.config import get_settings
    from app.main import app

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    get_settings.cache_clear()
    deps.repository.cache_clear()
    repo = deps.repository()

    def insert(table, values):
        columns = list(values)
        with repo._runtime.database.write() as db:
            db.execute(
                f"INSERT INTO {table} ({','.join(columns)}) "
                f"VALUES ({','.join('?' for _ in columns)})",
                [values[c] for c in columns],
            )

    return cases.build(repo, TestClient(app), insert)


def _elements(world, notebook_id):
    with world.repo._runtime.database.connect() as db:
        rows = db.execute(
            "SELECT e.* FROM source_elements e JOIN sources s ON s.id = e.source_id "
            "WHERE s.notebook_id = ?",
            (notebook_id,),
        ).fetchall()
    return {row["text"]: dict(row) for row in rows}


def _assets(world, notebook_id):
    return cases.asset_rows(world, notebook_id)


def test_copy_remaps_element_asset_id_to_copied_asset(world):
    copy_id = cases.copy(world)
    cases.assert_images_point_into_the_copy(world, copy_id, _elements(world, copy_id))
    # 反证:同一个代理拿原库的资产 id 在副本里是 404(修复前副本里的图片就是这样坏的)。
    stale = world.client.get(
        f"/api/notebooks/{copy_id}/assets/{world.assets['img1']}", headers=world.recipient_h
    )
    assert stale.status_code == 404


def test_copy_asset_rows_follow_copied_source_lifecycle(world):
    copy_id = cases.copy(world)
    cases.assert_copy_asset_source_ids(world, copy_id)
    cases.assert_copy_assets_follow_copy_sources(world, copy_id)


def test_copy_keeps_asset_source_id_that_has_no_mapping(world):
    """来源已不存在的资产行:副本里 source_id 原值不动——不置 NULL(置 NULL 会被孤儿清扫
    连行带文件删掉)。粘贴图本来就是 NULL,保持 NULL。"""
    copy_id = cases.copy(world)
    assert cases.copy_asset_for(world, copy_id, "gone")["source_id"] == cases.GONE_SOURCE
    assert cases.copy_asset_for(world, copy_id, "pasted")["source_id"] is None


def test_copy_leaves_non_image_element_metadata_byte_identical(world):
    """没有可重映射的顶层 asset_id 的元素,副本 metadata 与原库逐字节相同——包括嵌套的
    asset_id、非对象 JSON、不可哈希/不认识/为 null 的 asset_id、值里恰好含子串的情形。"""
    copy_id = cases.copy(world)
    copied = _elements(world, copy_id)
    for name, raw in world.raw.items():
        if name in cases.REMAPPED:
            continue
        assert copied[name]["metadata"] == raw, name
    # 嵌套的 asset_id 只是别人的数据:仍是原库 id,没被改写。
    assert cases.decoded(copied["nested-asset"]["metadata"])["figure"]["asset_id"] == (
        world.assets["img1"]
    )


def test_copy_skips_metadata_that_is_not_json(world):
    """SQLite 的 metadata 是文本,可能不是合法 JSON:含 ``"asset_id"`` 子串也原样复制,拷贝成功。"""
    broken = '{"asset_id": "' + world.assets["img1"] + '"'
    with world.repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO source_elements (id,source_id,element_type,location_label,text,metadata,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("el-k2-broken", world.src_img, "image", "p99", "broken-json", broken, cases.NOW),
        )
    copy_id = cases.copy(world)
    assert _elements(world, copy_id)["broken-json"]["metadata"] == broken


def test_copy_leaves_the_original_notebook_untouched(world):
    before = (_elements(world, world.nb), _assets(world, world.nb))
    cases.copy(world)
    assert (_elements(world, world.nb), _assets(world, world.nb)) == before


def _count_statements(monkeypatch):
    executed: list[str] = []
    cursor = sqlite_database._DiagnosticCursor
    real_execute, real_executemany = cursor.execute, cursor.executemany

    def execute(self, sql, parameters=(), /):
        executed.append(sql)
        return real_execute(self, sql, parameters)

    def executemany(self, sql, seq_of_parameters, /):
        executed.append(sql)
        return real_executemany(self, sql, seq_of_parameters)

    monkeypatch.setattr(cursor, "execute", execute)
    monkeypatch.setattr(cursor, "executemany", executemany)
    return executed


def _legacy_asset_row(data, new_id, source_map):
    data["notebook_id"] = new_id


def test_remap_adds_no_statement_to_the_copy(world, monkeypatch):
    """重映射是纯内存改写:同一本笔记本,修复前(两个 helper 换回旧语义)与修复后的拷贝
    执行的语句条数相同、语句原文的多重集相同。"""
    cases.copy(world)  # 预热:首拷触发的一次性懒初始化不进比较
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
    # 预热之外的两次拷贝确实走了两种语义(否则上面的相等不说明任何事)。
    legacy_img = cases.decoded(_elements(world, legacy_copy)["img-1"]["metadata"])["asset_id"]
    fixed_img = cases.decoded(_elements(world, fixed_copy)["img-1"]["metadata"])["asset_id"]
    assert legacy_img == world.assets["img1"] and fixed_img in _assets(world, fixed_copy)
