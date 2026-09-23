"""守卫: 增量包的导入 (app.migration.sync.import_ 的 PR-3c 相位)。对应
docs/incremental-sync-design.md §8「导入相位」的增量分支。

跑的是真正的两库端到端: 库 A 种数据 + 开捕获闸 → 全量导出 → 库 B 导入 (基线)
→ 库 A 改动/删除 → 增量导出 → 库 B 导入 → 逐表核对。盯的是增量独有的那几件事:
包分类与链校验、删除条目的范围校验、3c 删除重放 (归属/absent/孤儿/文件/FTS)、
3d 笔记本删除传播 (tombstone + 作业行)、4 文件合并 (不换目录) 与断点续跑。
PostgreSQL 泳道在 tests/postgres/test_sync_incremental_import_pg.py。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.core.config import Settings
from app.migration.sync.export import ExportReport, export_notebooks
from app.migration.sync.import_ import (
    SyncImportError,
    import_package,
)
from app.migration.sync.package import (
    CHECKSUMS_NAME,
    DELETES_NAME,
    MANIFEST_NAME,
    MODE_FULL,
    MODE_INCREMENTAL,
    json_line,
    rows_path,
)
from app.models.schemas import NotebookCreate
from app.repositories.ports import UploadedSourceFile
from app.services.sqlite_repository import SQLiteRepository


SOURCE_ENV = "dev"
TARGET_ENV = "prod"
MOMENT = "2026-01-01T00:00:00+00:00"


# --------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def _quiet_side_channels(monkeypatch):
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")


def _settings(root: Path) -> Settings:
    root.mkdir(parents=True, exist_ok=True)
    return Settings(
        database_url=f"sqlite:///{root / 'sync.db'}",
        storage_dir=str(root / "storage"),
    )


def _upload(repo, notebook_id: str, name: str) -> str:
    repo.upload_sources(
        notebook_id,
        [
            UploadedSourceFile(
                file_name=name,
                content_type="text/plain",
                content=f"{name} body\n".encode("utf-8") * 8,
                doc_type="",
                doc_type_explicit=False,
            )
        ],
    )
    with repo._connect() as db:
        row = db.execute(
            "SELECT id FROM sources WHERE notebook_id=? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (notebook_id,),
        ).fetchone()
    assert row is not None, f"upload of {name} produced no sources row"
    return str(row["id"])


def _seed_asset(repo, settings: Settings, notebook_id: str, asset_id: str) -> Path:
    directory = Path(settings.storage_dir) / "assets" / notebook_id
    directory.mkdir(parents=True, exist_ok=True)
    body = directory / f"{asset_id}.png"
    body.write_bytes(b"\x89PNG-body")
    with repo._write() as db:
        db.execute(
            "INSERT INTO notebook_assets(id,notebook_id,filename,mime,size,"
            "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
            (asset_id, notebook_id, "picture.png", "image/png", 9,
             "user-local", MOMENT),
        )
    return body


def _seed_notebook(repo, name: str) -> str:
    notebook = repo.create_notebook(NotebookCreate(name=name))
    _upload(repo, notebook.id, f"{name}.txt")
    table_id = repo.create_knowhow_table(
        notebook.id, f"{name}-table", "",
        [{"name": "Topic", "role": "anchor"}], created_by="user-local",
    )
    columns = repo.get_knowhow_table(table_id)["columns"]
    repo.add_knowhow_row(
        table_id, {column["id"]: f"{name} value" for column in columns},
        actor="user-local",
    )
    repo.create_memory_candidate(
        notebook.id, "user-local", None, "记忆请求", f"{name} 记忆", "正文",
        ["t"], "test",
    )
    return notebook.id


def _enable_capture(repo) -> None:
    with repo._write() as db:
        db.execute(
            "INSERT INTO sync_capture_control (singleton, enabled, enabled_at) "
            "VALUES (1, 1, ?) ON CONFLICT (singleton) DO UPDATE SET enabled = 1",
            (MOMENT,),
        )


def _close_capture(repo) -> None:
    with repo._write() as db:
        db.execute("UPDATE sync_capture_control SET enabled = 0 WHERE singleton = 1")


@pytest.fixture
def source(tmp_path):
    settings = _settings(tmp_path / "a")
    repo = SQLiteRepository(settings)
    try:
        yield {"repo": repo, "settings": settings}
    finally:
        repo.close()


@pytest.fixture
def target(tmp_path):
    settings = _settings(tmp_path / "b")
    repo = SQLiteRepository(settings)
    try:
        yield {"repo": repo, "settings": settings}
    finally:
        repo.close()


# ---------------------------------------------------------------- helpers


def _export(source, out_dir: Path, notebook_ids=None, *, full=False) -> ExportReport:
    return export_notebooks(
        source["settings"],
        target_env=TARGET_ENV,
        out_dir=out_dir,
        notebook_ids=notebook_ids,
        source_env=SOURCE_ENV,
        full=full,
    )


def _import(target, package_dir: Path, **kwargs):
    return import_package(target["settings"], package_dir, **kwargs)


def _count(repo, statement: str, params=()) -> int:
    with repo._connect() as db:
        return int(db.execute(statement, params).fetchone()[0])


def _one(repo, statement: str, params=()):
    with repo._connect() as db:
        return db.execute(statement, params).fetchone()


def _rows(package: Path, table: str) -> list[dict]:
    text = (package / rows_path(table)).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def _deletes(package: Path) -> list[dict]:
    text = (package / DELETES_NAME).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def _write_deletes(package: Path, entries) -> None:
    (package / DELETES_NAME).write_text(
        "".join(json_line(entry) + "\n" for entry in entries), encoding="utf-8"
    )


def _manifest(package: Path) -> dict:
    return json.loads((package / MANIFEST_NAME).read_text(encoding="utf-8"))


def _write_manifest(package: Path, document: dict) -> None:
    (package / MANIFEST_NAME).write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )


def _reseal(package: Path) -> None:
    """Recompute checksums.json and the manifest digest after a test edits a
    package file on purpose, so the edit is tested for what it IS rather than
    stopped at the checksum gate (same helper as tests/test_sync_import.py)."""
    checksums = json.loads((package / CHECKSUMS_NAME).read_text(encoding="utf-8"))
    for relative in checksums:
        checksums[relative] = hashlib.sha256(
            (package / relative).read_bytes()
        ).hexdigest()
    (package / CHECKSUMS_NAME).write_text(
        json.dumps(checksums, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    document = _manifest(package)
    document["checksums_sha256"] = hashlib.sha256(
        (package / CHECKSUMS_NAME).read_bytes()
    ).hexdigest()
    for table, entry in (document.get("tables") or {}).items():
        digest = checksums.get(rows_path(table))
        if digest is not None:
            entry["sha256"] = digest
    _write_manifest(package, document)


@pytest.fixture
def baseline(source, target, tmp_path):
    """Two notebooks at the source, capture on, one full baseline exported AND
    imported into the target. Every test here starts from a target that is a
    real mirror, because that is the only state an incremental window has any
    meaning against."""
    alpha = _seed_notebook(source["repo"], "alpha")
    beta = _seed_notebook(source["repo"], "beta")
    _enable_capture(source["repo"])
    out = tmp_path / "out"
    full = _export(source, out)
    assert full.mode == MODE_FULL
    report = _import(target, full.package_dir)
    assert report.error == ""
    assert report.mode == MODE_FULL
    return {
        "source": source,
        "target": target,
        "out": out,
        "alpha": alpha,
        "beta": beta,
        "full": full,
    }


def _window(baseline) -> ExportReport:
    report = _export(baseline["source"], baseline["out"])
    assert report.mode == MODE_INCREMENTAL, "the fixture's capture gate is open"
    return report


# ------------------------------------------------------- end to end: rows


def test_a_changed_row_reaches_the_mirror_and_the_report_names_the_mode(baseline):
    repo = baseline["source"]["repo"]
    with repo._write() as db:
        db.execute(
            "UPDATE notebooks SET name='alpha v2' WHERE id=?", (baseline["alpha"],)
        )

    window = _window(baseline)
    report = _import(baseline["target"], window.package_dir)

    assert report.error == ""
    assert report.mode == MODE_INCREMENTAL
    assert report.base_package_id == baseline["full"].package_id
    assert _one(
        baseline["target"]["repo"], "SELECT name FROM notebooks WHERE id=?",
        (baseline["alpha"],),
    )["name"] == "alpha v2"


def test_a_new_parent_scoped_row_arrives_without_its_unchanged_parent(baseline):
    """A knowhow row added at the source: PARENT-scoped through
    knowhow_tables, whose own row did NOT change and is therefore NOT in the
    window. A full package proves a PARENT-scoped row's scope by carrying its
    parent; a window cannot, so the importer resolves the parent against the
    TARGET and accepts it only if it lands in this package's notebook set.

    变异验证: 去掉 ``_verify_row_scopes`` 的增量 PARENT 兜底, 本条报红
    (包被拒, 消息点名 knowhow_changes.table_id)。"""
    repo = baseline["source"]["repo"]
    table_id = _one(
        repo, "SELECT id FROM knowhow_tables WHERE notebook_id=?",
        (baseline["alpha"],),
    )["id"]
    columns = repo.get_knowhow_table(table_id)["columns"]
    row_id = repo.add_knowhow_row(
        table_id, {column["id"]: "second value" for column in columns},
        actor="user-local",
    )

    window = _window(baseline)
    assert table_id not in {
        str(row["id"]) for row in _rows(window.package_dir, "knowhow_tables")
    }
    report = _import(baseline["target"], window.package_dir)

    assert report.error == ""
    assert _count(
        baseline["target"]["repo"],
        "SELECT COUNT(*) FROM knowhow_rows WHERE id=?",
        (row_id,),
    ) == 1
    assert _count(
        baseline["target"]["repo"],
        "SELECT COUNT(*) FROM knowhow_cells WHERE row_id=?",
        (row_id,),
    ) >= 1


def test_a_parent_scoped_row_whose_parent_is_another_notebooks_is_refused(baseline):
    """The other half of that leeway. Resolving the parent at the target is
    evidence, not a bypass: a parent that resolves to a notebook this package
    does not declare still refuses the package.

    变异验证: 把兜底改成「目标端存在即通过」, 本条报红。"""
    repo = baseline["source"]["repo"]
    table_id = _one(
        repo, "SELECT id FROM knowhow_tables WHERE notebook_id=?",
        (baseline["alpha"],),
    )["id"]
    columns = repo.get_knowhow_table(table_id)["columns"]
    repo.add_knowhow_row(
        table_id, {column["id"]: "second value" for column in columns},
        actor="user-local",
    )
    window = _window(baseline)
    # Re-point the target's copy of the parent table at beta, which this
    # window does not declare.
    with baseline["target"]["repo"]._write() as db:
        db.execute(
            "UPDATE knowhow_tables SET notebook_id=? WHERE id=?",
            (baseline["beta"], table_id),
        )

    with pytest.raises(SyncImportError) as failure:
        _import(baseline["target"], window.package_dir)

    assert table_id in str(failure.value)


def test_the_window_does_not_reconcile_what_it_does_not_mention(baseline):
    """The one thing an incremental import must NOT do. Phase 3a sweeps every
    row of a package's notebooks that the package does not carry -- correct
    for a snapshot, catastrophic for a window, which carries only what
    changed. A row the source never touched has to survive untouched.

    变异验证: 让 ``_import`` 对增量包也调用 ``_prune_snapshot``, 本条报红。"""
    repo = baseline["source"]["repo"]
    before = _count(
        baseline["target"]["repo"],
        "SELECT COUNT(*) FROM chunks WHERE notebook_id=?", (baseline["beta"],),
    )
    assert before, "the baseline gave beta chunks to lose"
    with repo._write() as db:
        db.execute(
            "UPDATE notebooks SET name='alpha v2' WHERE id=?", (baseline["alpha"],)
        )

    window = _window(baseline)
    _import(baseline["target"], window.package_dir)

    assert _count(
        baseline["target"]["repo"],
        "SELECT COUNT(*) FROM chunks WHERE notebook_id=?", (baseline["beta"],),
    ) == before


# ---------------------------------------------------- end to end: deletes


def test_a_deleted_chunk_is_replayed_and_leaves_the_sqlite_index(baseline):
    """The delete replay's core case, plus the SQLite FTS5 shadow table.
    ``chunks_fts`` has no triggers, so a row deleted here stays lexically
    searchable forever unless phase 3c re-projects the index in the same
    transaction.

    变异验证: 去掉 ``_replay_delete_table`` 末尾的 ``_rebuild_sqlite_fts``,
    本条的 chunks_fts 断言报红。"""
    repo = baseline["source"]["repo"]
    chunk = _one(
        repo, "SELECT id FROM chunks WHERE notebook_id=? LIMIT 1",
        (baseline["alpha"],),
    )["id"]
    with repo._write() as db:
        db.execute("DELETE FROM chunk_elements WHERE chunk_id=?", (chunk,))
        db.execute("DELETE FROM chunks WHERE id=?", (chunk,))
        db.execute("DELETE FROM chunks_fts WHERE chunk_id=?", (chunk,))

    window = _window(baseline)
    report = _import(baseline["target"], window.package_dir)

    assert report.error == ""
    assert report.deletes_applied >= 1
    mirror = baseline["target"]["repo"]
    assert _count(mirror, "SELECT COUNT(*) FROM chunks WHERE id=?", (chunk,)) == 0
    assert _count(
        mirror, "SELECT COUNT(*) FROM chunks_fts WHERE chunk_id=?", (chunk,)
    ) == 0


def test_a_delete_whose_row_the_target_does_not_have_is_counted_absent(baseline):
    """Idempotence stated as a counter rather than as a crash. That is the
    ordinary shape of a resumed run (phase 3c redoes the table it was killed
    in), and of a source that deleted a row this target never received.

    变异验证: 把 ``absent`` 分支改成抛错, 本条报红。"""
    window = _prepared_window(baseline)
    _write_deletes(
        window.package_dir,
        [
            *_deletes(window.package_dir),
            {"table": "chunks", "key": {"id": "ck-never-existed"},
             "notebook_id": baseline["alpha"], "parent_key": None},
        ],
    )
    _reseal(window.package_dir)

    report = _import(baseline["target"], window.package_dir)

    assert report.error == ""
    assert report.deletes_absent == 1
    assert report.deletes_applied == 0


def test_a_deleted_source_takes_its_stored_file_with_it(baseline):
    """``sources`` rows own bytes under ``storage/notebooks/<nb>/``. Nothing
    else will ever come back for them: the window does not carry a deleted
    row's file, and an incremental file phase merges rather than replaces.

    变异验证: 去掉 ``_replay_delete_table`` 里 ``sources`` 的 ``_FileRemoval``
    收集 (或 ``_remove_deleted_files`` 的 unlink), 本条报红。"""
    repo = baseline["source"]["repo"]
    source_id = _one(
        repo, "SELECT id FROM sources WHERE notebook_id=?", (baseline["alpha"],)
    )["id"]
    mirrored = Path(
        _one(
            baseline["target"]["repo"],
            "SELECT file_path FROM sources WHERE id=?", (source_id,),
        )["file_path"]
    )
    assert mirrored.is_file(), "the baseline installed the source's bytes"
    with repo._write() as db:
        db.execute("DELETE FROM chunk_elements WHERE notebook_id=?", (baseline["alpha"],))
        db.execute("DELETE FROM chunks WHERE source_id=?", (source_id,))
        db.execute("DELETE FROM source_elements WHERE source_id=?", (source_id,))
        db.execute("DELETE FROM sources WHERE id=?", (source_id,))

    window = _window(baseline)
    report = _import(baseline["target"], window.package_dir)

    assert report.error == ""
    assert _count(
        baseline["target"]["repo"], "SELECT COUNT(*) FROM sources WHERE id=?",
        (source_id,),
    ) == 0
    assert not mirrored.exists()


def test_a_deleted_asset_takes_its_body_with_it(baseline):
    """``notebook_assets`` reaches its bytes by STEM (``<id>.*``), not through
    a path column -- the same asymmetry the exporter registers in
    ``incremental._STEM_TABLES``."""
    repo = baseline["source"]["repo"]
    _seed_asset(repo, baseline["source"]["settings"], baseline["alpha"], "asset-1")
    carrier = _window(baseline)
    _import(baseline["target"], carrier.package_dir)
    mirrored = (
        Path(baseline["target"]["settings"].storage_dir)
        / "assets" / baseline["alpha"] / "asset-1.png"
    )
    assert mirrored.is_file(), "the window carried the asset body across"
    with repo._write() as db:
        db.execute("DELETE FROM notebook_assets WHERE id=?", ("asset-1",))

    window = _window(baseline)
    _import(baseline["target"], window.package_dir)

    assert _count(
        baseline["target"]["repo"],
        "SELECT COUNT(*) FROM notebook_assets WHERE id=?", ("asset-1",),
    ) == 0
    assert not mirrored.exists()


def test_a_deleted_memory_is_replayed_by_key(baseline):
    """§5's promise, finally kept. ``memory_items`` and its children are
    exempt from the snapshot sweep because a sweep cannot tell a source-side
    deletion from a target-side creation -- a replayed KEY can, so the window
    deletes exactly the mirrored memory and nothing else."""
    repo = baseline["source"]["repo"]
    memory = _one(
        repo, "SELECT id FROM memory_items WHERE notebook_id=?",
        (baseline["alpha"],),
    )["id"]
    mirror = baseline["target"]["repo"]
    assert _count(mirror, "SELECT COUNT(*) FROM memory_items WHERE id=?", (memory,)) == 1
    with mirror._write() as db:
        # A memory the TARGET's own user made on the mirror, which must
        # survive a window that deletes the source's.
        db.execute(
            "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,"
            "promotion_state,title,content_md,tags_json,created_at,updated_at) "
            "VALUES(?,?,'user-local','external_agent','candidate','none','local',"
            "'body','[]',?,?)",
            ("mem-local", baseline["alpha"], MOMENT, MOMENT),
        )
    with repo._write() as db:
        db.execute("DELETE FROM memory_items WHERE id=?", (memory,))

    window = _window(baseline)
    report = _import(baseline["target"], window.package_dir)

    assert report.error == ""
    assert _count(mirror, "SELECT COUNT(*) FROM memory_items WHERE id=?", (memory,)) == 0
    assert _count(
        mirror, "SELECT COUNT(*) FROM memory_items WHERE id=?", ("mem-local",)
    ) == 1


def test_a_seed_only_revocation_never_reaches_the_mirror(baseline):
    """The other half of §5: the target owns membership and sharing decisions
    after the first import. The exporter refuses to write such a delete and
    ``_verify_delete_scopes`` refuses to read one, so a revoked grant at the
    source leaves the mirror's grant alone.

    The grant rides in on a notebook the WINDOW creates, because a seed_only
    row is only ever seeded together with a parent this import created
    (``_SEED_PARENT``) -- which doubles as the check that a window can create
    a notebook from scratch at all."""
    repo = baseline["source"]["repo"]
    fresh = _seed_notebook(repo, "gamma")
    with repo._write() as db:
        db.execute(
            "INSERT INTO notebook_grants(id,notebook_id,principal_type,"
            "principal_id,role,created_by,created_at) "
            "VALUES(?,?,'everyone','','reader','user-local',?)",
            ("grant-x", fresh, MOMENT),
        )
    carrier = _window(baseline)
    assert _import(baseline["target"], carrier.package_dir).error == ""
    mirror = baseline["target"]["repo"]
    assert _one(
        mirror, "SELECT status FROM notebooks WHERE id=?", (fresh,)
    )["status"] == "draft"
    assert _count(
        mirror, "SELECT COUNT(*) FROM notebook_grants WHERE id=?", ("grant-x",)
    ) == 1
    with repo._write() as db:
        db.execute("DELETE FROM notebook_grants WHERE id=?", ("grant-x",))

    window = _window(baseline)
    _import(baseline["target"], window.package_dir)

    assert {entry["table"] for entry in _deletes(window.package_dir)}.isdisjoint(
        {"notebook_grants", "group_members", "notebook_members"}
    )
    assert _count(
        mirror, "SELECT COUNT(*) FROM notebook_grants WHERE id=?", ("grant-x",)
    ) == 1


# --------------------------------------------- 3d: notebook delete propagation


def test_a_deleted_notebook_is_tombstoned_and_queued_not_deleted(baseline):
    """An imported notebook deletion is the same operation the HTTP route
    performs: flip to ``deleting`` and insert a ``queued`` job row, in ONE
    transaction. The import never deletes the row itself -- the six-phase
    runner archives activity and clears storage first.

    变异验证: 去掉 3d 的作业行 INSERT, 本条报红 (笔记本被墓碑化却没有作业)。"""
    repo = baseline["source"]["repo"]
    with repo._write() as db:
        db.execute("DELETE FROM notebooks WHERE id=?", (baseline["beta"],))

    window = _window(baseline)
    assert _manifest(window.package_dir)["deleted_notebooks"] == [baseline["beta"]]
    report = _import(baseline["target"], window.package_dir)

    assert report.error == ""
    assert report.notebooks_deleted == (baseline["beta"],)
    assert report.notebooks_delete_skipped == ()
    mirror = baseline["target"]["repo"]
    assert _one(
        mirror, "SELECT status FROM notebooks WHERE id=?", (baseline["beta"],)
    )["status"] == "deleting"
    job = _one(
        mirror,
        "SELECT id,status,phase,attempts FROM notebook_delete_jobs "
        "WHERE notebook_id=?",
        (baseline["beta"],),
    )
    assert job is not None
    assert (job["status"], job["phase"], job["attempts"]) == ("queued", "mark", 0)
    assert str(job["id"]).startswith("ndj-")


def test_a_notebook_being_copied_at_the_target_is_skipped_not_tombstoned(baseline):
    """A deep copy in flight here owns that row's lifecycle. The import does
    not fight it and does not wait: it says so in the report instead."""
    repo = baseline["source"]["repo"]
    with repo._write() as db:
        db.execute("DELETE FROM notebooks WHERE id=?", (baseline["beta"],))
    with baseline["target"]["repo"]._write() as db:
        db.execute(
            "UPDATE notebooks SET status='copying' WHERE id=?", (baseline["beta"],)
        )

    window = _window(baseline)
    report = _import(baseline["target"], window.package_dir)

    assert report.notebooks_deleted == ()
    assert report.notebooks_delete_skipped == (baseline["beta"],)
    assert _one(
        baseline["target"]["repo"], "SELECT status FROM notebooks WHERE id=?",
        (baseline["beta"],),
    )["status"] == "copying"
    assert _count(
        baseline["target"]["repo"],
        "SELECT COUNT(*) FROM notebook_delete_jobs WHERE notebook_id=?",
        (baseline["beta"],),
    ) == 0


def test_deleting_a_notebook_the_target_owns_locally_is_refused(baseline):
    """The mirror fence, on the destructive side. A notebook id is not proof
    of anything, and a delete job is not undoable once its runner starts.

    Refused in PREFLIGHT, which the dry run is what proves: a dry run stops
    after identity mapping and writes nothing, so a refusal there can only
    have come from ``_assert_deleted_notebooks_are_ours``. Phase 3d asks the
    same question again inside its own transaction, but by then the run has
    already applied every row in the window.

    变异验证: 去掉 ``_assert_deleted_notebooks_are_ours``, 本条的 dry-run 断言
    报红 (3d 的复查仍会拦住非 dry-run 的那一半)。"""
    repo = baseline["source"]["repo"]
    with repo._write() as db:
        db.execute("DELETE FROM notebooks WHERE id=?", (baseline["beta"],))
    with baseline["target"]["repo"]._write() as db:
        db.execute(
            "UPDATE notebooks SET sync_origin='' WHERE id=?", (baseline["beta"],)
        )

    window = _window(baseline)
    with pytest.raises(SyncImportError) as dry:
        _import(baseline["target"], window.package_dir, dry_run=True)
    assert baseline["beta"] in str(dry.value)

    with pytest.raises(SyncImportError) as failure:
        _import(baseline["target"], window.package_dir)

    assert baseline["beta"] in str(failure.value)
    assert _one(
        baseline["target"]["repo"], "SELECT status FROM notebooks WHERE id=?",
        (baseline["beta"],),
    )["status"] != "deleting"


# ------------------------------------------------------ delete scope guards


def test_a_delete_aimed_at_another_notebooks_row_refuses_the_whole_import(
    baseline
):
    """The guard this phase exists to have. Ids are exported verbatim and
    never reissued, so a package can name a key the target holds under a
    completely different notebook -- and a DELETE by bare key would destroy
    it on the package's say-so.

    变异验证: 去掉 ``_replay_delete_table`` 的归属校验 (``owner not in
    allowed`` 那一条), 本条报红。"""
    repo = baseline["source"]["repo"]
    with repo._write() as db:
        db.execute(
            "UPDATE notebooks SET name='alpha v2' WHERE id=?", (baseline["alpha"],)
        )
    window = _window(baseline)
    victim = _one(
        baseline["target"]["repo"],
        "SELECT id FROM chunks WHERE notebook_id=? LIMIT 1", (baseline["beta"],),
    )["id"]
    # beta is not in this window's notebook set, so its chunk is out of scope.
    assert baseline["beta"] not in _manifest(window.package_dir)["notebooks"]
    _write_deletes(
        window.package_dir,
        [
            *_deletes(window.package_dir),
            {"table": "chunks", "key": {"id": victim}, "notebook_id": None,
             "parent_key": None},
        ],
    )
    _reseal(window.package_dir)

    with pytest.raises(SyncImportError) as failure:
        _import(baseline["target"], window.package_dir)

    message = str(failure.value)
    assert "chunks" in message and baseline["beta"] in message
    assert _count(
        baseline["target"]["repo"], "SELECT COUNT(*) FROM chunks WHERE id=?",
        (victim,),
    ) == 1


def test_an_orphan_delete_that_names_no_notebook_is_a_no_op(baseline):
    """§11's registered rule, now implemented: a PARENT-scoped entry whose
    chain is broken at the target and that carries no notebook of its own
    cannot be attributed, so it is skipped and counted rather than guessed.

    变异验证: 让孤儿分支直接删除而不是计数, 本条报红。"""
    repo = baseline["source"]["repo"]
    with repo._write() as db:
        db.execute(
            "UPDATE notebooks SET name='alpha v2' WHERE id=?", (baseline["alpha"],)
        )
    window = _window(baseline)
    mirror = baseline["target"]["repo"]
    row_id = _one(mirror, "SELECT id FROM knowhow_rows LIMIT 1")["id"]
    column_id = _one(
        mirror, "SELECT id FROM knowhow_columns WHERE table_id="
        "(SELECT table_id FROM knowhow_rows WHERE id=?) LIMIT 1",
        (row_id,),
    )["id"]
    _write_deletes(
        window.package_dir,
        [
            *_deletes(window.package_dir),
            # A knowhow_cell_code row whose parent knowhow_rows row does not
            # exist at the target: unresolvable, and the entry names nothing.
            {"table": "knowhow_cell_code", "key": {"id": "kcc-orphan"},
             "notebook_id": None, "parent_key": None},
        ],
    )
    _reseal(window.package_dir)
    with mirror._write() as db:
        db.execute(
            "INSERT INTO knowhow_cell_code(id,row_id,column_id,code_text,"
            "language,updated_by,cell_content_hash,created_at,updated_at) "
            "VALUES(?,?,?,'x','py','user-local','',?,?)",
            ("kcc-orphan", row_id, column_id, MOMENT, MOMENT),
        )
    # Break the chain WITHOUT removing the row. Through the repository the
    # parent DELETE would cascade this row away (which is the ``absent``
    # case, not the orphan one), so the surgery goes through a second
    # connection with SQLite's default foreign_keys=OFF -- the state a
    # restored-from-backup or hand-repaired mirror can genuinely be in, and
    # the one this branch exists for.
    _without_foreign_keys(
        baseline["target"]["settings"],
        "DELETE FROM knowhow_rows WHERE id=?",
        (row_id,),
    )

    report = _import(baseline["target"], window.package_dir)

    assert report.error == ""
    assert report.deletes_orphan_skipped == 1
    assert _count(
        mirror, "SELECT COUNT(*) FROM knowhow_cell_code WHERE id=?",
        ("kcc-orphan",),
    ) == 1


def test_a_delete_from_a_seed_only_table_is_refused(baseline):
    """The exporter never writes one; ``manifest.json`` is not the only thing
    that can lie, so the importer refuses one independently."""
    window = _prepared_window(baseline)
    _write_deletes(
        window.package_dir,
        [
            *_deletes(window.package_dir),
            {"table": "notebook_grants", "key": {"id": "g-anything"},
             "notebook_id": baseline["alpha"], "parent_key": None},
        ],
    )
    _reseal(window.package_dir)

    with pytest.raises(SyncImportError) as failure:
        _import(baseline["target"], window.package_dir)

    assert "seed_only" in str(failure.value)


def test_a_delete_whose_key_columns_do_not_match_is_refused(baseline):
    """A missing key column would widen the DELETE's predicate from one row to
    a whole partition.

    变异验证: 去掉 ``_verify_delete_scopes`` 的键列集相等断言, 本条报红。"""
    window = _prepared_window(baseline)
    _write_deletes(
        window.package_dir,
        [
            {"table": "chunk_elements",
             "key": {"chunk_id": "ck-anything"},
             "notebook_id": baseline["alpha"], "parent_key": None},
        ],
    )
    _reseal(window.package_dir)

    with pytest.raises(SyncImportError) as failure:
        _import(baseline["target"], window.package_dir)

    message = str(failure.value)
    assert "chunk_elements" in message and "synchronization key" in message


def test_a_delete_attributed_outside_the_package_is_refused_in_preflight(baseline):
    """Cheaper than the per-row ownership check and independent of it: an
    entry that ADMITS to belonging elsewhere is refused before a single write
    transaction opens."""
    window = _prepared_window(baseline)
    _write_deletes(
        window.package_dir,
        [
            {"table": "chunks", "key": {"id": "ck-anything"},
             "notebook_id": "nb-not-in-this-package", "parent_key": None},
        ],
    )
    _reseal(window.package_dir)

    with pytest.raises(SyncImportError) as failure:
        _import(baseline["target"], window.package_dir)

    assert "nb-not-in-this-package" in str(failure.value)


def test_the_manifests_deleted_notebooks_must_match_the_deletes_file(baseline):
    """``manifest.json`` is not checksummed, so a crafted one could WIDEN the
    set of notebooks every other delete entry is allowed to touch simply by
    listing an id. ``deletes.jsonl`` is checksummed and is the authority.

    变异验证: 去掉这条相等断言, 本条报红。"""
    window = _prepared_window(baseline)
    document = _manifest(window.package_dir)
    document["deleted_notebooks"] = ["nb-invented"]
    _write_manifest(window.package_dir, document)

    with pytest.raises(SyncImportError) as failure:
        _import(baseline["target"], window.package_dir)

    message = str(failure.value)
    assert "deleted_notebooks" in message and "nb-invented" in message


def _prepared_window(baseline) -> ExportReport:
    """A real window with one harmless row change in it, for the tests that
    then splice a crafted delete entry into it."""
    with baseline["source"]["repo"]._write() as db:
        db.execute(
            "UPDATE notebooks SET name='alpha v2' WHERE id=?", (baseline["alpha"],)
        )
    return _window(baseline)


# ------------------------------------------------------------- chain rules


def test_a_window_whose_base_was_never_imported_is_refused(baseline, tmp_path):
    window = _prepared_window(baseline)
    document = _manifest(window.package_dir)
    document["base_package_id"] = "pkg-never-seen"
    _write_manifest(window.package_dir, document)

    with pytest.raises(SyncImportError) as failure:
        _import(baseline["target"], window.package_dir)

    message = str(failure.value)
    assert "pkg-never-seen" in message
    assert "never applied it" in message


def test_a_window_is_refused_once_a_later_window_has_been_applied(baseline):
    """The chain-head rule. Re-applying an earlier window on top of a later
    one rolls those rows back to the state the earlier window ends at.

    变异验证: 去掉 ``_assert_chain`` 的 ``downstream`` 拒绝, 本条报红。"""
    repo = baseline["source"]["repo"]
    with repo._write() as db:
        db.execute(
            "UPDATE notebooks SET name='alpha v2' WHERE id=?", (baseline["alpha"],)
        )
    first = _window(baseline)
    _import(baseline["target"], first.package_dir)
    with repo._write() as db:
        db.execute(
            "UPDATE notebooks SET name='alpha v3' WHERE id=?", (baseline["alpha"],)
        )
    second = _window(baseline)
    _import(baseline["target"], second.package_dir)
    # Now offer the FIRST window's content again under a fresh package id.
    replay = first.package_dir.parent / "replay"
    _copy_package(first.package_dir, replay)
    document = _manifest(replay)
    document["package_id"] = "pkg-replayed-window"
    _write_manifest(replay, document)

    with pytest.raises(SyncImportError) as failure:
        _import(baseline["target"], replay)

    message = str(failure.value)
    assert second.package_id in message
    assert "roll those notebooks back" in message
    assert _one(
        baseline["target"]["repo"], "SELECT name FROM notebooks WHERE id=?",
        (baseline["alpha"],),
    )["name"] == "alpha v3"


def test_a_window_is_refused_while_a_failed_package_is_outstanding(baseline):
    """An incremental package carries only its own window, so it can never
    stand in for what a half-applied run was going to apply -- it is refused
    rather than allowed to supersede it.

    变异验证: 去掉 ``_assert_chain`` 的 ``outstanding`` 拒绝, 本条报红。"""
    window = _prepared_window(baseline)
    with baseline["target"]["repo"]._write() as db:
        db.execute(
            "INSERT INTO sync_imports(package_id,source_env,from_seq,to_seq,"
            "status,started_at,finished_at,report_json) "
            "VALUES(?,?,0,0,'failed',?,?,?)",
            ("pkg-half-applied", SOURCE_ENV, MOMENT, MOMENT, "{}"),
        )

    with pytest.raises(SyncImportError) as failure:
        _import(baseline["target"], window.package_dir)

    message = str(failure.value)
    assert "pkg-half-applied" in message
    assert "--resume" in message


def test_a_notebook_scoped_full_import_does_not_break_the_chain(baseline):
    """A ``--notebook`` export does not advance the source's watermark, so the
    source keeps building windows from the same base. Importing one at the
    target must therefore not make the next window unimportable.

    变异验证: 把 ``downstream_of`` 换成「created_at 更新就算下游」, 本条报红。"""
    repo = baseline["source"]["repo"]
    with repo._write() as db:
        db.execute(
            "UPDATE notebooks SET name='alpha v2' WHERE id=?", (baseline["alpha"],)
        )
    scoped = _export(
        baseline["source"], baseline["out"] / "scoped",
        notebook_ids=[baseline["beta"]],
    )
    assert scoped.mode == MODE_FULL and scoped.to_seq == 0
    scoped_report = _import(baseline["target"], scoped.package_dir)
    assert scoped_report.error == ""

    window = _window(baseline)
    report = _import(baseline["target"], window.package_dir)

    assert report.error == ""
    assert report.base_package_id == baseline["full"].package_id
    assert _one(
        baseline["target"]["repo"], "SELECT name FROM notebooks WHERE id=?",
        (baseline["alpha"],),
    )["name"] == "alpha v2"


def test_an_empty_window_is_applied_and_becomes_the_chain_head(baseline):
    """A window with no rows and no deletes is a real window: it records that
    the chain advanced past a quiet stretch. It has to be applied, recorded
    done, and be what the NEXT window continues from."""
    empty = _window(baseline)
    assert _deletes(empty.package_dir) == []

    report = _import(baseline["target"], empty.package_dir)

    assert report.error == ""
    assert report.mode == MODE_INCREMENTAL
    assert report.deletes_applied == 0
    with baseline["source"]["repo"]._write() as db:
        db.execute(
            "UPDATE notebooks SET name='alpha v2' WHERE id=?", (baseline["alpha"],)
        )
    following = _window(baseline)
    assert following.base_package_id == empty.package_id

    assert _import(baseline["target"], following.package_dir).error == ""


def test_the_stored_report_records_the_mode_and_base_of_every_row(baseline):
    """The chain rules read these back out of ``sync_imports.report_json``;
    nothing else persists them (``from_seq``/``to_seq`` are real columns).

    变异验证: 从 ``ImportReport.as_json`` 里去掉 ``base_package_id``, 本条报红。"""
    window = _prepared_window(baseline)
    _import(baseline["target"], window.package_dir)

    rows = {}
    with baseline["target"]["repo"]._connect() as db:
        for row in db.execute(
            "SELECT package_id, status, to_seq, report_json FROM sync_imports"
        ):
            rows[str(row["package_id"])] = (
                str(row["status"]), int(row["to_seq"]),
                json.loads(row["report_json"]),
            )
    assert rows[baseline["full"].package_id][2]["mode"] == MODE_FULL
    status, to_seq, document = rows[window.package_id]
    assert status == "done"
    assert to_seq == window.to_seq
    assert document["mode"] == MODE_INCREMENTAL
    assert document["base_package_id"] == baseline["full"].package_id


# ----------------------------------------------------------- files & resume


def test_the_file_phase_merges_instead_of_replacing(baseline):
    """A window carries only the files its changed rows point at, so the full
    package's directory swap would delete everything it does not mention. It
    must add the new bytes, leave the untouched ones alone, and leave no
    ``.sync-old`` behind.

    变异验证: 让 ``_import`` 对增量包也调用 ``_install_files``, 本条报红。"""
    repo = baseline["source"]["repo"]
    storage = Path(baseline["target"]["settings"].storage_dir)
    untouched = sorted(
        (storage / "notebooks" / baseline["alpha"]).iterdir()
    )
    assert untouched, "the baseline installed alpha's uploads"
    before = {path.name: path.read_bytes() for path in untouched}
    new_source = _upload(repo, baseline["alpha"], "second.txt")

    window = _window(baseline)
    report = _import(baseline["target"], window.package_dir)

    assert report.error == ""
    directory = storage / "notebooks" / baseline["alpha"]
    after = {path.name: path.read_bytes() for path in sorted(directory.iterdir())}
    for name, body in before.items():
        assert after[name] == body, f"{name} was rewritten by a merge"
    assert len(after) == len(before) + 1
    mirrored = Path(
        _one(
            baseline["target"]["repo"], "SELECT file_path FROM sources WHERE id=?",
            (new_source,),
        )["file_path"]
    )
    assert mirrored.is_file()
    leftovers = [
        path.name
        for path in directory.parent.iterdir()
        if ".sync-old" in path.name or path.name.endswith(".sync-tmp")
    ]
    assert leftovers == []


def test_a_run_interrupted_inside_the_delete_phase_resumes(baseline):
    """Phase 3c writes one ``__delete__:<table>`` progress step per table in
    the same transaction as that table's deletes, so a crash loses at most the
    table in flight -- and the resume must redo only that one.

    变异验证: 去掉 3c 的 ``if step in done: continue``, 本条的 warning 断言
    仍绿但重放会重做已完成的表; 去掉 ``_record_progress``, 本条报红。"""
    repo = baseline["source"]["repo"]
    chunk = _one(
        repo, "SELECT id FROM chunks WHERE notebook_id=? LIMIT 1",
        (baseline["alpha"],),
    )["id"]
    with repo._write() as db:
        db.execute("DELETE FROM chunk_elements WHERE chunk_id=?", (chunk,))
        db.execute("DELETE FROM chunks WHERE id=?", (chunk,))
        db.execute("DELETE FROM chunks_fts WHERE chunk_id=?", (chunk,))
    window = _window(baseline)

    from app.migration.sync import import_ as import_module

    original = import_module._replay_delete_table
    state = {"seen": 0}

    def explode(backend, conn, table, context):
        outcome = original(backend, conn, table, context)
        if table == "chunk_elements":
            state["seen"] += 1
            raise RuntimeError("boom mid-delete-phase")
        return outcome

    import_module._replay_delete_table = explode
    try:
        with pytest.raises(SyncImportError):
            _import(baseline["target"], window.package_dir)
    finally:
        import_module._replay_delete_table = original
    assert state["seen"] == 1
    steps = {
        str(row["table_name"])
        for row in _all(
            baseline["target"]["repo"],
            "SELECT table_name FROM sync_import_progress WHERE package_id=?",
            (window.package_id,),
        )
    }
    assert "__delete__:chunk_elements" not in steps
    assert any(step.startswith("__delete__:") for step in steps)

    report = _import(baseline["target"], window.package_dir, resume=True)

    assert report.error == ""
    assert _count(
        baseline["target"]["repo"], "SELECT COUNT(*) FROM chunks WHERE id=?",
        (chunk,),
    ) == 0


def test_a_dry_run_reports_the_mode_and_writes_nothing(baseline):
    window = _prepared_window(baseline)

    report = _import(baseline["target"], window.package_dir, dry_run=True)

    assert report.dry_run
    assert report.mode == MODE_INCREMENTAL
    assert report.base_package_id == baseline["full"].package_id
    assert report.deletes_applied == 0
    assert _count(
        baseline["target"]["repo"],
        "SELECT COUNT(*) FROM sync_imports WHERE package_id=?",
        (window.package_id,),
    ) == 0
    assert _one(
        baseline["target"]["repo"], "SELECT name FROM notebooks WHERE id=?",
        (baseline["alpha"],),
    )["name"] == "alpha"


def _all(repo, statement: str, params=()):
    with repo._connect() as db:
        return [dict(row) for row in db.execute(statement, params).fetchall()]


def _copy_package(origin: Path, destination: Path) -> None:
    import shutil

    shutil.copytree(origin, destination)


def _without_foreign_keys(settings: Settings, statement: str, params=()) -> None:
    """Run one statement on a second connection with SQLite's default
    ``foreign_keys=OFF``, so a test can produce a broken parent chain the
    repository's own connections would refuse to create."""
    import sqlite3

    path = str(settings.database_url).removeprefix("sqlite:///")
    connection = sqlite3.connect(path)
    try:
        connection.execute(statement, params)
        connection.commit()
    finally:
        connection.close()
