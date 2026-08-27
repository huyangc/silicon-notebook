"""`batch_ingest backfill-images` 的端到端与仓储行为。

用真实 ingestion 路径造一个小 markdown 来源（真元素、真 chunk、真 chunk 向量），
再用一棵假 MinerU output 树跑回填，逐条钉住红线：chunk id 集合与每个 chunk 的
text 逐字节不变、chunk_embeddings 一行不动、KG 相关表零变化、反查行与
`sources.updated_at` 同事务、显示链路（`image_asset_rows`）真取得到新图。
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from app.core.config import Settings
from app.services import batch_ingest as bi
from app.services import image_backfill_phase as phase
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import RecordingModelProvider


MD = """# 系统总体设计

本章介绍系统的总体设计目标与边界，并给出关键取舍。

![](images/aaa.jpg)

图 1 系统总体架构

数据通路由采集、解析、检索三段构成，各段之间以持久化产物解耦。

![](images/bbb.jpg)

以上为主要模块的职责划分，下一章展开每个模块的内部结构。

![](images/missing.jpg)
"""


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    embedder = FakeEmbedder(dim=16)
    provider = RecordingModelProvider(
        embedding_clients={
            workload: embedder
            for workload in (
                "retrieval_query_embedding",
                "source_element_embedding",
                "chunk_embedding",
                "knowledge_object_embedding",
                "relation_embedding",
                "memory_embedding",
                "knowhow_embedding",
            )
        }
    )
    repository = SQLiteRepository(
        Settings(model_services_config=""), model_provider=provider
    )

    @contextmanager
    def _open_test_repository(_settings, **_kwargs):
        yield repository

    monkeypatch.setattr(bi, "open_maintenance_cli_repository", _open_test_repository)
    return repository


@pytest.fixture
def outputs(tmp_path):
    """假 MinerU output 树：`output/<session>/<pdf>/auto/images/<name>`。"""
    images = tmp_path / "output" / "sess" / "doc" / "auto" / "images"
    images.mkdir(parents=True)
    # 最小合法 PNG（AssetService 只校验 mime 与尺寸，不解码像素）。
    (images / "aaa.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"a" * 64)
    (images / "bbb.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"b" * 64)
    return [tmp_path / "output"]


@pytest.fixture
def seeded(repo, tmp_path):
    """真实摄取一个 markdown 来源，返回 (notebook_id, source_id)。"""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "design.md").write_text(MD, encoding="utf-8")
    notebook_id = bi.ensure_notebook(repo, None, "nb")
    bi.run_ingest(repo, notebook_id, bi.iter_files(docs), workers=1)
    with repo._connect() as db:
        source_id = db.execute(
            "SELECT id FROM sources WHERE notebook_id=?", (notebook_id,)
        ).fetchone()["id"]
    _seed_kg(repo, notebook_id, source_id)
    return notebook_id, source_id


def _seed_kg(repo, notebook_id: str, source_id: str) -> None:
    """给 KG 相关表各放一行真内容。

    "零 KG 变动"是本阶段的红线之一，而 `after["kg"] == before["kg"]` 在两边都是
    `{...: 0}` 时是**空转**的：把断言删掉、或者让阶段真的去写 KG，用例都照样绿。
    有了这两行，那条断言才在比较实际内容。"""
    stamp = "2020-01-01T00:00:00+00:00"
    with repo._connect() as db:
        db.execute(
            "INSERT INTO knowledge_objects "
            "(id, notebook_id, object_type, status, payload, evidence, source_id, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "ko-backfill-guard",
                notebook_id,
                "concept",
                "approved",
                json.dumps({"name": "回填守卫用的概念"}),
                json.dumps([]),
                source_id,
                stamp,
                stamp,
            ),
        )
        db.execute(
            "INSERT INTO extraction_runs "
            "(id, notebook_id, source_id, run_type, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                "run-backfill-guard",
                notebook_id,
                source_id,
                "kg",
                "completed",
                stamp,
                stamp,
            ),
        )
        db.commit()


def _snapshot(repo, notebook_id: str) -> dict:
    with repo._connect() as db:
        chunks = {
            row["id"]: row["text"]
            for row in db.execute(
                "SELECT id, text FROM chunks WHERE notebook_id=?", (notebook_id,)
            ).fetchall()
        }
        embeddings = {
            row["chunk_id"]: (row["created_at"], bytes(row["vector"]))
            for row in db.execute(
                "SELECT chunk_id, created_at, vector FROM chunk_embeddings "
                "WHERE notebook_id=?",
                (notebook_id,),
            ).fetchall()
        }
        kg = {
            table: db.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
            for table in ("knowledge_objects", "knowledge_relations", "extraction_runs")
        }
        elements = {
            row["id"]: (row["element_type"], row["text"], row["created_at"])
            for row in db.execute(
                "SELECT e.id, e.element_type, e.text, e.created_at FROM source_elements e "
                "JOIN sources s ON s.id = e.source_id WHERE s.notebook_id=?",
                (notebook_id,),
            ).fetchall()
        }
    return {
        "chunks": chunks,
        "embeddings": embeddings,
        "kg": kg,
        "elements": elements,
    }


def _run(repo, notebook_id, outputs, **kwargs):
    return phase.run_backfill_images(
        repo, notebook_id, mineru_outputs=outputs, **kwargs
    )


def _rows(repo, sql, *params):
    with repo._connect() as db:
        return [dict(row) for row in db.execute(sql, params).fetchall()]


# ------------------------------------------------------------------ 红线

def test_backfill_leaves_chunk_ids_text_and_vectors_byte_identical(
    repo, seeded, outputs
):
    notebook_id, source_id = seeded
    before = _snapshot(repo, notebook_id)
    result = _run(repo, notebook_id, outputs)

    assert result["images_inserted"] == 2
    after = _snapshot(repo, notebook_id)
    assert after["chunks"] == before["chunks"]  # id 集合与 text 逐字节
    assert after["embeddings"] == before["embeddings"]
    assert after["kg"] == before["kg"]
    # 既有元素一行都没被改写（只插入）。
    for element_id, value in before["elements"].items():
        assert after["elements"][element_id] == value


def test_missing_image_is_accounted_and_the_rest_still_land(repo, seeded, outputs):
    notebook_id, _ = seeded
    result = _run(repo, notebook_id, outputs)
    assert result["skipped"].get("image_not_found") == 1
    assert result["images_inserted"] == 2


def test_new_elements_sort_after_their_anchor_and_share_the_element_batch_stamp(
    repo, seeded, outputs
):
    notebook_id, source_id = seeded
    stamps_before = {
        row["created_at"]
        for row in _rows(
            repo, "SELECT created_at FROM source_elements WHERE source_id=?", source_id
        )
    }
    _run(repo, notebook_id, outputs)
    rows = _rows(
        repo,
        "SELECT id, element_type, created_at, metadata FROM source_elements "
        "WHERE source_id=? ORDER BY id",
        source_id,
    )
    images = [row for row in rows if row["element_type"] == "image"]
    assert len(images) == 2
    assert {row["created_at"] for row in rows} == stamps_before
    for image in images:
        anchor, _, suffix = image["id"].rpartition("-g")
        assert suffix.isdigit()
        # 锚点确实是该来源的既有元素，且新 id 紧跟其后。
        ids = [row["id"] for row in rows]
        assert anchor in ids
        assert ids.index(image["id"]) == ids.index(anchor) + 1


def test_metadata_uses_the_parse_path_spelling_plus_src_for_idempotency(
    repo, seeded, outputs
):
    notebook_id, source_id = seeded
    _run(repo, notebook_id, outputs)
    rows = _rows(
        repo,
        "SELECT id, location_label, text, metadata FROM source_elements "
        "WHERE source_id=? AND element_type='image' ORDER BY id",
        source_id,
    )
    first = json.loads(rows[0]["metadata"])
    assert first["parser"] == "image_backfill"
    assert first["src"] == "images/aaa.jpg"
    assert first["asset_id"]
    assert first["caption"] == "图 1 系统总体架构"
    assert rows[0]["text"] == "图 1 系统总体架构"
    assert rows[0]["location_label"] == "Markdown image 1"
    # 第二张图没有 Figure/表 形态的相邻行，也没有 alt：图注留空，显示不依赖它。
    assert "caption" not in json.loads(rows[1]["metadata"])
    assert rows[1]["text"] == ""


def test_chunks_gain_the_new_element_ids_at_the_tail(repo, seeded, outputs):
    notebook_id, source_id = seeded
    before = {
        row["id"]: json.loads(row["element_ids"])
        for row in _rows(
            repo, "SELECT id, element_ids FROM chunks WHERE source_id=?", source_id
        )
    }
    _run(repo, notebook_id, outputs)
    after = {
        row["id"]: json.loads(row["element_ids"])
        for row in _rows(
            repo, "SELECT id, element_ids FROM chunks WHERE source_id=?", source_id
        )
    }
    assert set(after) == set(before)
    added = []
    for chunk_id, element_ids in after.items():
        assert element_ids[: len(before[chunk_id])] == before[chunk_id]  # 只在尾部追加
        added.extend(element_ids[len(before[chunk_id]) :])
    assert len(added) == 2
    assert all("-g" in element_id for element_id in added)


def test_reverse_rows_land_in_the_same_write_as_the_chunk_update(
    repo, seeded, outputs
):
    """v46 红线：改 `chunks.element_ids` 也是 chunk 写路径，反查行必须同事务。"""
    notebook_id, source_id = seeded
    _run(repo, notebook_id, outputs)
    new_ids = [
        row["id"]
        for row in _rows(
            repo,
            "SELECT id FROM source_elements WHERE source_id=? AND element_type='image'",
            source_id,
        )
    ]
    assert new_ids
    reverse = {
        row["element_id"]: row["chunk_id"]
        for row in _rows(
            repo,
            "SELECT element_id, chunk_id FROM chunk_elements WHERE notebook_id=?",
            notebook_id,
        )
    }
    for element_id in new_ids:
        assert element_id in reverse
        stored = json.loads(
            _rows(
                repo, "SELECT element_ids FROM chunks WHERE id=?", reverse[element_id]
            )[0]["element_ids"]
        )
        assert element_id in stored


def test_sources_updated_at_advances_with_the_element_generation(
    repo, seeded, outputs
):
    notebook_id, source_id = seeded
    before = _rows(repo, "SELECT updated_at FROM sources WHERE id=?", source_id)[0]
    _run(repo, notebook_id, outputs)
    after = _rows(repo, "SELECT updated_at FROM sources WHERE id=?", source_id)[0]
    assert after["updated_at"] > before["updated_at"]


def test_chunked_at_is_untouched_so_the_source_is_not_flagged_for_reparse(
    repo, seeded, outputs
):
    """体检 H3 的判据是 ``chunked_at IS NULL``、H2 的是"零元素"。本阶段不碰
    `chunked_at` 且只增元素，所以这批来源不会被判成需要重新解析/重新分块。"""
    notebook_id, source_id = seeded
    before = _rows(repo, "SELECT chunked_at FROM sources WHERE id=?", source_id)[0]
    _run(repo, notebook_id, outputs)
    after = _rows(repo, "SELECT chunked_at FROM sources WHERE id=?", source_id)[0]
    assert after["chunked_at"] == before["chunked_at"]
    assert after["chunked_at"] is not None
    with repo._connect() as db:
        missing_chunks = repo._runtime.queries.sources_missing_chunks(db, notebook_id)
        without_elements = repo._runtime.queries.sources_without_elements(
            db, notebook_id
        )
    assert missing_chunks == set()
    assert without_elements == set()


# ------------------------------------------------------------------ 显示链路

def test_citation_image_path_can_reach_the_backfilled_images(repo, seeded, outputs):
    """`image_asset_rows` 是答案带图那次实时点查的取数口；准入只有
    ``element_type='image'`` + ``metadata.asset_id`` 非空两条。"""
    from app.services.evidence_context import _citation_image

    notebook_id, source_id = seeded
    _run(repo, notebook_id, outputs)
    chunk_element_ids: list[str] = []
    for row in _rows(
        repo, "SELECT element_ids FROM chunks WHERE source_id=?", source_id
    ):
        chunk_element_ids.extend(json.loads(row["element_ids"]))

    rows = repo._runtime.source_store.image_asset_rows(chunk_element_ids)
    assert len(rows) == 2
    images = [_citation_image(element_id, metadata) for element_id, metadata in rows]
    assert all(image is not None for image in images)
    assert all(image.asset_id for image in images)
    # 资产文件真的落盘了。
    for element_id, metadata in rows:
        asset = repo.get_notebook_asset(json.loads(metadata)["asset_id"])
        assert asset is not None
        assert (
            Path(repo.storage_dir) / "assets" / notebook_id / f"{asset['id']}.jpg"
        ).is_file()


# ------------------------------------------------------------------ 幂等/开关

def test_rerun_is_a_no_op(repo, seeded, outputs):
    notebook_id, _ = seeded
    _run(repo, notebook_id, outputs)
    before = _snapshot(repo, notebook_id)
    assets_before = _rows(repo, "SELECT id FROM notebook_assets")

    result = _run(repo, notebook_id, outputs)
    assert result["images_inserted"] == 0
    assert result["skipped"].get("already_backfilled") == 2
    assert _snapshot(repo, notebook_id) == before
    assert _rows(repo, "SELECT id FROM notebook_assets") == assets_before


def test_late_found_image_is_added_without_touching_the_earlier_ones(
    repo, seeded, outputs, tmp_path
):
    notebook_id, source_id = seeded
    _run(repo, notebook_id, outputs)
    before = _snapshot(repo, notebook_id)

    (outputs[0] / "sess" / "doc" / "auto" / "images" / "missing.jpg").write_bytes(
        b"\xff\xd8\xff\xe0" + b"c" * 64
    )
    result = _run(repo, notebook_id, outputs)
    assert result["images_inserted"] == 1

    after = _snapshot(repo, notebook_id)
    assert after["chunks"] == before["chunks"]
    assert after["embeddings"] == before["embeddings"]
    for element_id, value in before["elements"].items():
        assert after["elements"][element_id] == value
    assert len(after["elements"]) == len(before["elements"]) + 1


def test_dry_run_writes_nothing(repo, seeded, outputs):
    notebook_id, _ = seeded
    before = _snapshot(repo, notebook_id)
    result = _run(repo, notebook_id, outputs, dry_run=True)
    assert result["images_inserted"] == 0
    assert result["sources_scanned"] == 1
    assert _snapshot(repo, notebook_id) == before
    assert _rows(repo, "SELECT id FROM notebook_assets") == []


def test_disabled_image_storage_refuses_to_run(repo, seeded, outputs, monkeypatch):
    notebook_id, _ = seeded
    monkeypatch.setattr(repo.settings, "mineru_return_images", False)
    with pytest.raises(phase.ImageBackfillDisabled):
        _run(repo, notebook_id, outputs)
    assert _rows(repo, "SELECT id FROM notebook_assets") == []


def test_per_source_cap_truncates_and_is_reported(repo, seeded, outputs, monkeypatch):
    notebook_id, _ = seeded
    monkeypatch.setattr(repo.settings, "mineru_max_images_per_source", 1)
    result = _run(repo, notebook_id, outputs)
    assert result["images_inserted"] == 1
    assert result["skipped"].get("per_source_cap") == 1


def test_oversized_images_never_reach_the_asset_store(repo, seeded, outputs, monkeypatch):
    notebook_id, _ = seeded
    monkeypatch.setattr(repo.settings, "mineru_max_image_bytes", 8)
    result = _run(repo, notebook_id, outputs)
    assert result["images_inserted"] == 0
    assert result["skipped"].get("image_too_large") == 2
    assert _rows(repo, "SELECT id FROM notebook_assets") == []


# ------------------------------------------------------------------ 隔离/回滚

def test_a_failed_write_transaction_rolls_back_this_source_s_assets(
    repo, seeded, outputs, monkeypatch
):
    notebook_id, source_id = seeded
    before = _snapshot(repo, notebook_id)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("write failed")

    monkeypatch.setattr(
        repo.maintenance, "apply_image_backfill", _boom, raising=True
    )
    result = _run(repo, notebook_id, outputs)

    assert result["sources_failed"] == 1
    assert result["images_inserted"] == 0
    assert _snapshot(repo, notebook_id) == before
    # 资产行与磁盘文件都不留孤儿（sweep_orphan_assets 明确不扫 source_id 非空
    # 的行，留下就永远没人回收）。
    assert _rows(repo, "SELECT id FROM notebook_assets") == []
    assets_dir = Path(repo.storage_dir) / "assets" / notebook_id
    assert not assets_dir.exists() or list(assets_dir.iterdir()) == []


def test_a_repo_root_relative_file_path_reads_from_any_working_directory(
    repo, seeded, outputs, tmp_path, monkeypatch
):
    """历史来源的 `sources.file_path` 可以是**仓库根相对**路径，而这条离线命令从
    任意 CWD 启动都合法。裸 `Path(file_path)` 会按进程 CWD 解析，于是整批历史来源
    被误报成 `file_unreadable`——看起来像"文件都没了"。解析必须走产品统一的那条
    （`SourceFileStore.resolve_path`：绝对原样、相对按仓库根）。"""
    notebook_id, source_id = seeded
    absolute = _rows(repo, "SELECT file_path FROM sources WHERE id=?", source_id)[0][
        "file_path"
    ]
    # 用例里的存储目录在 tmp_path 下，所以把解析根指到那儿，好让 file_path 能写成
    # 一个真正的相对路径（`resolve_path` 的规则本身不变：绝对原样、相对按根）。
    monkeypatch.setattr(repo._runtime.database, "root_dir", tmp_path)
    relative = str(Path(absolute).relative_to(tmp_path))
    assert not Path(relative).is_absolute()
    with repo._connect() as db:
        db.execute(
            "UPDATE sources SET file_path=? WHERE id=?", (relative, source_id)
        )
        db.commit()

    # 从一个**不是**解析根的 CWD 跑：裸 `Path(file_path)` 在这里必然读不到。
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert not (elsewhere / relative).exists(), "前置条件：裸 CWD 解析必须读不到"

    result = _run(repo, notebook_id, outputs)
    assert result["skipped"].get("file_unreadable") is None
    assert result["images_inserted"] == 2


# ------------------------------------------------------- 并发重解析（CAS）

def _reparse_in_place(repo, notebook_id, source_id):
    """模拟一次并发重新解析：换掉该来源的全部元素与 chunk（新 id、新 created_at）。

    这正是活服务上 `replace_elements` + 重新分块做的事——SQLite 侧的 maintenance
    CLI 没有停服 preflight，所以它可以恰好落在本阶段的 plan 与 apply 之间。"""
    with repo._connect() as db:
        db.execute("DELETE FROM chunk_elements WHERE notebook_id=?", (notebook_id,))
        db.execute("DELETE FROM chunks WHERE source_id=?", (source_id,))
        db.execute("DELETE FROM source_elements WHERE source_id=?", (source_id,))
        db.execute(
            "INSERT INTO source_elements "
            "(id, source_id, element_type, location_label, text, metadata, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                f"el-{source_id}-9001",
                source_id,
                "paragraph",
                "Markdown paragraph 1",
                "重新解析之后的全新正文。",
                json.dumps({"parser": "markdown"}),
                "2099-01-01T00:00:00+00:00",
            ),
        )
        db.execute(
            "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,"
            "element_ids,created_at) VALUES (?,?,?,?,?,?,?)",
            (
                "ck-reparsed",
                notebook_id,
                source_id,
                "重新解析之后的全新正文。",
                "",
                json.dumps([f"el-{source_id}-9001"]),
                "2099-01-01T00:00:00+00:00",
            ),
        )
        db.commit()


def _concurrent_reparse_run(repo, notebook_id, source_id, outputs, **kwargs):
    """在 plan 之后、apply 之前插一次并发重解析，然后跑完整趟。"""
    original = phase.plan_for_source
    fired: list[int] = []

    def _plan_then_reparse(repo_arg, source, image_index):
        result = original(repo_arg, source, image_index)
        if source["id"] == source_id and not fired:
            fired.append(1)
            _reparse_in_place(repo, notebook_id, source_id)
        return result

    phase.plan_for_source = _plan_then_reparse
    try:
        result = _run(repo, notebook_id, outputs, **kwargs)
    finally:
        phase.plan_for_source = original
    assert fired, "变异守护：并发重解析没有真的发生，这个用例什么都没验证"
    return result


def test_a_concurrent_reparse_makes_the_source_fall_through_without_dirty_writes(
    repo, seeded, outputs
):
    """SQLite 侧 maintenance CLI 没有停服强制，所以计划阶段的元素/chunk 快照可能
    在写事务开始前就已作废。照它写下去会把旧 `element_ids` 整份盖回换代后的 chunk、
    把新图挂到已删的锚点 id 上——三种都不报错，只是把库写脏。写事务里的 CAS 必须
    让整源安全落空。"""
    notebook_id, source_id = seeded
    result = _concurrent_reparse_run(repo, notebook_id, source_id, outputs)

    assert result["sources_concurrent_change"] == 1
    assert result["sources_failed"] == 0        # 不是"坏了"，是"稍后重跑"
    assert result["images_inserted"] == 0
    assert result["skipped"].get("concurrent_change") == 2

    # 库里零脏写：重解析后的那条元素/ chunk 原样，没有多出图片元素或反查行。
    assert _rows(
        repo,
        "SELECT id FROM source_elements WHERE source_id=? AND element_type='image'",
        source_id,
    ) == []
    chunks = _rows(repo, "SELECT id, element_ids FROM chunks WHERE source_id=?", source_id)
    assert [row["id"] for row in chunks] == ["ck-reparsed"]
    assert json.loads(chunks[0]["element_ids"]) == [f"el-{source_id}-9001"]
    # 本趟落下的资产也被清扫掉了，不留孤儿。
    assert _rows(repo, "SELECT id FROM notebook_assets") == []


def _replace_elements_only(repo, source_id):
    """只换元素、**不动 chunk**——这是 ingestion 真实存在的中间窗口。

    `replace_elements` 与 `replace_source_chunks` 是两次独立的仓储写（中间那把
    `_source_chunk_lock` 是**进程内**锁，离线 CLI 是另一个进程，拿不到它），所以
    "元素已经换代、chunk 还是旧的"这个状态是可达的。这一格恰恰是 chunk CAS
    **看不见**的：目标 chunk 的 id 与 element_ids 都没变。"""
    with repo._connect() as db:
        db.execute("DELETE FROM source_elements WHERE source_id=?", (source_id,))
        db.execute(
            "INSERT INTO source_elements "
            "(id, source_id, element_type, location_label, text, metadata, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                f"el-{source_id}-9001",
                source_id,
                "paragraph",
                "Markdown paragraph 1",
                "重新解析之后的全新正文。",
                json.dumps({"parser": "markdown"}),
                "2099-01-01T00:00:00+00:00",
            ),
        )
        db.commit()


def test_elements_replaced_but_chunks_not_yet_rewritten_is_also_refused(
    repo, seeded, outputs
):
    """元素代次 CAS 单独承重的那一格：chunk 的 id 与 element_ids 一个字节没变，
    只有元素换了代——chunk CAS 完全看不见它。少了元素代次比对，本趟就会把图挂到
    一批**已经被删掉**的锚点 id 上。"""
    notebook_id, source_id = seeded
    original = phase.plan_for_source
    fired: list[int] = []

    def _plan_then_replace(repo_arg, source, image_index):
        result = original(repo_arg, source, image_index)
        if not fired:
            fired.append(1)
            _replace_elements_only(repo, source_id)
        return result

    chunks_before = _rows(
        repo, "SELECT id, element_ids FROM chunks WHERE source_id=?", source_id
    )
    phase.plan_for_source = _plan_then_replace
    try:
        result = _run(repo, notebook_id, outputs)
    finally:
        phase.plan_for_source = original

    assert fired, "变异守护：元素替换没有真的发生"
    assert result["sources_concurrent_change"] == 1
    assert result["sources_failed"] == 0
    assert result["images_inserted"] == 0
    # chunk 一个字节没动，也没有图片元素挂到已删的锚点上。
    assert _rows(
        repo, "SELECT id, element_ids FROM chunks WHERE source_id=?", source_id
    ) == chunks_before
    assert _rows(
        repo,
        "SELECT id FROM source_elements WHERE source_id=? AND element_type='image'",
        source_id,
    ) == []
    assert _rows(repo, "SELECT id FROM notebook_assets") == []


def test_a_chunk_that_moved_without_any_element_change_is_also_refused(
    repo, seeded, outputs
):
    """chunk CAS 单独承重的那一格：元素代次信号**一个字节都没变**（COUNT 与
    MAX(created_at) 都不动），只有 chunk 的 `element_ids` 被另一个写者改了。

    这不是构造出来的形态：本命令**自己**的就地补齐就长这样——补一条未入 chunk 的
    既有元素时它只 append `chunks.element_ids`，不新增任何元素行。两个 backfill
    进程（或一次重叠的重跑）撞到同一个来源就正好落在这一格。少了 chunk CAS，本趟
    会把计划时读到的旧列表整份盖回去，把对方刚 append 的那条**默默抹掉**。"""
    notebook_id, source_id = seeded
    original = phase.plan_for_source
    fired: list[int] = []

    def _plan_then_touch_chunk(repo_arg, source, image_index):
        result = original(repo_arg, source, image_index)
        if not fired:
            fired.append(1)
            with repo._connect() as db:
                row = db.execute(
                    "SELECT id, element_ids FROM chunks WHERE source_id=? "
                    "ORDER BY id LIMIT 1",
                    (source_id,),
                ).fetchone()
                # 另一个写者 append 了一条**既有**元素 id：source_elements 不动。
                db.execute(
                    "UPDATE chunks SET element_ids=? WHERE id=?",
                    (
                        json.dumps(
                            json.loads(row["element_ids"])
                            + [f"el-{source_id}-9999"]
                        ),
                        row["id"],
                    ),
                )
                db.commit()
        return result

    signature_before = _rows(
        repo,
        "SELECT COUNT(*) AS n, MAX(created_at) AS newest FROM source_elements "
        "WHERE source_id=?",
        source_id,
    )[0]
    phase.plan_for_source = _plan_then_touch_chunk
    try:
        result = _run(repo, notebook_id, outputs)
    finally:
        phase.plan_for_source = original

    assert fired, "变异守护：并发 chunk 改写没有真的发生"
    signature_after = _rows(
        repo,
        "SELECT COUNT(*) AS n, MAX(created_at) AS newest FROM source_elements "
        "WHERE source_id=?",
        source_id,
    )[0]
    assert dict(signature_after) == dict(signature_before), (
        "前置条件：元素代次信号必须没变，否则这个用例验的是元素 CAS 而不是 chunk CAS"
    )
    assert result["sources_concurrent_change"] == 1
    assert result["sources_failed"] == 0
    assert result["images_inserted"] == 0
    # 对方 append 的那条还在——没有被旧快照盖掉。
    kept = [
        json.loads(row["element_ids"])
        for row in _rows(
            repo, "SELECT element_ids FROM chunks WHERE source_id=? ORDER BY id", source_id
        )
    ]
    assert any(f"el-{source_id}-9999" in ids for ids in kept)
    assert _rows(repo, "SELECT id FROM notebook_assets") == []


def test_a_concurrent_reparse_does_not_stop_the_rest_of_the_run(
    repo, seeded, outputs, tmp_path
):
    """单源收编：被并发改掉的那个源落空，后面的源照常补。"""
    notebook_id, source_id = seeded
    docs = tmp_path / "second"
    docs.mkdir()
    (docs / "other.md").write_text(
        MD + "\n第二篇文档独有的收尾段落。\n", encoding="utf-8"
    )
    bi.run_ingest(repo, notebook_id, bi.iter_files(docs), workers=1)
    other = next(
        row["id"]
        for row in _rows(repo, "SELECT id FROM sources ORDER BY id")
        if row["id"] != source_id
    )

    result = _concurrent_reparse_run(repo, notebook_id, source_id, outputs)
    assert result["sources_scanned"] == 2
    assert result["sources_concurrent_change"] == 1
    assert result["images_inserted"] == 2  # 另一个源照常补上
    owners = {
        row["source_id"]
        for row in _rows(
            repo, "SELECT source_id FROM source_elements WHERE element_type='image'"
        )
    }
    assert owners == {other}


def test_an_asset_row_orphaned_mid_pass_is_reclaimed_by_that_same_pass(
    repo, seeded, outputs, monkeypatch, tmp_path
):
    """`save_source_image` **先提交 `notebook_assets` 行、后写盘**，所以写盘失败时
    它抛异常而调用方拿不到返回值——那一行既没人引用，`sweep_orphan_assets` 又刻意
    不扫带 `source_id` 的行，于是成为一条谁也够不着的孤儿。

    模拟走**真实的** `save_source_image`（只把落盘目标换成一个写不进去的路径），
    这样 `on_row_created` 回调这条承重接线才真的被执行到——它就是本命令拿到那个
    id 的唯一途径。"""
    from app.services.knowhow.assets import AssetService

    notebook_id, source_id = seeded
    original_path_for = AssetService.path_for
    hijacked: list[str] = []

    def _unwritable_path(self, asset):
        if not hijacked:
            hijacked.append(asset["id"])
            return tmp_path  # 已存在的目录：write_bytes 抛 IsADirectoryError
        return original_path_for(self, asset)

    monkeypatch.setattr(AssetService, "path_for", _unwritable_path)
    result = _run(repo, notebook_id, outputs)

    assert hijacked, "变异守护：写盘失败没有真的发生，这个用例什么都没验证"
    assert result["skipped"].get("asset_write_failed") == 1
    assert result["images_inserted"] == 1  # 第二张照常落地
    assert result["orphan_assets_removed"] == 1
    # 泄漏那一行没了，活着的那张图的资产还在。
    remaining = {row["id"] for row in _rows(repo, "SELECT id FROM notebook_assets")}
    assert hijacked[0] not in remaining
    assert len(remaining) == 1


def test_an_asset_created_concurrently_by_a_live_service_is_never_deleted(
    repo, seeded, outputs, monkeypatch, tmp_path
):
    """清扫只许碰**本次调用亲手铸出来**的 id。

    SQLite 侧本命令可与活服务并存，而在线解析是**先落资产、之后才换元素**，所以
    本趟执行期间冒出来的资产行完全可能是并发重解析建的合法数据（CAS 看的是元素与
    chunk，不是资产，通过也不排除它）。文件名同样区分不开：在线 MinerU 路径按
    ``Path(img_path).name`` 落的就是 ``<sha>.jpg``，与我们铸的名字形状逐字相同。
    这里在同一趟里让一条这样的行出现，并把它做成"没有元素引用"的最坏形态。"""
    from app.services.knowhow.assets import AssetService

    notebook_id, source_id = seeded
    original_path_for = AssetService.path_for
    concurrent: list[str] = []
    hijacked: list[str] = []

    def _unwritable_path(self, asset):
        if not hijacked:
            hijacked.append(asset["id"])
            # 活服务此刻正在重新解析同一个来源：先落资产，元素稍后才换。
            concurrent.append(
                repo.insert_notebook_asset(
                    notebook_id,
                    "aaa.jpg",  # 与我们铸的名字逐字相同，形状上无从区分
                    "image/jpeg",
                    16,
                    "live-service",
                    source_id=source_id,
                )
            )
            return tmp_path
        return original_path_for(self, asset)

    monkeypatch.setattr(AssetService, "path_for", _unwritable_path)
    result = _run(repo, notebook_id, outputs)

    assert hijacked and concurrent, "变异守护：并发资产没有真的建出来"
    assert result["orphan_assets_removed"] == 1  # 只删了自己那条
    remaining = {row["id"] for row in _rows(repo, "SELECT id FROM notebook_assets")}
    assert concurrent[0] in remaining, "并发的合法资产被误删了"
    assert hijacked[0] not in remaining


def test_the_sweep_never_touches_asset_rows_it_did_not_create(
    repo, seeded, outputs
):
    """反向护栏：清扫**只**碰本次调用亲手铸出来的 id。

    若换成任何形式的"无人引用"差集判据，深拷贝留下的资产行会被连同磁盘文件一起
    删掉——`notebook_sharing` 为 `notebook_assets` 铸新 id，却**不**重映射
    `source_elements.metadata.asset_id`（它的 json_maps 只含 element/source/object
    三类），所以一本副本里每一条来源图片资产行都"无人引用"。这里预置的正是那种
    形状的行。"""
    notebook_id, source_id = seeded
    stale = repo.insert_notebook_asset(
        notebook_id, "copied.jpg", "image/jpeg", 3, "u", source_id=source_id
    )
    result = _run(repo, notebook_id, outputs)

    assert result["images_inserted"] == 2
    assert result["orphan_assets_removed"] == 0
    assert stale in {row["id"] for row in _rows(repo, "SELECT id FROM notebook_assets")}


def test_a_failed_write_transaction_sweeps_this_pass_s_assets(
    repo, seeded, outputs, monkeypatch
):
    """元素事务失败这条路同样收掉本次铸出来的全部资产（连那些"行已提交、写盘失败"
    因而没有返回值的也在内——它们的 id 由 `on_row_created` 回调记下了）。"""
    notebook_id, _ = seeded

    def _boom(*_args, **_kwargs):
        raise RuntimeError("write failed")

    monkeypatch.setattr(repo.maintenance, "apply_image_backfill", _boom, raising=True)
    result = _run(repo, notebook_id, outputs)
    assert result["sources_failed"] == 1
    assert _rows(repo, "SELECT id FROM notebook_assets") == []


def test_report_jsonl_carries_counts_only(repo, seeded, outputs, tmp_path):
    notebook_id, source_id = seeded
    report = tmp_path / "report.jsonl"
    _run(repo, notebook_id, outputs, report_path=report)
    entries = [json.loads(line) for line in report.read_text().splitlines()]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["source_id"] == source_id
    assert entry["inserted"] == 2
    assert entry["candidates"] == 2
    assert entry["candidates_insert"] == 2
    assert entry["candidates_enrich"] == 0
    assert entry["skipped"] == {"image_not_found": 1}
    assert set(entry) == {
        "source_id",
        "status",
        "file_name",
        "candidates",
        "candidates_insert",
        "candidates_enrich",
        "inserted",
        "enriched",
        "captions",
        "orphan_assets_removed",
        "coverage",
        "skipped",
    }


def test_report_parent_directory_is_created(repo, seeded, outputs, tmp_path):
    """`--report` 指向一个还不存在的目录时不该在扫完整本库之后才丢掉全部明细。"""
    notebook_id, _ = seeded
    report = tmp_path / "runs" / "2026-08" / "report.jsonl"
    _run(repo, notebook_id, outputs, report_path=report)
    assert report.is_file()
    assert len(report.read_text().splitlines()) == 1


def test_source_id_filter_processes_only_that_source(repo, seeded, outputs, tmp_path):
    notebook_id, source_id = seeded
    docs = tmp_path / "more"
    docs.mkdir()
    # 与上传的内容哈希去重错开，否则第二个来源根本不会被建出来（见
    # `test_after_id_resumes_past_already_processed_sources` 的同款注释）。
    (docs / "other.md").write_text(MD + "\n第二篇文档独有的收尾段落。\n", encoding="utf-8")
    bi.run_ingest(repo, notebook_id, bi.iter_files(docs), workers=1)
    assert len(_rows(repo, "SELECT id FROM sources")) == 2

    result = _run(repo, notebook_id, outputs, source_id=source_id)
    assert result["sources_scanned"] == 1
    owners = {
        row["source_id"]
        for row in _rows(
            repo,
            "SELECT source_id FROM source_elements WHERE element_type='image'",
        )
    }
    assert owners == {source_id}


# -------------------------------------------------- 既有无资产图片的就地补齐

#: 带 alt 的相对路径图片：解析路径会为它产出一条 image 元素（alt 当图注、写下
#: `metadata.src`），但拿不到 `asset_id`（单文件 markdown 路径不传 resolve_image）。
MD_WITH_ALT = """# 系统总体设计

