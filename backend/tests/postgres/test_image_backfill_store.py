"""`backfill-images` 仓储半的 PostgreSQL 对等（G3 泳道）。

生产后端就是 PostgreSQL，所以这四个方法的 PG 半必须在真库上跑过：SQLite 侧的
`chunks.element_ids` 是 TEXT JSON、PG 是 jsonb，``created_at`` 一侧是 ISO 文本、
另一侧是 ``timestamptz``，``ORDER BY id`` 与 ``ORDER BY id COLLATE "C"`` 也各自
成立——这些差异 SQLite 泳道一条都验不到。
"""
from __future__ import annotations

import json

import pytest

from app.models.notebooks import NotebookCreate
from app.repositories.postgres._store_utils import jsonb, normalize_timestamp


pytestmark = pytest.mark.xdist_group(name="postgres_image_backfill")


@pytest.fixture
def postgres_repository(postgres_settings):
    from app.repositories.postgres.repository import PostgresRepository

    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


def _seed(repository, notebook_id: str) -> str:
    """一个 markdown 来源 + 三条元素 + 一个 chunk 覆盖前两条。"""
    runtime = repository._runtime
    now = normalize_timestamp(runtime.seams.now())
    source_id = "src-md"
    with runtime.database.write() as db:
        db.execute(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,status,parse_status,file_name,"
            "file_path,file_size,file_hash,summary,created_at,updated_at,doc_type,"
            "chunked_at) "
            "VALUES (%s,%s,'design','markdown','extracted','parsed','design.md',"
            "'/tmp/design.md',0,'hash-md','',%s,%s,'textbook',%s)",
            (source_id, notebook_id, now, now, now),
        )
        for ordinal, (element_type, text) in enumerate(
            (
                ("heading", "系统总体设计"),
                ("paragraph", "本章介绍系统的总体设计目标与边界。"),
                ("paragraph", "数据通路由采集、解析、检索三段构成。"),
            ),
            start=1,
        ):
            db.execute(
                "INSERT INTO source_elements "
                "(id,source_id,element_type,location_label,text,metadata,created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    f"el-{source_id}-{ordinal:04d}",
                    source_id,
                    element_type,
                    f"Markdown {element_type} {ordinal}",
                    text,
                    jsonb({"parser": "markdown"}),
                    now,
                ),
            )
        db.execute(
            "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,"
            "element_ids,created_at) VALUES ('ck-1',%s,%s,'chunk body','',%s,%s)",
            (
                notebook_id,
                source_id,
                jsonb([f"el-{source_id}-0001", f"el-{source_id}-0002"]),
                now,
            ),
        )
    return source_id


@pytest.mark.postgres_integration
def test_source_page_selects_markdown_candidates_by_keyset(postgres_repository):
    notebook_id = postgres_repository.create_notebook(NotebookCreate(name="bf")).id
    source_id = _seed(postgres_repository, notebook_id)
    runtime = postgres_repository._runtime
    now = normalize_timestamp(runtime.seams.now())
    with runtime.database.write() as db:
        # 非 markdown 来源与隐藏合成源都不该出现在候选里。
        db.execute(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,status,parse_status,file_name,"
            "file_path,file_size,file_hash,summary,created_at,updated_at,doc_type) "
            "VALUES ('src-pdf',%s,'p','pdf','extracted','parsed','p.pdf','',0,"
            "'h1','',%s,%s,'') ",
            (notebook_id, now, now),
        )
        db.execute(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,status,parse_status,file_name,"
            "file_path,file_size,file_hash,summary,created_at,updated_at,doc_type) "
            "VALUES ('src-mem',%s,'m','memory','extracted','parsed','m.md','',0,"
            "'h2','',%s,%s,'')",
            (notebook_id, now, now),
        )

    maintenance = postgres_repository.maintenance
    page = maintenance.image_backfill_source_page(notebook_id, "", 50)
    assert [row["id"] for row in page] == [source_id]
    assert page[0]["file_name"] == "design.md"
    assert page[0]["file_path"] == "/tmp/design.md"
    # keyset：从最后一个 id 之后继续翻页就没有了。
    assert maintenance.image_backfill_source_page(notebook_id, source_id, 50) == []


