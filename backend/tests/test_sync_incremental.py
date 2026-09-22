"""守卫: 按变更日志读的增量导出 (app.migration.sync.incremental +
app.migration.sync.export 的模式判定)。对应 docs/incremental-sync-design.md
§7「增量窗口」与 §8 的包格式 v2。

本文件是 SQLite 泳道。SQLite 没有「已发号未提交」的窗口, 所以补偿窗口那一半
只能在 PostgreSQL 泳道 (tests/postgres/test_sync_incremental_pg.py) 里真的测到;
这里盯的是两端共有的那部分: 模式判定、窗口压缩、归属解析、seed_only 与镜像
过滤、deletes、文件只含变更行、空窗口、水位推进与限定导出不推水位。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.config import Settings
from app.migration.sync import export as export_module
from app.migration.sync.export import ExportReport, export_notebooks
from app.migration.sync.import_ import SyncImportError
from app.migration.sync.package import (
    DELETES_NAME,
    KG_EPOCHS_NAME,
    MANIFEST_NAME,
    MODE_FULL,
    MODE_INCREMENTAL,
    PACKAGE_FORMAT_VERSION,
    rows_path,
)
from app.models.schemas import NotebookCreate
from app.repositories.ports import UploadedSourceFile
from app.services.sqlite_repository import SQLiteRepository


SOURCE_ENV = "dev"
TARGET_ENV = "prod"
MOMENT = "2026-01-01T00:00:00+00:00"


# ------------------------------------------------------------------- seeds


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'sync.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return Settings()


@pytest.fixture
def repo(settings):
    repository = SQLiteRepository(settings)
    try:
        yield repository
    finally:
        repository.close()


def _upload(repo, notebook_id: str, name: str) -> str:
    result = repo.upload_sources(
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
    del result
    with repo._connect() as db:
        row = db.execute(
            "SELECT id FROM sources WHERE notebook_id=? AND title LIKE ? "
            "ORDER BY created_at DESC LIMIT 1",
            (notebook_id, f"%{Path(name).stem}%"),
        ).fetchone()
    assert row is not None, f"upload of {name} produced no sources row"
    return str(row["id"])


def _grant_group(repo, notebook_id: str, group_id: str) -> None:
    with repo._write() as db:
        db.execute(
            "INSERT OR IGNORE INTO users(id, email, display_name, role, status, "
            "username, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("member-1", "m@example.invalid", "M", "member", "active",
             "member-1", MOMENT, MOMENT),
        )
        db.execute(
            "INSERT INTO groups(id,name,kind,description,created_by,created_at,"
            "updated_at,owner_id) VALUES(?,?,?,?,?,?,?,?)",
            (group_id, group_id, "team", "", "user-local", MOMENT, MOMENT, "member-1"),
        )
        db.execute(
            "INSERT INTO group_members(group_id,user_id,role,added_at,added_by) "
            "VALUES(?,?,?,?,?)",
            (group_id, "member-1", "admin", MOMENT, "user-local"),
        )
        db.execute(
            "INSERT INTO notebook_grants(id,notebook_id,principal_type,principal_id,"
            "role,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
            (f"g-{group_id}", notebook_id, "group", group_id, "reader",
             "user-local", MOMENT),
        )


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
    _grant_group(repo, notebook.id, f"grp-{name}")
    return notebook.id


def _enable_capture(repo) -> None:
    """Turn the source-side capture gate on. Written directly rather than
    through ``sync capture enable``: that command also clears the watermark
    and the log, which is exactly what these tests are setting up by hand."""
    with repo._write() as db:
        db.execute(
            "INSERT INTO sync_capture_control (singleton, enabled, enabled_at) "
            "VALUES (1, 1, ?) ON CONFLICT (singleton) DO UPDATE SET enabled = 1",
            (MOMENT,),
        )


def _close_capture(repo) -> None:
    with repo._write() as db:
        db.execute("UPDATE sync_capture_control SET enabled = 0 WHERE singleton = 1")


# ---------------------------------------------------------------- helpers


def _export(settings, out_dir: Path, notebook_ids=None, *, full=False) -> ExportReport:
    return export_notebooks(
        settings,
        target_env=TARGET_ENV,
        out_dir=out_dir,
        notebook_ids=notebook_ids,
        source_env=SOURCE_ENV,
        full=full,
    )


def _rows(package: Path, table: str) -> list[dict]:
    text = (package / rows_path(table)).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def _ids(package: Path, table: str, column: str = "id") -> set[str]:
    return {str(row[column]) for row in _rows(package, table)}


def _deletes(package: Path) -> list[dict]:
    text = (package / DELETES_NAME).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def _manifest(package: Path) -> dict:
    return json.loads((package / MANIFEST_NAME).read_text(encoding="utf-8"))


def _watermark(repo) -> dict:
    with repo._connect() as db:
        row = db.execute(
            "SELECT * FROM sync_export_state WHERE target_env=?", (TARGET_ENV,)
        ).fetchone()
    return dict(row) if row is not None else {}


@pytest.fixture
def baseline(repo, settings, tmp_path):
    """Capture on, one full baseline export taken, two notebooks seeded. Every
    incremental test starts here: the watermark this leaves behind is what the
    next export resumes from."""
    first = _seed_notebook(repo, "alpha")
    second = _seed_notebook(repo, "beta")
    _enable_capture(repo)
    report = _export(settings, tmp_path / "out")
    assert report.mode == MODE_FULL
    return {
        "repo": repo,
        "settings": settings,
        "out": tmp_path / "out",
        "alpha": first,
        "beta": second,
        "baseline": report,
    }


# ------------------------------------------------------------ mode decision


def test_the_export_after_a_captured_baseline_is_incremental(baseline):
    report = _export(baseline["settings"], baseline["out"])

    assert report.mode == MODE_INCREMENTAL
    assert report.from_seq == baseline["baseline"].to_seq + 1
    assert report.base_package_id == baseline["baseline"].package_id
    assert _manifest(report.package_dir)["mode"] == MODE_INCREMENTAL
    assert _manifest(report.package_dir)["format_version"] == PACKAGE_FORMAT_VERSION


def test_a_closed_capture_gate_forces_a_full_export(baseline):
    """The watermark is still there and still says ``captured = 1``, but the
    gate is off NOW: rows can have changed since it closed with no log entry
    to prove it, so resuming from that watermark would skip them silently.

    变异验证: 去掉模式判定里的 ``gate_open``, 本条必须报红。"""
    _close_capture(baseline["repo"])

    report = _export(baseline["settings"], baseline["out"])

    assert report.mode == MODE_FULL
    # ...and the watermark it writes says so, so the export after it is full too.
    assert _watermark(baseline["repo"])["captured"] == 0


def test_an_uncaptured_watermark_forces_a_full_export(baseline):
    """A watermark row left by a pre-v85/0065 database (or by an export taken
    with the gate closed) reads ``captured = 0``: it records how far that
    export saw but promises nothing about the log behind it.

    变异验证: 去掉模式判定里的 ``watermark.captured``, 本条必须报红。"""
    with baseline["repo"]._write() as db:
        db.execute(
            "UPDATE sync_export_state SET captured = 0 WHERE target_env = ?",
            (TARGET_ENV,),
        )

    assert _export(baseline["settings"], baseline["out"]).mode == MODE_FULL


def test_no_watermark_at_all_forces_a_full_export(repo, settings, tmp_path):
    _seed_notebook(repo, "alpha")
    _enable_capture(repo)

    report = _export(settings, tmp_path / "out")

    assert report.mode == MODE_FULL
    assert report.from_seq == 0
    assert report.base_package_id == ""


def test_full_overrides_a_usable_watermark(baseline):
    report = _export(baseline["settings"], baseline["out"], full=True)

    assert report.mode == MODE_FULL
    assert report.scoped is False
    assert report.watermark_advanced is True
    # A re-baseline still advances the watermark -- that is what makes the
    # export AFTER it incremental again.
    assert _watermark(baseline["repo"])["package_id"] == report.package_id


# ------------------------------------------- scoped exports and the watermark


def test_a_scoped_export_is_full_and_never_moves_the_watermark(baseline):
    """``--notebook`` names a subset, so its watermark would be a lie about
    every OTHER notebook: their changes would fall below the next window's
    floor and never be picked up again.

    变异验证: 让限定导出也调用 ``_advance_watermark``, 本条必须报红。"""
    before = _watermark(baseline["repo"])

    report = _export(baseline["settings"], baseline["out"], [baseline["alpha"]])

    assert report.mode == MODE_FULL
    assert report.scoped is True
    assert report.watermark_advanced is False
    assert (report.from_seq, report.to_seq, report.base_package_id) == (0, 0, "")
    assert report.package_dir.name.startswith(f"sync-{SOURCE_ENV}-0-0-")
    assert _watermark(baseline["repo"]) == before


def test_full_and_scoped_together_still_do_not_move_the_watermark(baseline):
    before = _watermark(baseline["repo"])

    report = _export(
        baseline["settings"], baseline["out"], [baseline["alpha"]], full=True
    )

    assert report.scoped is True
    assert report.watermark_advanced is False
    assert _watermark(baseline["repo"]) == before


def test_a_scoped_export_does_not_swallow_another_notebooks_window(baseline):
    """The point of the rule above, end to end: change beta, take a scoped
    export of alpha, and the NEXT unscoped export must still be incremental
    from the original watermark AND carry beta's change."""
    repo = baseline["repo"]
    with repo._write() as db:
        db.execute(
            "UPDATE notebooks SET name = ? WHERE id = ?", ("beta renamed", baseline["beta"])
        )

    _export(baseline["settings"], baseline["out"], [baseline["alpha"]])
    report = _export(baseline["settings"], baseline["out"])

    assert report.mode == MODE_INCREMENTAL
    assert report.from_seq == baseline["baseline"].to_seq + 1
    names = {row["id"]: row["name"] for row in _rows(report.package_dir, "notebooks")}
    assert names.get(baseline["beta"]) == "beta renamed"


