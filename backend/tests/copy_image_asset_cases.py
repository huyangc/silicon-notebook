"""深拷贝里来源图片的资产重映射(K2)——**两端吃同一份世界**。

``tests/test_notebook_copy_image_assets.py``(SQLite)与
``tests/postgres/test_notebook_copy_image_assets_pg.py``(PostgreSQL)各自 import 本模块,
各自只写「把行插进库」「把一本笔记本的元素读出来」两个薄适配器;世界、动作与断言都在这里。

世界(owner 与 recipient 经真路由注册、登录;笔记本由 owner 经真路由创建):

* 来源 ``src-img``:两张经 ``AssetService.save_source_image`` 落库落盘的来源图片
  (``notebook_assets.source_id`` = 该来源),两个顶层 ``asset_id`` 指向它们的图片元素;
  以及一组**不该被改动**的元素 metadata 形态:普通段落(紧凑分隔符 + 中文)、空对象、
  嵌套的 ``asset_id``、非对象 JSON、不可哈希的 ``asset_id``、不在本库的 ``asset_id``、
  值里恰好含 ``"asset_id"`` 子串、``asset_id`` 为 null。
* 来源 ``src-txt``:一个普通段落元素。
* 一张粘贴图(``source_id`` 为 NULL)与一行 ``source_id`` 指向**已不存在的来源**的资产行。

不是测试模块(没有 ``test_`` 前缀),pytest 不会收集它。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.services.knowhow.assets import AssetService

NOW = "2026-10-12T00:00:00+00:00"
PNG = b"\x89PNG\r\n\x1a\n" + b"k2-copy-image-asset" * 8
PNG_2 = b"\x89PNG\r\n\x1a\n" + b"k2-second-figure" * 8
GONE_SOURCE = "src-gone-k2"

#: 元素名 -> metadata 原文模板(``<img1>``/``<img2>`` 换成原库资产 id)。元素的 ``text``
#: 就是元素名,用来在副本里找回对应元素。
REMAPPED = {
    "img-1": ('{"asset_id": "<img1>", "caption": "图一 波形", "page": 2}', "img1"),
    "img-2": ('{"asset_id":"<img2>"}', "img2"),
}
UNCHANGED = {
    "para-compact": '{"page":3,  "标签":"段落"}',
    "para-empty": "{}",
    "nested-asset": '{"figure": {"asset_id": "<img1>"}}',
    "non-object": '["asset_id", "<img1>"]',
    "unhashable": '{"asset_id": ["<img1>"]}',
    # 下面三条故意用紧凑分隔符:命中子串、解析了、但没有值可换——SQLite 上必须逐字节不动,
    # 「解析过就重新序列化」会把它们改成默认分隔符。
    "unknown-asset": '{"asset_id":"asset-not-in-this-notebook"}',
    "substring-only": '{"label":"asset_id"}',
    "null-asset": '{"asset_id":null}',
}
TXT_ELEMENT = ("txt-para", '{"page": 1}')


@dataclass
class World:
    client: Any
    repo: Any
    owner_h: dict
    recipient_h: dict
    owner_id: str
    recipient_id: str
    nb: str
    src_img: str
    src_txt: str
    assets: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)


def _login(client, username: str) -> tuple[dict, str]:
    password = "pw123456"
    client.post("/api/auth/register", json={"username": username, "password": password})
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    return headers, client.get("/api/me", headers=headers).json()["id"]


def build(repo, client, insert: Callable[[str, dict], None]) -> World:
    """``insert(table, values)``:``values["metadata"]`` 是原文文本,适配器自己决定怎么存。"""
    owner_h, owner_id = _login(client, "a00000921")
    recipient_h, recipient_id = _login(client, "a00000922")
    created = client.post("/api/notebooks", json={"name": "K2 原库"}, headers=owner_h)
    assert created.status_code == 200, created.text
    nb = created.json()["id"]
    world = World(
        client=client, repo=repo, owner_h=owner_h, recipient_h=recipient_h,
        owner_id=owner_id, recipient_id=recipient_id, nb=nb,
        src_img="src-k2-img", src_txt="src-k2-txt",
    )
    for sid, title in ((world.src_img, "带图来源"), (world.src_txt, "纯文本来源")):
        insert("sources", {
            "id": sid, "notebook_id": nb, "title": title, "source_type": "document",
            "status": "ready", "parse_status": "ready", "created_at": NOW, "updated_at": NOW,
        })
    service = AssetService(repo)
    world.assets["img1"] = service.save_source_image(
        nb, world.src_img, "fig1.png", "image/png", PNG, owner_id
    )["id"]
    world.assets["img2"] = service.save_source_image(
        nb, world.src_img, "fig2.png", "image/png", PNG_2, owner_id
    )["id"]
    world.assets["pasted"] = service.save(nb, "paste.png", "image/png", PNG, owner_id)["id"]
    world.assets["gone"] = repo._runtime.knowhow_store.insert_notebook_asset(
        nb, "orphan.png", "image/png", len(PNG), owner_id, source_id=GONE_SOURCE
    )

    def fill(template: str) -> str:
        return template.replace("<img1>", world.assets["img1"]).replace(
            "<img2>", world.assets["img2"]
        )

    elements = {name: fill(tpl) for name, (tpl, _asset) in REMAPPED.items()}
    elements.update({name: fill(tpl) for name, tpl in UNCHANGED.items()})
    for position, (name, raw) in enumerate(elements.items()):
        insert("source_elements", {
            "id": f"el-k2-{position}", "source_id": world.src_img,
            "element_type": "image" if name.startswith("img-") else "paragraph",
            "location_label": f"p{position}", "text": name, "metadata": raw,
            "created_at": NOW,
        })
    insert("source_elements", {
        "id": "el-k2-txt", "source_id": world.src_txt, "element_type": "paragraph",
        "location_label": "p1", "text": TXT_ELEMENT[0], "metadata": TXT_ELEMENT[1],
        "created_at": NOW,
    })
    elements[TXT_ELEMENT[0]] = TXT_ELEMENT[1]
    world.raw = elements
    return world


def copy(world: World) -> str:
    """新主人拿到一本深拷贝(与生产路由同一个服务入口)。"""
    return world.repo._runtime.sharing.copy_notebook(
        world.nb, new_owner_id=world.recipient_id
    ).id


def asset_rows(world: World, notebook_id: str) -> dict[str, dict]:
    with world.repo._runtime.database.connect() as db:
        rows = db.execute(
            "SELECT * FROM notebook_assets WHERE notebook_id = "
            + ("%s" if _is_postgres(world) else "?"),
            (notebook_id,),
        ).fetchall()
    return {row["id"]: dict(row) for row in rows}


def copy_asset_for(world: World, copy_id: str, key: str) -> dict:
    """副本里与原库资产 ``key`` 对应的那一行(按原库行的 filename 找回,本世界里唯一)。"""
    original = asset_rows(world, world.nb)[world.assets[key]]
    matches = [
        row for row in asset_rows(world, copy_id).values()
        if row["filename"] == original["filename"]
    ]
    assert len(matches) == 1, matches
    return matches[0]


def asset_path(world: World, row: dict) -> Path:
    return AssetService(world.repo).path_for(row)


def source_id_by_title(world: World, notebook_id: str, title: str) -> str:
    with world.repo._runtime.database.connect() as db:
        p = "%s" if _is_postgres(world) else "?"
        row = db.execute(
            f"SELECT id FROM sources WHERE notebook_id = {p} AND title = {p}",
            (notebook_id, title),
        ).fetchone()
    return row["id"]


def decoded(metadata: Any) -> Any:
    return json.loads(metadata) if isinstance(metadata, str) else metadata


def _is_postgres(world: World) -> bool:
    return type(world.repo._runtime.database).__module__.endswith("postgres.database")


# ------------------------------------------------------------------ 断言


def assert_images_point_into_the_copy(world: World, copy_id: str, elements: dict) -> None:
    """副本的图片元素指向副本自己的资产行,文件在,新主人经资产代理拿到原字节且走长缓存。"""
    copy_assets = asset_rows(world, copy_id)
    for name, (_tpl, key) in REMAPPED.items():
        metadata = decoded(elements[name]["metadata"])
        original = decoded(world.raw[name])
        new_asset_id = metadata["asset_id"]
        assert new_asset_id != world.assets[key], name
        assert new_asset_id in copy_assets, (name, new_asset_id, sorted(copy_assets))
        # 只有 asset_id 的值变了,其余键原样。
        assert {**metadata, "asset_id": original["asset_id"]} == original, name
        row = copy_assets[new_asset_id]
        assert row["notebook_id"] == copy_id
        assert asset_path(world, row).is_file(), name
        got = world.client.get(
            f"/api/notebooks/{copy_id}/assets/{new_asset_id}", headers=world.recipient_h
        )
        assert got.status_code == 200, (name, got.status_code, got.text)
        assert got.content == (PNG if key == "img1" else PNG_2)
        assert got.headers["cache-control"] == "private, max-age=86400"


def assert_copy_asset_source_ids(world: World, copy_id: str) -> None:
    """来源图片资产行挂到副本自己的来源上;粘贴图仍是 NULL;来源已不存在的那行原值不动。"""
    copy_src_img = source_id_by_title(world, copy_id, "带图来源")
    for key in ("img1", "img2"):
        assert copy_asset_for(world, copy_id, key)["source_id"] == copy_src_img, key
    assert copy_asset_for(world, copy_id, "pasted")["source_id"] is None
    assert copy_asset_for(world, copy_id, "gone")["source_id"] == GONE_SOURCE


def delete_source(world: World, source_id: str, headers: dict) -> None:
    resp = world.client.delete(f"/api/sources/{source_id}", headers=headers)
    assert resp.status_code == 204, resp.text


def assert_copy_assets_follow_copy_sources(world: World, copy_id: str) -> None:
    """删原库的来源不再连带删副本的图片;删副本自己的来源才清副本的图片行与文件。"""
    copied = {key: copy_asset_for(world, copy_id, key) for key in ("img1", "img2")}
    delete_source(world, world.src_img, world.owner_h)
    assert world.assets["img1"] not in asset_rows(world, world.nb)  # 原库自己的确实删了
    still = asset_rows(world, copy_id)
    for key, row in copied.items():
        assert row["id"] in still, key
        assert asset_path(world, row).is_file(), key
    delete_source(world, source_id_by_title(world, copy_id, "带图来源"), world.recipient_h)
    after = asset_rows(world, copy_id)
    for key, row in copied.items():
        assert row["id"] not in after, key
        assert not asset_path(world, row).exists(), key
    # 与来源无关的两行(粘贴图、来源早已不存在的行)不受影响。
    assert copy_asset_for(world, copy_id, "pasted")
    assert copy_asset_for(world, copy_id, "gone")