@pytest.mark.postgres_integration
def test_keyset_paging_visits_every_markdown_source_exactly_once(postgres_repository):
    """比较键与排序键必须同一个 collation。

    库的默认 collation 不是 `C` 时，裸 `id > %s` 与 `ORDER BY id COLLATE "C"`
    给出的是两种顺序，keyset 翻页会**漏源或重复**——而这两种结局都不报错：漏源
    只是"这批图没补上"，重复只是多做一遍幂等的活。这里用带标点、在
    `C` 与 `en_US.UTF-8` 下排序不同的 id 逐页（LIMIT 1）走一遍，断言每个来源恰好
    出现一次、且顺序就是 C collation 序。"""
    notebook_id = postgres_repository.create_notebook(NotebookCreate(name="bf")).id
    runtime = postgres_repository._runtime
    now = normalize_timestamp(runtime.seams.now())
    # `_`(0x5F) < `a`(0x61) 在 C 下成立，而 en_US.UTF-8 在主级别忽略标点。
    ids = ["src-a_b", "src-ab", "src-a-c", "src-aB"]
    with runtime.database.write() as db:
        for index, source_id in enumerate(ids):
            db.execute(
                "INSERT INTO sources "
                "(id,notebook_id,title,source_type,status,parse_status,file_name,"
                "file_path,file_size,file_hash,summary,created_at,updated_at,doc_type) "
                "VALUES (%s,%s,'d','markdown','extracted','parsed','d.md','',0,%s,"
                "'',%s,%s,'')",
                (source_id, notebook_id, f"h-{index}", now, now),
            )

    seen: list[str] = []
    after = ""
    while True:
        page = postgres_repository.maintenance.image_backfill_source_page(
            notebook_id, after, 1
        )
        if not page:
            break
        seen.append(page[0]["id"])
        after = page[-1]["id"]
        assert len(seen) <= len(ids) + 1, f"keyset 不推进/重复: {seen}"
    assert seen == sorted(ids)  # Python 的 str 序就是 C collation 序


@pytest.mark.postgres_integration
def test_source_state_decodes_jsonb_and_reports_the_element_generation(
    postgres_repository,
):
    notebook_id = postgres_repository.create_notebook(NotebookCreate(name="bf")).id
    source_id = _seed(postgres_repository, notebook_id)

    state = postgres_repository.maintenance.image_backfill_source_state(source_id)
    assert [element["id"] for element in state["elements"]] == [
        f"el-{source_id}-0001",
        f"el-{source_id}-0002",
        f"el-{source_id}-0003",
    ]
    assert state["elements"][0]["metadata"] == {"parser": "markdown"}
    # jsonb 直接还成 list，不是字符串。
    assert state["chunks"] == [
        {
            "id": "ck-1",
            "element_ids": [f"el-{source_id}-0001", f"el-{source_id}-0002"],
        }
    ]
    with postgres_repository._runtime.database.connect() as db:
        newest = db.execute(
            "SELECT MAX(created_at) AS m FROM source_elements WHERE source_id=%s",
            (source_id,),
        ).fetchone()["m"]
    assert state["element_created_at"] == newest