# ------------------------------------------------------- window and contents


def test_an_incremental_package_carries_only_the_changed_rows(baseline):
    repo = baseline["repo"]
    with repo._write() as db:
        db.execute(
            "UPDATE notebooks SET name = ? WHERE id = ?", ("alpha v2", baseline["alpha"])
        )

    report = _export(baseline["settings"], baseline["out"])

    assert _ids(report.package_dir, "notebooks") == {baseline["alpha"]}
    assert _rows(report.package_dir, "notebooks")[0]["name"] == "alpha v2"
    # Nothing else moved, so nothing else travels.
    assert _rows(report.package_dir, "chunks") == []
    assert _rows(report.package_dir, "sources") == []
    assert report.notebooks == (baseline["alpha"],)


def test_a_deleted_row_becomes_a_delete_entry(baseline):
    repo = baseline["repo"]
    with repo._connect() as db:
        chunk = db.execute(
            "SELECT id FROM chunks WHERE notebook_id=? ORDER BY id LIMIT 1",
            (baseline["alpha"],),
        ).fetchone()
    assert chunk is not None
    with repo._write() as db:
        db.execute("DELETE FROM chunks WHERE id = ?", (chunk["id"],))

    report = _export(baseline["settings"], baseline["out"])

    entries = [entry for entry in _deletes(report.package_dir) if entry["table"] == "chunks"]
    assert entries == [
        {
            "table": "chunks",
            "key": {"id": chunk["id"]},
            "notebook_id": baseline["alpha"],
            "parent_key": None,
        }
    ]
    assert report.deletes == len(_deletes(report.package_dir))
    assert _rows(report.package_dir, "chunks") == []