本章介绍系统的总体设计目标与边界，并给出关键取舍。

![图 1 系统总体架构](images/aaa.jpg)

数据通路由采集、解析、检索三段构成，各段之间以持久化产物解耦。
"""


@pytest.fixture
def seeded_with_alt(repo, tmp_path):
    docs = tmp_path / "alt-docs"
    docs.mkdir()
    (docs / "alt.md").write_text(MD_WITH_ALT, encoding="utf-8")
    notebook_id = bi.ensure_notebook(repo, None, "nb-alt")
    bi.run_ingest(repo, notebook_id, bi.iter_files(docs), workers=1)
    with repo._connect() as db:
        source_id = db.execute(
            "SELECT id FROM sources WHERE notebook_id=?", (notebook_id,)
        ).fetchone()["id"]
    return notebook_id, source_id


def _image_elements(repo, source_id):
    return _rows(
        repo,
        "SELECT id, element_type, text, created_at, metadata FROM source_elements "
        "WHERE source_id=? AND element_type='image' ORDER BY id",
        source_id,
    )


def test_an_existing_image_element_is_enriched_in_place_not_duplicated(
    repo, seeded_with_alt, outputs
):
    """生产解析路径对带 alt 的图产出的元素有 `src`、没有 `asset_id`，所以它不在
    "已补过"集合里；只插入就会给同一张图造出第二条元素行（两条都进 chunk、引用
    带图会重复显示同一张图）。改为就地补齐：只 UPDATE metadata。"""
    notebook_id, source_id = seeded_with_alt
    before = _image_elements(repo, source_id)
    assert len(before) == 1
    assert not json.loads(before[0]["metadata"]).get("asset_id")

    result = _run(repo, notebook_id, outputs)
    assert result["images_inserted"] == 0
    assert result["images_enriched"] == 1
    assert result["candidates_enrich"] == 1

    after = _image_elements(repo, source_id)
    assert len(after) == 1  # 没有多出第二条行
    assert after[0]["id"] == before[0]["id"]
    assert after[0]["text"] == before[0]["text"]  # 图注/正文不动
    assert after[0]["created_at"] == before[0]["created_at"]
    metadata = json.loads(after[0]["metadata"])
    assert metadata["asset_id"]
    assert metadata["src"] == "images/aaa.jpg"  # 增量判据留着
    assert metadata["parser"] == "markdown"  # 出处不改写


def test_enriched_image_is_reachable_by_the_citation_image_path(
    repo, seeded_with_alt, outputs
):
    from app.services.evidence_context import _citation_image

    notebook_id, source_id = seeded_with_alt
    _run(repo, notebook_id, outputs)
    element_ids: list[str] = []
    for row in _rows(
        repo, "SELECT element_ids FROM chunks WHERE source_id=?", source_id
    ):
        element_ids.extend(json.loads(row["element_ids"]))
    rows = repo._runtime.source_store.image_asset_rows(element_ids)
    assert len(rows) == 1
    assert _citation_image(*rows[0]) is not None


def test_enrichment_leaves_chunks_untouched_when_the_element_is_already_chunked(
    repo, seeded_with_alt, outputs
):
    """带图注的图早就进过分块，所以补齐它时 chunk 一个字节都不该改。"""
    notebook_id, source_id = seeded_with_alt
    before = {
        row["id"]: (row["text"], row["element_ids"])
        for row in _rows(
            repo,
            "SELECT id, text, element_ids FROM chunks WHERE source_id=?",
            source_id,
        )
    }
    _run(repo, notebook_id, outputs)
    after = {
        row["id"]: (row["text"], row["element_ids"])
        for row in _rows(
            repo,
            "SELECT id, text, element_ids FROM chunks WHERE source_id=?",
            source_id,
        )
    }
    assert after == before


def test_enriching_an_unchunked_element_appends_it_and_its_reverse_row(
    repo, seeded_with_alt, outputs
):
    """另一子形态：既有元素不属于任何 chunk。按新插入同款路径 append 进锚点
    chunk，并在**同一写事务**里补反查行。"""
    notebook_id, source_id = seeded_with_alt
    image_id = _image_elements(repo, source_id)[0]["id"]
    with repo._connect() as db:
        for row in db.execute(
            "SELECT id, element_ids FROM chunks WHERE source_id=?", (source_id,)
        ).fetchall():
            ids = [item for item in json.loads(row["element_ids"]) if item != image_id]
            db.execute(
                "UPDATE chunks SET element_ids=? WHERE id=?",
                (json.dumps(ids), row["id"]),
            )
        db.execute("DELETE FROM chunk_elements WHERE element_id=?", (image_id,))
        db.commit()

    result = _run(repo, notebook_id, outputs)
    assert result["images_enriched"] == 1
    owners = [
        row["id"]
        for row in _rows(
            repo, "SELECT id, element_ids FROM chunks WHERE source_id=?", source_id
        )
        if image_id in json.loads(row["element_ids"])
    ]
    assert len(owners) == 1
    reverse = _rows(
        repo,
        "SELECT chunk_id FROM chunk_elements WHERE element_id=?",
        image_id,
    )
    assert [row["chunk_id"] for row in reverse] == owners


def test_an_enrichment_only_source_reports_its_captions_in_every_outlet(
    repo, seeded_with_alt, outputs, tmp_path, capsys
):
    """图注统计只数新插入的话，一个**纯补齐**的来源会报「图注 0」——而那条元素明明
    带着图注。dry-run 与真跑必须给出同一个非零数字，且三个出口一致。"""
    notebook_id, source_id = seeded_with_alt
    report = tmp_path / "enrich.jsonl"
    planned = _run(repo, notebook_id, outputs, dry_run=True, report_path=report)

    assert planned["candidates_enrich"] == 1
    assert planned["candidates_insert"] == 0
    assert planned["captions"] == 1
    entry = json.loads(report.read_text().splitlines()[0])
    assert entry["captions"] == 1
    assert "图注=1" in capsys.readouterr().out

    applied = _run(repo, notebook_id, outputs, report_path=report)
    assert applied["images_enriched"] == 1
    assert applied["images_inserted"] == 0
    assert applied["captions"] == planned["captions"] == 1
    assert json.loads(report.read_text().splitlines()[1])["captions"] == 1


def test_a_pure_image_source_is_enriched_even_though_alignment_cannot_run(
    repo, outputs, tmp_path
):
    """端到端的纯图片 markdown：一条文本行都没有 → coverage 恒 0 → 落在漂移闸
    之下。补齐按精确 src 相等工作，不该被那道闸拦住。"""
    docs = tmp_path / "gallery"
    docs.mkdir()
    (docs / "gallery.md").write_text(
        "![图 1 系统总体架构](images/aaa.jpg)\n\n![图 2 数据通路](images/bbb.jpg)\n",
        encoding="utf-8",
    )
    notebook_id = bi.ensure_notebook(repo, None, "nb-gallery")
    bi.run_ingest(repo, notebook_id, bi.iter_files(docs), workers=1)
    with repo._connect() as db:
        source_id = db.execute(
            "SELECT id FROM sources WHERE notebook_id=?", (notebook_id,)
        ).fetchone()["id"]
    before = _image_elements(repo, source_id)
    assert len(before) == 2
    assert not any(json.loads(row["metadata"]).get("asset_id") for row in before)

    result = _run(repo, notebook_id, outputs)
    assert result["images_enriched"] == 2
    assert result["images_inserted"] == 0
    assert result["skipped"].get("alignment_drifted") is None
    assert result["captions"] == 2

    after = _image_elements(repo, source_id)
    assert len(after) == 2  # 没有多出重复行
    assert all(json.loads(row["metadata"])["asset_id"] for row in after)
    assert [row["text"] for row in after] == [row["text"] for row in before]


def test_enrichment_is_idempotent_across_reruns(repo, seeded_with_alt, outputs):
    notebook_id, source_id = seeded_with_alt
    _run(repo, notebook_id, outputs)
    before = _snapshot(repo, notebook_id)
    assets_before = _rows(repo, "SELECT id FROM notebook_assets")

    result = _run(repo, notebook_id, outputs)
    assert result["images_enriched"] == 0
    assert result["skipped"].get("already_backfilled") == 1
    assert _snapshot(repo, notebook_id) == before
    assert _rows(repo, "SELECT id FROM notebook_assets") == assets_before


# ------------------------------------------------------------ 前置条件 / 报表

def test_an_output_tree_with_no_images_refuses_to_run(repo, seeded, tmp_path):
    """空索引跑下去会是一次"看起来正常"的全库空扫（每张图都 image_not_found，
    汇总一片零），运维会读成"原图确实找不回来了"。早退报错。"""
    empty = tmp_path / "empty-output"
    (empty / "sess" / "doc" / "auto").mkdir(parents=True)
    notebook_id, _ = seeded
    with pytest.raises(phase.ImageBackfillIndexEmpty):
        _run(repo, notebook_id, [empty])


def test_every_output_root_missing_refuses_to_run(repo, seeded, tmp_path):
    notebook_id, _ = seeded
    with pytest.raises(phase.ImageBackfillIndexEmpty):
        _run(repo, notebook_id, [tmp_path / "nope-a", tmp_path / "nope-b"])


def test_one_missing_root_among_several_still_runs(repo, seeded, outputs, tmp_path):
    notebook_id, _ = seeded
    result = _run(repo, notebook_id, [tmp_path / "nope", outputs[0]])
    assert result["images_inserted"] == 2


def test_dry_run_prints_one_line_per_source_with_the_candidate_counts(
    repo, seeded, outputs, capsys
):
    """dry-run 的**唯一**产出就是这几行：不逐源打出来，运维拿不到"这一跑能补多
    少张"（汇总只有总数，看不出是哪些源、锚定失败集中在哪）。"""
    notebook_id, source_id = seeded
    result = _run(repo, notebook_id, outputs, dry_run=True)
    assert result["candidates_insert"] == 2
    assert result["images_inserted"] == 0
    out = capsys.readouterr().out
    assert source_id in out
    assert "候选=2(新增 2/补齐 0)" in out
    assert "缺图=1" in out