@pytest.mark.postgres_integration
def test_apply_writes_elements_chunk_ids_reverse_rows_and_updated_at(
    postgres_repository,
):
    notebook_id = postgres_repository.create_notebook(NotebookCreate(name="bf")).id
    source_id = _seed(postgres_repository, notebook_id)
    maintenance = postgres_repository.maintenance
    state = maintenance.image_backfill_source_state(source_id)
    stamp = state["element_created_at"]

    with postgres_repository._runtime.database.connect() as db:
        before = db.execute(
            "SELECT updated_at, chunked_at FROM sources WHERE id=%s", (source_id,)
        ).fetchone()
        chunk_text_before = db.execute(
            "SELECT text FROM chunks WHERE id='ck-1'"
        ).fetchone()["text"]

    new_id = f"el-{source_id}-0002-g01"
    maintenance.apply_image_backfill(
        notebook_id,
        source_id,
        [
            {
                "id": new_id,
                "element_type": "image",
                "location_label": "Markdown image 1",
                "text": "图 1 系统总体架构",
                "metadata": {
                    "parser": "image_backfill",
                    "src": "images/aaa.jpg",
                    "asset_id": "asset-1",
                    "caption": "图 1 系统总体架构",
                },
            }
        ],
        {
            "ck-1": [
                f"el-{source_id}-0001",
                f"el-{source_id}-0002",
                new_id,
            ]
        },
        (),
        created_at=stamp,
    )

    with postgres_repository._runtime.database.connect() as db:
        element = db.execute(
            "SELECT element_type, text, metadata, created_at FROM source_elements "
            "WHERE id=%s",
            (new_id,),
        ).fetchone()
        chunk = db.execute(
            "SELECT text, element_ids FROM chunks WHERE id='ck-1'"
        ).fetchone()
        reverse = db.execute(
            "SELECT element_id FROM chunk_elements WHERE notebook_id=%s "
            "ORDER BY element_id",
            (notebook_id,),
        ).fetchall()
        after = db.execute(
            "SELECT updated_at, chunked_at FROM sources WHERE id=%s", (source_id,)
        ).fetchone()

    assert element["element_type"] == "image"
    assert element["text"] == "图 1 系统总体架构"
    metadata = element["metadata"]
    if isinstance(metadata, str):  # pragma: no cover - jsonb 正常还成 dict
        metadata = json.loads(metadata)
    assert metadata["asset_id"] == "asset-1"
    # 元素批次戳共享：详情分页的 (created_at,id) 排序与命令目录代次都不漂。
    assert element["created_at"] == stamp

    assert chunk["text"] == chunk_text_before  # chunk 正文逐字节不变
    assert chunk["element_ids"][-1] == new_id  # 只在尾部追加
    assert [row["element_id"] for row in reverse] == sorted(
        [f"el-{source_id}-0001", f"el-{source_id}-0002", new_id]
    )
    assert after["updated_at"] > before["updated_at"]
    assert after["chunked_at"] == before["chunked_at"]  # 不碰完成标记


@pytest.mark.postgres_integration
def test_reverse_row_insert_is_idempotent_across_reruns(postgres_repository):
    """`ON CONFLICT DO NOTHING`：与离线 `backfill-chunk-elements` 互相幂等。"""
    notebook_id = postgres_repository.create_notebook(NotebookCreate(name="bf")).id
    source_id = _seed(postgres_repository, notebook_id)
    maintenance = postgres_repository.maintenance
    stamp = maintenance.image_backfill_source_state(source_id)["element_created_at"]
    # 两轮都送同样这两条老元素：第二轮的反查行整批与第一轮重叠，没有
    # `ON CONFLICT DO NOTHING` 就是一次主键冲突（整个事务回滚），而不是"多数了
    # 几行"——所以这条用例同时钉住幂等与"不炸"。
    element_ids = [f"el-{source_id}-0001", f"el-{source_id}-0002"]

    for index in range(2):
        maintenance.apply_image_backfill(
            notebook_id,
            source_id,
            [
                {
                    "id": f"el-{source_id}-0002-g{index + 1:03d}",
                    "element_type": "image",
                    "location_label": f"Markdown image {index + 1}",
                    "text": "",
                    "metadata": {"asset_id": f"asset-{index}"},
                }
            ],
            {"ck-1": element_ids + [f"el-{source_id}-0002-g{index + 1:03d}"]},
            (),
            created_at=stamp,
        )

    with postgres_repository._runtime.database.connect() as db:
        rows = db.execute(
            "SELECT COUNT(*) AS c FROM chunk_elements WHERE notebook_id=%s",
            (notebook_id,),
        ).fetchone()["c"]
    # 两轮各送 3 条（两条老的重复），主键去重后只剩 4 条不同的。
    assert rows == 4