def test_a_new_parent_scoped_row_is_attributed_through_its_scope_chain(baseline):
    """``knowhow_cells`` has no notebook column at all: its notebook is two
    hops away (cell -> row -> table). The change log only records the parent
    key, so the exporter has to walk that chain itself.

    变异验证: 让 PARENT 归属直接返回 None, 本条必须报红(行不进包)。"""
    repo = baseline["repo"]
    with repo._connect() as db:
        table_id = db.execute(
            "SELECT id FROM knowhow_tables WHERE notebook_id=? LIMIT 1",
            (baseline["alpha"],),
        ).fetchone()["id"]
    columns = repo.get_knowhow_table(table_id)["columns"]
    row_id = repo.add_knowhow_row(
        table_id, {column["id"]: "second row" for column in columns}, actor="user-local"
    )

    report = _export(baseline["settings"], baseline["out"])

    assert row_id in _ids(report.package_dir, "knowhow_rows")
    cells = _rows(report.package_dir, "knowhow_cells")
    assert cells and all(cell["row_id"] == row_id for cell in cells)
    assert baseline["alpha"] in report.notebooks


def test_a_global_row_change_travels_without_a_notebook(baseline):
    repo = baseline["repo"]
    with repo._write() as db:
        db.execute(
            "UPDATE groups SET description = ? WHERE id = ?",
            ("renamed", f"grp-alpha"),
        )

    report = _export(baseline["settings"], baseline["out"])

    rows = {row["id"]: row for row in _rows(report.package_dir, "groups")}
    assert rows["grp-alpha"]["description"] == "renamed"


def test_deleting_a_notebook_is_reported_and_not_listed_as_carried(baseline):
    repo = baseline["repo"]
    with repo._write() as db:
        db.execute("DELETE FROM notebooks WHERE id = ?", (baseline["alpha"],))

    report = _export(baseline["settings"], baseline["out"])

    assert report.deleted_notebooks == (baseline["alpha"],)
    assert baseline["alpha"] not in report.notebooks
    assert baseline["alpha"] not in _ids(report.package_dir, "notebooks")
    manifest = _manifest(report.package_dir)
    assert manifest["deleted_notebooks"] == [baseline["alpha"]]
    assert manifest["notebooks"] == list(report.notebooks)
    entries = [entry for entry in _deletes(report.package_dir)
               if entry["table"] == "notebooks"]
    assert entries == [
        {
            "table": "notebooks",
            "key": {"id": baseline["alpha"]},
            "notebook_id": baseline["alpha"],
            "parent_key": None,
        }
    ]
    # The notebook's child rows still travel as their own deletes: a target
    # that never had the notebook must not be left holding its chunks.
    assert any(entry["table"] == "chunks" for entry in _deletes(report.package_dir))