def test_dry_run_captions_match_what_the_real_run_lands(
    repo, seeded, outputs, tmp_path, capsys
):
    """真跑时 `captions` 按**实际落地**的图重算，而 dry-run 一张都不落——照那条
    口径它会恒为 0，逐源行、JSONL 与汇总齐刷刷报「图注 0」，恰好废掉 dry-run 唯一
    的估算用途。dry-run 下改取计划命中数，于是它与随后真跑的结果相符。"""
    notebook_id, _ = seeded
    report = tmp_path / "dry.jsonl"
    planned = _run(repo, notebook_id, outputs, dry_run=True, report_path=report)

    # 这份 MD 里只有第一张图带「图 1 …」图注。三个出口都要拿到这个数。
    assert planned["captions"] == 1
    assert json.loads(report.read_text().splitlines()[0])["captions"] == 1
    assert "图注=1" in capsys.readouterr().out
    assert _rows(repo, "SELECT id FROM notebook_assets") == []  # 仍然零写入

    applied = _run(repo, notebook_id, outputs)
    assert applied["captions"] == planned["captions"]


def test_source_id_that_matches_nothing_says_which_two_reasons(
    repo, seeded, outputs, capsys
):
    notebook_id, _ = seeded
    result = _run(repo, notebook_id, outputs, source_id="src-does-not-exist")
    assert result["sources_scanned"] == 0
    out = capsys.readouterr().out
    assert "--source-id src-does-not-exist 未命中候选" in out


