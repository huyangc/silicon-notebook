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
    return notebook_id, source_id


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


def test_report_jsonl_carries_counts_only(repo, seeded, outputs, tmp_path):
    notebook_id, source_id = seeded
    report = tmp_path / "report.jsonl"
    _run(repo, notebook_id, outputs, report_path=report)
    entries = [json.loads(line) for line in report.read_text().splitlines()]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["source_id"] == source_id
    assert entry["inserted"] == 2
    assert entry["skipped"] == {"image_not_found": 1}
    assert set(entry) == {
        "source_id",
        "status",
        "file_name",
        "candidates",
        "inserted",
        "captions",
        "coverage",
        "skipped",
    }


def test_source_id_filter_processes_only_that_source(repo, seeded, outputs, tmp_path):
    notebook_id, source_id = seeded
    docs = tmp_path / "more"
    docs.mkdir()
    (docs / "other.md").write_text(MD, encoding="utf-8")
    bi.run_ingest(repo, notebook_id, bi.iter_files(docs), workers=1)

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