def test_a_seed_only_tables_delete_is_never_exported(baseline):
    """§5「授权只播种」: the target owns membership and sharing decisions made
    after the first import, so a source-side revocation must not travel.

    变异验证: 去掉 ``table_delta`` 的 seed_only 过滤, 本条必须报红。"""
    repo = baseline["repo"]
    with repo._write() as db:
        db.execute("DELETE FROM notebook_grants WHERE id = ?", ("g-grp-alpha",))
        db.execute("DELETE FROM group_members WHERE group_id = ?", ("grp-alpha",))

    report = _export(baseline["settings"], baseline["out"])

    tables = {entry["table"] for entry in _deletes(report.package_dir)}
    assert "notebook_grants" not in tables
    assert "group_members" not in tables


def test_a_mirrored_notebooks_changes_are_skipped_and_counted(baseline):
    """A mirror's content came from another source environment; re-exporting
    it would launder that environment's rows into this one's name.

    变异验证: 去掉镜像过滤, 本条必须报红。"""
    repo = baseline["repo"]
    with repo._write() as db:
        db.execute(
            "UPDATE notebooks SET sync_origin = ? WHERE id = ?",
            ("upstream", baseline["beta"]),
        )
        db.execute(
            "UPDATE notebooks SET name = ? WHERE id = ?", ("beta edited", baseline["beta"])
        )
        db.execute(
            "UPDATE notebooks SET name = ? WHERE id = ?", ("alpha edited", baseline["alpha"])
        )

    report = _export(baseline["settings"], baseline["out"])

    assert report.notebooks == (baseline["alpha"],)
    assert baseline["beta"] not in _ids(report.package_dir, "notebooks")
    assert report.skipped_mirror_changes >= 1
    assert "mirror" in report.skipped[baseline["beta"]]


# ------------------------------------------------------------- compaction


def test_many_changes_to_one_key_are_written_once(baseline):
    """Three updates of one row are ONE package row carrying the current
    value. Compaction is what makes that true -- without it the same key
    would be written once per log entry, and the importer would upsert the
    same row three times.

    Note what this does NOT pin: whether compaction keeps the first or the
    last operation. The row VALUE is read back live either way, so only a
    key whose final operation differs from its first can tell the two apart
    -- see the upsert/delete pair below."""
    repo = baseline["repo"]
    for index in range(3):
        with repo._write() as db:
            db.execute(
                "UPDATE notebooks SET name = ? WHERE id = ?",
                (f"alpha v{index}", baseline["alpha"]),
            )
    with repo._connect() as db:
        logged = db.execute(
            "SELECT COUNT(*) AS n FROM sync_change_log WHERE table_name='notebooks'"
        ).fetchone()["n"]
    assert logged >= 3, "the seeds must really produce several log rows for one key"

    report = _export(baseline["settings"], baseline["out"])

    rows = _rows(report.package_dir, "notebooks")
    assert [row["name"] for row in rows] == ["alpha v2"]


def test_insert_then_delete_inside_one_window_is_a_delete(baseline):
    """变异验证: 把压缩改成取首态而非终态, 本条必须报红 —— 首态是 upsert,
    那条路径回读不到行、降级成 delete 并留下 warning, 断言的就是这个区别。"""
    repo = baseline["repo"]
    source_id = _upload(repo, baseline["alpha"], "transient.txt")
    with repo._write() as db:
        db.execute("DELETE FROM sources WHERE id = ?", (source_id,))

    report = _export(baseline["settings"], baseline["out"])

    assert source_id not in _ids(report.package_dir, "sources")
    assert {"table": "sources", "key": {"id": source_id},
            "notebook_id": baseline["alpha"], "parent_key": None} in _deletes(
        report.package_dir
    )
    # Compaction already knew the row was deleted, so the readback downgrade
    # must never have been reached.
    assert not [w for w in report.warnings if "logged as an upsert" in w]


def test_an_upsert_whose_row_is_gone_is_downgraded_to_a_delete(baseline):
    """Should not happen -- the delete that removed the row would have been
    logged too and would have won the compaction. If it ever does, shipping a
    delete is the conservative answer: shipping nothing would leave the
    target holding a row the source no longer has. Forced here by writing a
    log entry for a key that never existed."""
    repo = baseline["repo"]
    with repo._write() as db:
        db.execute(
            "INSERT INTO sync_change_log "
            "(table_name, key_json, operation, notebook_id, changed_at) "
            "VALUES ('chunks', ?, 'upsert', ?, ?)",
            (json.dumps({"id": "ghost-chunk"}), baseline["alpha"], MOMENT),
        )

    report = _export(baseline["settings"], baseline["out"])

    assert {"table": "chunks", "key": {"id": "ghost-chunk"},
            "notebook_id": baseline["alpha"], "parent_key": None} in _deletes(
        report.package_dir
    )
    assert [w for w in report.warnings if "ghost-chunk" in w]