def test_after_id_resumes_past_already_processed_sources(
    repo, seeded, outputs, tmp_path
):
    """`--after-id` 是 keyset 起点直传：中断后重跑安全但会全量重扫，传上一跑最后
    处理到的来源 id 就能跳过那一段。"""
    notebook_id, first_id = seeded
    docs = tmp_path / "more"
    docs.mkdir()
    # 正文必须与第一个来源不同：上传按内容哈希在同 notebook 内去重，逐字复制
    # 会复用既有来源行，这个用例就只剩一个来源、断言全部空转。
    (docs / "other.md").write_text(MD + "\n第二篇文档独有的收尾段落。\n", encoding="utf-8")
    bi.run_ingest(repo, notebook_id, bi.iter_files(docs), workers=1)
    ids = sorted(row["id"] for row in _rows(repo, "SELECT id FROM sources"))
    assert len(ids) == 2

    result = _run(repo, notebook_id, outputs, after_id=ids[0])
    assert result["sources_scanned"] == 1
    owners = {
        row["source_id"]
        for row in _rows(
            repo, "SELECT source_id FROM source_elements WHERE element_type='image'"
        )
    }
    assert owners == {ids[1]}


def test_an_unknown_extension_is_rejected_rather_than_stored_as_jpeg(
    repo, seeded, tmp_path
):
    """`_guess_mime` 不再兜底成 image/jpeg：兜底救不了任何真图片（MinerU 只产
    jpg/png），只会把一个认不出扩展名的文件当 JPEG 写进资产表，落一个浏览器永远
    渲染不出来的错 mime。"""
    images = tmp_path / "odd" / "sess" / "doc" / "auto" / "images"
    images.mkdir(parents=True)
    (images / "aaa.jpg.bin").write_bytes(b"\xff\xd8\xff\xe0" + b"a" * 64)
    notebook_id, _ = seeded
    # 让 markdown 里的引用指向这个怪扩展名。
    with repo._connect() as db:
        path = db.execute(
            "SELECT file_path FROM sources WHERE notebook_id=?", (notebook_id,)
        ).fetchone()["file_path"]
    Path(path).write_text(
        MD.replace("images/aaa.jpg", "images/aaa.jpg.bin"), encoding="utf-8"
    )
    result = _run(repo, notebook_id, [tmp_path / "odd"])
    assert result["images_inserted"] == 0
    assert result["skipped"].get("mime_rejected") == 1
    assert _rows(repo, "SELECT id FROM notebook_assets") == []