@pytest.mark.postgres_integration
def test_metadata_updates_enrich_in_place_without_touching_anything_else(
    postgres_repository,
):
    """就地补齐（`image_backfill.EnrichedImage`）的 PG 半：只改 ``metadata``，
    ``text``/``created_at``/``location_label`` 一律不动，jsonb 写入照常还成 dict。"""
    notebook_id = postgres_repository.create_notebook(NotebookCreate(name="bf")).id
    source_id = _seed(postgres_repository, notebook_id)
    runtime = postgres_repository._runtime
    maintenance = postgres_repository.maintenance
    stamp = maintenance.image_backfill_source_state(source_id)["element_created_at"]
    image_id = f"el-{source_id}-0004"
    with runtime.database.write() as db:
        db.execute(
            "INSERT INTO source_elements "
            "(id,source_id,element_type,location_label,text,metadata,created_at) "
            "VALUES (%s,%s,'image','Markdown image 1','图 1 系统总体架构',%s,%s)",
            (
                image_id,
                source_id,
                jsonb({"parser": "markdown", "src": "images/aaa.jpg"}),
                stamp,
            ),
        )
    with runtime.database.connect() as db:
        before = db.execute(
            "SELECT text, location_label, created_at FROM source_elements WHERE id=%s",
            (image_id,),
        ).fetchone()

    maintenance.apply_image_backfill(
        notebook_id,
        source_id,
        [],
        {},
        [
            {
                "id": image_id,
                "metadata": {
                    "parser": "markdown",
                    "src": "images/aaa.jpg",
                    "asset_id": "asset-9",
                },
            }
        ],
        created_at=stamp,
    )

    with runtime.database.connect() as db:
        after = db.execute(
            "SELECT text, location_label, created_at, metadata FROM source_elements "
            "WHERE id=%s",
            (image_id,),
        ).fetchone()
        others = db.execute(
            "SELECT COUNT(*) AS c FROM source_elements WHERE source_id=%s "
            "AND element_type='image'",
            (source_id,),
        ).fetchone()["c"]
        updated_at = db.execute(
            "SELECT updated_at FROM sources WHERE id=%s", (source_id,)
        ).fetchone()["updated_at"]

    assert others == 1  # 没有多出第二条 image 行
    assert after["text"] == before["text"]
    assert after["location_label"] == before["location_label"]
    assert after["created_at"] == before["created_at"]
    metadata = after["metadata"]
    if isinstance(metadata, str):  # pragma: no cover - jsonb 正常还成 dict
        metadata = json.loads(metadata)
    assert metadata == {
        "parser": "markdown",
        "src": "images/aaa.jpg",
        "asset_id": "asset-9",
    }
    assert updated_at is not None  # 纯补齐同样推进变更信号


@pytest.mark.postgres_integration
def test_discard_assets_removes_only_the_named_rows(postgres_repository):
    notebook_id = postgres_repository.create_notebook(NotebookCreate(name="bf")).id
    source_id = _seed(postgres_repository, notebook_id)
    keep = postgres_repository.insert_notebook_asset(
        notebook_id, "keep.jpg", "image/jpeg", 3, "u", source_id=source_id
    )
    drop = postgres_repository.insert_notebook_asset(
        notebook_id, "drop.jpg", "image/jpeg", 3, "u", source_id=source_id
    )

    removed = postgres_repository.maintenance.image_backfill_discard_assets([drop])
    assert [row["id"] for row in removed] == [drop]
    assert isinstance(removed[0]["created_at"], str)  # SQLite 兼容形状
    assert postgres_repository.get_notebook_asset(drop) is None
    assert postgres_repository.get_notebook_asset(keep) is not None
    assert postgres_repository.maintenance.image_backfill_discard_assets([]) == []