def test_delete_then_reinsert_inside_one_window_is_an_upsert(baseline):
    repo = baseline["repo"]
    with repo._connect() as db:
        row = dict(db.execute(
            "SELECT * FROM sources WHERE notebook_id=? LIMIT 1", (baseline["alpha"],)
        ).fetchone())
    with repo._write() as db:
        db.execute("DELETE FROM sources WHERE id = ?", (row["id"],))
        placeholders = ",".join("?" for _ in row)
        names = ",".join(f'"{name}"' for name in row)
        db.execute(
            f"INSERT INTO sources({names}) VALUES({placeholders})", tuple(row.values())
        )

    report = _export(baseline["settings"], baseline["out"])

    assert row["id"] in _ids(report.package_dir, "sources")
    assert not [
        entry for entry in _deletes(report.package_dir)
        if entry["table"] == "sources" and entry["key"] == {"id": row["id"]}
    ]


def test_kg_epoch_rows_are_ignored_by_the_window(baseline):
    """A ``kg_epoch`` log row is an event, not a row state; PR-3b does not
    fold on it and ``kg_epochs.jsonl`` stays empty (§7「压缩」)."""
    repo = baseline["repo"]
    with repo._write() as db:
        db.execute(
            "INSERT INTO sync_change_log "
            "(table_name, key_json, operation, notebook_id, changed_at) "
            "VALUES ('unified_kg_state', ?, 'kg_epoch', ?, ?)",
            (json.dumps({"notebook_id": baseline["alpha"]}), baseline["alpha"], MOMENT),
        )

    report = _export(baseline["settings"], baseline["out"])

    assert (report.package_dir / KG_EPOCHS_NAME).read_text(encoding="utf-8") == ""
    assert _manifest(report.package_dir)["kg_epochs"] == 0
    assert _rows(report.package_dir, "unified_kg_state") == []


# ------------------------------------------------------ notebooks backfill


def test_a_notebook_row_is_backfilled_for_every_notebook_the_package_carries(
    baseline,
):
    """Only a chunk changed, so ``notebooks`` itself has no log row -- but the
    package still has to declare the notebook it carries a chunk for, or
    ``manifest.notebooks`` and ``rows/notebooks.jsonl`` disagree and the
    importer's declaration check has nothing to verify the scope against.

    变异验证: 去掉 ``_backfilled_notebooks``, 本条必须报红。"""
    repo = baseline["repo"]
    with repo._write() as db:
        db.execute(
            "UPDATE chunks SET text = ? WHERE notebook_id = ?",
            ("edited body", baseline["alpha"]),
        )

    report = _export(baseline["settings"], baseline["out"])

    assert _ids(report.package_dir, "notebooks") == {baseline["alpha"]}
    assert _manifest(report.package_dir)["notebooks"] == [baseline["alpha"]]
    assert report.notebooks == (baseline["alpha"],)


# ------------------------------------------------------------------- files


def test_only_the_changed_rows_files_are_carried(baseline):
    """变异验证: 把增量文件相位换回 ``_write_files`` 的整目录复制, 本条必须报红。"""
    repo = baseline["repo"]
    _upload(repo, baseline["alpha"], "added.txt")

    report = _export(baseline["settings"], baseline["out"])

    carried = sorted(
        path.name
        for path in (report.package_dir / "files").rglob("*")
        if path.is_file()
    )
    assert any(name.endswith("added.txt") for name in carried)
    assert not any(name.endswith("alpha.txt") for name in carried)
    assert report.file_count == len(carried)


# ------------------------------------------------------- empty window & chain


def test_an_empty_window_still_produces_a_package_and_advances_the_watermark(
    baseline,
):
    first = _export(baseline["settings"], baseline["out"])
    assert first.mode == MODE_INCREMENTAL

    second = _export(baseline["settings"], baseline["out"])

    assert second.mode == MODE_INCREMENTAL
    assert second.empty is True
    assert second.deletes == 0
    assert all(count == 0 for count in second.table_counts.values())
    assert (second.package_dir / DELETES_NAME).read_text(encoding="utf-8") == ""
    assert _manifest(second.package_dir)["notebooks"] == []
    # The chain stays continuous: this package names the one before it, and
    # the watermark moved even though nothing changed.
    assert second.base_package_id == first.package_id
    assert _watermark(baseline["repo"])["package_id"] == second.package_id