# ------------------------------------------------------------------ CLI

def test_cli_requires_a_notebook_and_an_output_tree(capsys):
    assert bi.main(["backfill-images", "--mineru-output", "/tmp/x"]) == 2
    assert bi.main(["backfill-images", "--notebook-id", "nb"]) == 2
    err = capsys.readouterr().err
    assert "backfill-images" in err


def test_cli_dry_run_is_a_database_pass_not_an_input_dir_preview(
    repo, seeded, outputs, capsys
):
    notebook_id, _ = seeded
    code = bi.main(
        [
            "backfill-images",
            "--notebook-id",
            notebook_id,
            "--mineru-output",
            str(outputs[0]),
            "--dry-run",
            "--confirm-service-stopped",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "backfill-images done" in out
    assert _rows(repo, "SELECT id FROM notebook_assets") == []


def test_a_query_string_src_is_enriched_end_to_end_not_duplicated(
    repo, outputs, tmp_path
):
    """端到端的 query/fragment 形态：解析路径存全量 src，回填两侧共用规范形。"""
    docs = tmp_path / "qs"
    docs.mkdir()
    (docs / "qs.md").write_text(
        "正文一段足够长的话在这里说明系统设计。\n\n"
        "![图 1 系统总体架构](images/aaa.jpg?raw=1#anchor)\n",
        encoding="utf-8",
    )
    notebook_id = bi.ensure_notebook(repo, None, "nb-qs")
    bi.run_ingest(repo, notebook_id, bi.iter_files(docs), workers=1)
    with repo._connect() as db:
        source_id = db.execute(
            "SELECT id FROM sources WHERE notebook_id=?", (notebook_id,)
        ).fetchone()["id"]
    before = _image_elements(repo, source_id)
    assert len(before) == 1
    assert json.loads(before[0]["metadata"])["src"] == "images/aaa.jpg?raw=1#anchor"

    result = _run(repo, notebook_id, outputs)
    assert result["images_enriched"] == 1
    assert result["images_inserted"] == 0

    after = _image_elements(repo, source_id)
    assert len(after) == 1  # 没有重复行
    assert json.loads(after[0]["metadata"])["asset_id"]
    # 重跑幂等：规范形同样让「已补过」那半认得出来。
    assert _run(repo, notebook_id, outputs)["images_enriched"] == 0


def test_cli_exits_non_zero_when_a_source_fails(repo, seeded, outputs, monkeypatch):
    """丢掉汇总、恒返回 0 会让编排脚本把一次「写事务炸了」的运行当成完全成功。"""
    notebook_id, _ = seeded

    def _boom(*_args, **_kwargs):
        raise RuntimeError("write failed")

    monkeypatch.setattr(repo.maintenance, "apply_image_backfill", _boom, raising=True)
    code = bi.main(
        [
            "backfill-images",
            "--notebook-id",
            notebook_id,
            "--mineru-output",
            str(outputs[0]),
            "--confirm-service-stopped",
        ]
    )
    assert code == 1


def test_cli_exits_zero_when_a_source_is_only_concurrently_changed(
    repo, seeded, outputs
):
    """`concurrent_change` 是「稍后重跑」不是「坏了」：它不进退出码，否则活服务上
    的正常运维跑会恒定非零。"""
    notebook_id, source_id = seeded
    original = phase.plan_for_source
    fired: list[int] = []

    def _plan_then_reparse(repo_arg, source, image_index):
        result = original(repo_arg, source, image_index)
        if not fired:
            fired.append(1)
            _reparse_in_place(repo, notebook_id, source_id)
        return result

    phase.plan_for_source = _plan_then_reparse
    try:
        code = bi.main(
            [
                "backfill-images",
                "--notebook-id",
                notebook_id,
                "--mineru-output",
                str(outputs[0]),
                "--confirm-service-stopped",
            ]
        )
    finally:
        phase.plan_for_source = original
    assert fired, "变异守护：并发重解析没有真的发生"
    assert code == 0


def test_cli_applies_the_backfill(repo, seeded, outputs):
    notebook_id, source_id = seeded
    code = bi.main(
        [
            "backfill-images",
            "--notebook-id",
            notebook_id,
            "--mineru-output",
            str(outputs[0]),
            "--confirm-service-stopped",
        ]
    )
    assert code == 0
    images = _rows(
        repo,
        "SELECT id FROM source_elements WHERE source_id=? AND element_type='image'",
        source_id,
    )
    assert len(images) == 2