def test_the_watermark_records_the_seq_the_snapshot_and_the_gate(baseline):
    """The resume point is the PAIR (seq, snapshot) plus ``captured``; a
    second export must overwrite all three, never keep the first run's.

    变异验证: 从 ``_advance_watermark`` 的 DO UPDATE SET 里删掉 ``captured``
    或 ``exported_snapshot``, 本条(或它的 PG 姊妹条)必须报红。"""
    first = _watermark(baseline["repo"])
    assert first["captured"] == 1
    # SQLite has no transaction snapshot to record and needs none -- one
    # writer at a time means seq order is commit order.
    assert first["exported_snapshot"] is None

    with baseline["repo"]._write() as db:
        db.execute(
            "UPDATE notebooks SET name='x' WHERE id=?", (baseline["alpha"],)
        )
    second_report = _export(baseline["settings"], baseline["out"])
    second = _watermark(baseline["repo"])

    assert second["exported_through_seq"] > first["exported_through_seq"]
    assert second["exported_through_seq"] == second_report.to_seq
    assert second["captured"] == 1

    _close_capture(baseline["repo"])
    _export(baseline["settings"], baseline["out"])

    third = _watermark(baseline["repo"])
    assert third["captured"] == 0
    assert third["exported_through_seq"] == 0


# ------------------------------------------------------------ import side


def test_the_importer_refuses_an_incremental_package_and_names_pr_3c(
    baseline, tmp_path
):
    with baseline["repo"]._write() as db:
        db.execute(
            "UPDATE notebooks SET name='alpha v2' WHERE id=?", (baseline["alpha"],)
        )
    report = _export(baseline["settings"], baseline["out"])
    assert report.mode == MODE_INCREMENTAL

    from app.migration.sync.import_ import _read_manifest, _reject_incremental_payload

    manifest = _read_manifest(report.package_dir)
    with pytest.raises(SyncImportError) as failure:
        _reject_incremental_payload(report.package_dir, manifest)

    message = str(failure.value)
    assert MODE_INCREMENTAL in message
    assert report.base_package_id in message
    assert f"from_seq={report.from_seq}" in message


# ------------------------------------------------------- watermark monotonic


def test_pruning_the_log_below_the_watermark_never_moves_it_backwards(baseline):
    """``sync prune-log`` deletes log rows at or below every captured
    watermark, so a quiet environment's log can end up EMPTY and ``MAX(seq)``
    reads 0 -- below the watermark it was pruned against. The watermark must
    stay where it is: letting it fall back would re-open a window over seq
    values that were already exported AND no longer exist, and the export
    after that would think it had never seen them.

    变异验证: 把 ``to_seq`` 的 ``max(..., W)`` 钳制去掉(直接用
    ``captured_through_seq``), 本条必须报红。
    """
    repo = baseline["repo"]
    with repo._write() as db:
        db.execute("UPDATE notebooks SET name='alpha v2' WHERE id=?", (baseline["alpha"],))
    first = _export(baseline["settings"], baseline["out"])
    assert first.mode == MODE_INCREMENTAL
    watermark = _watermark(repo)["exported_through_seq"]
    assert watermark == first.to_seq > 0

    with repo._write() as db:
        db.execute("DELETE FROM sync_change_log")

    second = _export(baseline["settings"], baseline["out"])

    assert second.mode == MODE_INCREMENTAL
    assert second.captured_through_seq == 0, "the log really is empty"
    assert second.to_seq == watermark
    assert second.from_seq == watermark + 1
    assert second.empty is True
    assert _watermark(repo)["exported_through_seq"] == watermark

    # ...and the chain picks up exactly where it left off. SQLite's
    # AUTOINCREMENT never reuses a seq, so the next change is numbered above
    # the watermark and lands inside the next window.
    with repo._write() as db:
        db.execute("UPDATE notebooks SET name='alpha v3' WHERE id=?", (baseline["alpha"],))
    third = _export(baseline["settings"], baseline["out"])

    assert third.from_seq == watermark + 1
    assert third.to_seq > watermark
    assert [row["name"] for row in _rows(third.package_dir, "notebooks")] == ["alpha v3"]


# ----------------------------------------------------- lifecycle status rule


def test_a_mid_copy_notebooks_changes_still_travel(baseline):
    """``notebooks.status`` is target-owned; a source-side ``copying`` is a
    moment in a local process, not a statement about what the target should
    hold. Dropping the window's changes for it while the watermark moved past
    them would lose them permanently.

    变异验证: 把 status 过滤加回 ``NotebookState``/``_classify``, 本条必须报红。
    """
    repo = baseline["repo"]
    with repo._write() as db:
        db.execute(
            "UPDATE notebooks SET status='copying', name='alpha busy' WHERE id=?",
            (baseline["alpha"],),
        )

    report = _export(baseline["settings"], baseline["out"])

    assert baseline["alpha"] in report.notebooks
    assert baseline["alpha"] not in report.skipped
    assert [row["name"] for row in _rows(report.package_dir, "notebooks")] == [
        "alpha busy"
    ]


# --------------------------------------------- deletes declare their notebook


def test_a_delete_only_window_still_declares_its_notebook(baseline):
    """A package that says "delete this chunk" without declaring the notebook
    the chunk belongs to would be describing a notebook outside its own
    scope, and the importer's manifest/rows equality check would have nothing
    to verify that scope against.

    变异验证: 去掉 delete 分支里的 ``delta.notebooks.add``, 本条必须报红。
    """
    repo = baseline["repo"]
    with repo._connect() as db:
        chunk = db.execute(
            "SELECT id FROM chunks WHERE notebook_id=? ORDER BY id LIMIT 1",
            (baseline["alpha"],),
        ).fetchone()
    with repo._write() as db:
        db.execute("DELETE FROM chunks WHERE id=?", (chunk["id"],))

    report = _export(baseline["settings"], baseline["out"])

    assert report.notebooks == (baseline["alpha"],)
    assert _manifest(report.package_dir)["notebooks"] == [baseline["alpha"]]
    assert _ids(report.package_dir, "notebooks") == {baseline["alpha"]}
    assert report.deletes == len(_deletes(report.package_dir)) >= 1


# ------------------------------------------------------------- capture gate


def test_the_gate_moving_mid_export_refuses_to_claim_captured(
    baseline, monkeypatch
):
    """``sync capture disable`` CLEARS the change log. A disable + enable pair
    while an export runs leaves ``enabled`` looking exactly as the read
    snapshot found it, while everything the next window would have described
    is gone. The watermark is still written -- the seq and snapshot are true
    facts about this run -- but it must not claim to be resumable.

    变异验证: 去掉 ``_advance_watermark`` 里的控制行重读(直接写
    ``captured=gate[0]``), 本条必须报红。
    """
    repo = baseline["repo"]
    real = export_module._advance_watermark

    def hooked(*args, **kwargs):
        with repo._write() as db:
            db.execute(
                "UPDATE sync_capture_control SET enabled=0, disabled_at=? "
                "WHERE singleton=1",
                ("2026-02-01T00:00:00+00:00",),
            )
            db.execute(
                "UPDATE sync_capture_control SET enabled=1, enabled_at=? "
                "WHERE singleton=1",
                ("2026-02-01T00:00:01+00:00",),
            )
        return real(*args, **kwargs)

    monkeypatch.setattr(export_module, "_advance_watermark", hooked)

    report = _export(baseline["settings"], baseline["out"])

    assert report.watermark_advanced is True
    assert report.captured is False
    assert [w for w in report.warnings if "capture gate changed" in w]
    assert _watermark(repo)["captured"] == 0

    monkeypatch.undo()
    assert _export(baseline["settings"], baseline["out"]).mode == MODE_FULL


def test_an_untouched_gate_still_claims_captured(baseline):
    """The other half: nothing moved, so the generation the write transaction
    re-reads is the one the snapshot saw and the watermark is resumable."""
    report = _export(baseline["settings"], baseline["out"])

    assert report.captured is True
    assert not [w for w in report.warnings if "capture gate" in w]
    assert _watermark(baseline["repo"])["captured"] == 1


# --------------------------------------------------------------- asset files


def test_an_assets_bytes_travel_by_stem(baseline):
    """``notebook_assets`` declares no path column: its bytes are
    ``<asset id>.<ext>`` under ``storage/assets/<notebook>/``, and the
    extension comes from a mime table this package may not import. The file
    phase therefore carries whatever ``<id>.*`` is on disk -- and nothing
    else in that directory."""
    repo = baseline["repo"]
    storage = Path(baseline["settings"].storage_dir) / "assets" / baseline["alpha"]
    storage.mkdir(parents=True, exist_ok=True)
    (storage / "asset-wanted.png").write_bytes(b"\x89PNG wanted")
    (storage / "asset-other.png").write_bytes(b"\x89PNG other")
    with repo._write() as db:
        db.execute(
            "INSERT INTO notebook_assets(id,notebook_id,filename,mime,size,"
            "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
            ("asset-wanted", baseline["alpha"], "p.png", "image/png", 11,
             "user-local", MOMENT),
        )

    report = _export(baseline["settings"], baseline["out"])

    carried = sorted(
        str(path.relative_to(report.package_dir))
        for path in (report.package_dir / "files").rglob("*")
        if path.is_file()
    )
    assert f"files/assets/{baseline['alpha']}/asset-wanted.png" in carried
    assert not [name for name in carried if "asset-other" in name]


# ------------------------------------------- resolve_file_requests, directly


def _requests(tmp_path):
    from app.migration.sync import incremental as module

    return module, tmp_path / "storage", tmp_path / "root"


def test_a_relative_path_column_is_resolved_against_the_root(tmp_path):
    """Legacy ``sources.file_path`` rows predate the absolute-path convention
    and both backends resolve them against ``root_dir``
    (``sqlite/database.py::resolve_path``). Skipping that normalization would
    report every such row as missing while its bytes sat right there.

    变异验证: 去掉 ``resolve_file_requests`` 的 ``is_absolute`` 归一, 本条必须报红。
    """
    module, storage, root = _requests(tmp_path)
    base = storage / "notebooks" / "nb-1"
    base.mkdir(parents=True)
    (base / "note.txt").write_bytes(b"body")
    relative = (base / "note.txt").relative_to(root.parent)

    entries, missing, warnings = module.resolve_file_requests(
        storage, root.parent, [module.FileRequest("notebooks", "nb-1", path=str(relative))]
    )

    assert entries == [("files/notebooks/nb-1/note.txt", base / "note.txt")]
    assert (missing, warnings) == ([], [])


def test_a_path_outside_the_notebook_root_is_reported_as_missing(tmp_path):
    module, storage, root = _requests(tmp_path)

    entries, missing, warnings = module.resolve_file_requests(
        storage, root, [module.FileRequest("notebooks", "nb-1", path="/elsewhere/x.txt")]
    )

    assert entries == []
    assert missing == ["files/notebooks/nb-1/x.txt"]
    assert warnings and "does not resolve" in warnings[0]


def test_a_traversal_path_is_refused_and_not_even_recorded(tmp_path):
    """``Path.relative_to`` is LEXICAL: ``<base>/../../etc/passwd`` IS
    "under" base as text. The package-relative path is what would be written
    to disk and into checksums.json, so it gets the real check -- and a
    refused path must not reach ``missing_files`` either, since that is a
    manifest field naming package paths.

    变异验证: 去掉包内相对路径的 ``is_safe_relative_path`` 检查, 本条必须报红。
    """
    module, storage, root = _requests(tmp_path)
    evil = storage / "notebooks" / "nb-1" / ".." / ".." / ".." / "etc" / "passwd"

    entries, missing, warnings = module.resolve_file_requests(
        storage, root, [module.FileRequest("notebooks", "nb-1", path=str(evil))]
    )

    assert entries == []
    assert missing == []
    assert warnings and "outside the package" in warnings[0]


def test_an_asset_id_that_matches_nothing_is_reported_as_missing(tmp_path):
    module, storage, root = _requests(tmp_path)
    (storage / "assets" / "nb-1").mkdir(parents=True)

    entries, missing, warnings = module.resolve_file_requests(
        storage, root, [module.FileRequest("assets", "nb-1", stem="asset-1")]
    )

    assert entries == []
    assert missing == ["files/assets/nb-1/asset-1"]
    assert warnings and "no file named" in warnings[0]


def test_an_unsafe_asset_id_is_never_globbed_with(tmp_path):
    module, storage, root = _requests(tmp_path)

    entries, missing, warnings = module.resolve_file_requests(
        storage, root, [module.FileRequest("assets", "nb-1", stem="../evil")]
    )

    assert (entries, missing) == ([], [])
    assert warnings and "not a safe file name" in warnings[0]


# --------------------------------------------------- path-column reconciliation


def test_an_unhandled_path_column_fails_the_module_guard(monkeypatch):
    """An incremental package carries only the files its changed rows point
    at, so a path column the export file phase does not know about is a
    silent, one-sided loss: the rows travel, their bytes do not. Same shape
    as ``import_._check_path_columns_are_covered``."""
    from app.migration.sync import incremental as module

    monkeypatch.setattr(module, "_EXPORTED_PATH_COLUMNS", {})
    with pytest.raises(RuntimeError) as failure:
        module._check_path_columns_are_covered()
    assert "sources" in str(failure.value) and "file_path" in str(failure.value)

    monkeypatch.setattr(
        module,
        "_EXPORTED_PATH_COLUMNS",
        {("sources", "file_path"): "notebooks", ("chunks", "made_up"): "notebooks"},
    )
    with pytest.raises(RuntimeError) as failure:
        module._check_path_columns_are_covered()
    assert "made_up" in str(failure.value)


# --------------------------------------------------------- bounded warnings


def test_many_vanished_upserts_produce_one_bounded_warning(baseline):
    """A window that downgrades hundreds of keys has ONE problem, not
    hundreds, and an operator report has to stay readable."""
    repo = baseline["repo"]
    with repo._write() as db:
        for index in range(12):
            db.execute(
                "INSERT INTO sync_change_log "
                "(table_name, key_json, operation, notebook_id, changed_at) "
                "VALUES ('chunks', ?, 'upsert', ?, ?)",
                (json.dumps({"id": f"ghost-{index:02d}"}), baseline["alpha"], MOMENT),
            )

    report = _export(baseline["settings"], baseline["out"])

    downgrades = [w for w in report.warnings if "no longer exist" in w]
    assert len(downgrades) == 1
    assert "12 key(s)" in downgrades[0]
    assert "+7 more" in downgrades[0]
    assert len(_deletes(report.package_dir)) == 12
