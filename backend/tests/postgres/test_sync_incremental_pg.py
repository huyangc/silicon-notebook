"""守卫: 增量导出 (app.migration.sync.incremental) 的 PostgreSQL 泳道。

SQLite 泳道 (tests/test_sync_incremental.py) 已经钉住模式判定、压缩、归属、
seed_only/镜像过滤、deletes 与文件。这里只覆盖**按后端会分叉**的那一段:

- 补偿窗口。SQLite 没有「已发号但未提交」的窗口, 所以那一半只有在这里能真的
  测到: 一个在上次导出快照建立时仍在途、之后才提交的事务, 它的日志行 seq 比
  水位还小, 主窗口 (seq > W) 永远够不到它。少了补偿窗口就是**静默永久丢行**。
- ``key_json`` 在 PG 上是 jsonb (psycopg 交回 dict、键序归一), SQLite 上是
  ``json_object`` 文本 (声明序)。按解析后的对象比较才能两端一致。
- ``exported_snapshot`` 在 PG 上是真的快照文本, 每次导出都要被覆写。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.config import Settings
from app.migration.sync.export import export_notebooks
from app.migration.sync.package import (
    DELETES_NAME,
    MODE_FULL,
    MODE_INCREMENTAL,
    rows_path,
)
from app.models.schemas import NotebookCreate
from app.repositories.ports import UploadedSourceFile


pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_sync_incremental"),
]

SOURCE_ENV = "dev"
TARGET_ENV = "prod"


@pytest.fixture
def export_settings(postgres_scope, tmp_path) -> Settings:
    return Settings(
        database_url=postgres_scope.url,
        storage_dir=str(tmp_path / "storage"),
        postgres_pool_min_size=1,
        postgres_pool_max_size=4,
        postgres_pool_acquire_timeout_seconds=2,
        postgres_statement_timeout_seconds=15,
        postgres_lock_timeout_seconds=2,
    )


@pytest.fixture
def repository(export_settings):
    from app.repositories.postgres.repository import PostgresRepository

    repo = PostgresRepository(export_settings)
    try:
        yield repo
    finally:
        repo.close()


def _seed(repo, name: str) -> str:
    notebook = repo.create_notebook(NotebookCreate(name=name))
    repo.upload_sources(
        notebook.id,
        [
            UploadedSourceFile(
                file_name=f"{name}.txt",
                content_type="text/plain",
                content=b"alpha beta gamma\n" * 8,
                doc_type="",
                doc_type_explicit=False,
            )
        ],
    )
    return notebook.id


def _enable_capture(repo) -> None:
    with repo._write() as db:
        db.execute(
            "INSERT INTO sync_capture_control (singleton, enabled, enabled_at) "
            "VALUES (1, true, now()) "
            "ON CONFLICT (singleton) DO UPDATE SET enabled = true"
        )


def _export(settings, out_dir: Path, notebook_ids=None, *, full=False):
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


def _names(package: Path) -> dict[str, str]:
    return {str(row["id"]): str(row["name"]) for row in _rows(package, "notebooks")}


def _deletes(package: Path) -> list[dict]:
    text = (package / DELETES_NAME).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def _watermark(repo) -> dict:
    with repo._connect() as db:
        row = db.execute(
            "SELECT * FROM sync_export_state WHERE target_env=%s", (TARGET_ENV,)
        ).fetchone()
    return dict(row) if row is not None else {}


@pytest.fixture
def baseline(repository, export_settings, postgres_scope, tmp_path):
    alpha = _seed(repository, "alpha")
    beta = _seed(repository, "beta")
    _enable_capture(repository)
    report = _export(export_settings, tmp_path / "out")
    assert report.mode == MODE_FULL
    return {
        "repo": repository,
        "settings": export_settings,
        "url": postgres_scope.url,
        "out": tmp_path / "out",
        "alpha": alpha,
        "beta": beta,
        "baseline": report,
    }


# ------------------------------------------------------- the ordinary window


def test_the_export_after_a_captured_baseline_is_incremental_on_postgres(baseline):
    """``key_json`` is ``jsonb`` here, so psycopg hands the compaction a dict
    where SQLite hands it text. The window has to match keys by the PARSED
    object or the two backends would compact differently."""
    with baseline["repo"]._write() as db:
        db.execute(
            "UPDATE notebooks SET name=%s WHERE id=%s", ("alpha v2", baseline["alpha"])
        )

    report = _export(baseline["settings"], baseline["out"])

    assert report.mode == MODE_INCREMENTAL
    assert _names(report.package_dir) == {baseline["alpha"]: "alpha v2"}
    assert report.base_package_id == baseline["baseline"].package_id


def test_a_deleted_row_travels_as_a_delete_entry_on_postgres(baseline):
    with baseline["repo"]._connect() as db:
        chunk = db.execute(
            "SELECT id FROM chunks WHERE notebook_id=%s ORDER BY id LIMIT 1",
            (baseline["alpha"],),
        ).fetchone()
    assert chunk is not None
    with baseline["repo"]._write() as db:
        db.execute("DELETE FROM chunks WHERE id=%s", (chunk["id"],))

    report = _export(baseline["settings"], baseline["out"])

    assert {
        "table": "chunks",
        "key": {"id": str(chunk["id"])},
        "notebook_id": baseline["alpha"],
        "parent_key": None,
    } in _deletes(report.package_dir)


# ------------------------------------------------------- compensation window


def test_a_transaction_in_flight_at_the_previous_snapshot_is_picked_up_later(
    baseline,
):
    """The gap this whole mechanism exists for (§7 「增量窗口」step 2).

    Writer A takes its ``seq`` and stays uncommitted; writer B takes a HIGHER
    ``seq`` and commits first. The export taken in between sees B and not A,
    and its watermark is therefore B's ``seq`` -- which is ABOVE A's. A plain
    ``seq > W`` window would never reach A again: nothing is missing from the
    log, only from the windows. The compensation half finds it by asking
    which log rows the PREVIOUS export's snapshot could not see.

    变异验证: 让 ``compensation_rows`` 恒返回 ``{}``, 本条必须报红(导出 2 里
    没有 A 的那次改名), 而其它用例仍然全绿 —— 这就是「静默丢行」的形状。
    """
    import psycopg
    from psycopg.rows import dict_row

    alpha, beta = baseline["alpha"], baseline["beta"]
    with psycopg.connect(baseline["url"], row_factory=dict_row) as writer_a:
        # A: writes (and so logs) but does NOT commit.
        writer_a.execute(
            "UPDATE notebooks SET name=%s WHERE id=%s", ("alpha from A", alpha)
        )
        seq_a = writer_a.execute(
            "SELECT MAX(seq) AS seq FROM sync_change_log"
        ).fetchone()["seq"]

        # B: a later seq, committed first.
        with baseline["repo"]._write() as db:
            db.execute(
                "UPDATE notebooks SET name=%s WHERE id=%s", ("beta from B", beta)
            )
        with baseline["repo"]._connect() as db:
            seq_b = db.execute(
                "SELECT MAX(seq) AS seq FROM sync_change_log"
            ).fetchone()["seq"]
        assert seq_a < seq_b, "the gap needs A to hold the LOWER seq"

        first = _export(baseline["settings"], baseline["out"])
        assert first.mode == MODE_INCREMENTAL
        assert _names(first.package_dir) == {beta: "beta from B"}
        assert first.to_seq == seq_b, "the watermark skipped past A's seq"

        writer_a.commit()

    second = _export(baseline["settings"], baseline["out"])

    assert second.mode == MODE_INCREMENTAL
    assert _names(second.package_dir) == {alpha: "alpha from A"}

    # ...and exactly once: the export after it can see A in its own previous
    # snapshot, so the compensation window no longer selects it.
    third = _export(baseline["settings"], baseline["out"])
    assert third.empty is True
    assert _names(third.package_dir) == {}


def test_the_compensation_window_is_bounded_by_the_stored_snapshot(baseline):
    """The bound that makes the query cheap must not be the thing that makes
    it correct: a committed transaction the previous export COULD see is
    excluded by the visibility test, not by its seq."""
    from app.migration.sync import incremental

    with baseline["repo"]._write() as db:
        db.execute(
            "UPDATE notebooks SET name=%s WHERE id=%s", ("alpha v2", baseline["alpha"])
        )
    _export(baseline["settings"], baseline["out"])

    from app.migration.sync.export import _Source

    source = _Source(baseline["settings"], Path(__file__).resolve().parents[3])
    try:
        with source.read() as conn:
            watermark = incremental.read_watermark(source, conn, TARGET_ENV)
            assert watermark is not None and watermark.exported_snapshot
            assert incremental.compensation_rows(source, conn, watermark) == {}
    finally:
        source.close()


# ------------------------------------------------------------- the watermark


def test_two_exports_write_two_different_snapshots(baseline):
    """``exported_snapshot`` and ``captured`` must both be in the ON CONFLICT
    DO UPDATE SET list, not only in the INSERT list. Keeping the first run's
    snapshot would hand the next compensation window a stale (older) ``xmin``
    and re-read a widening tail of the log forever; keeping the first run's
    ``captured`` would let a gate-closed export inherit a ``true`` it never
    earned.

    变异验证: 从 ``_advance_watermark`` 的 DO UPDATE SET 里删掉
    ``exported_snapshot`` 或 ``captured``, 本条必须报红。
    """
    first = _watermark(baseline["repo"])
    assert first["captured"] is True
    assert ":" in str(first["exported_snapshot"])

    with baseline["repo"]._write() as db:
        db.execute(
            "UPDATE notebooks SET name=%s WHERE id=%s", ("alpha v2", baseline["alpha"])
        )
    _export(baseline["settings"], baseline["out"])
    second = _watermark(baseline["repo"])

    assert second["exported_snapshot"] != first["exported_snapshot"]
    assert second["captured"] is True

    with baseline["repo"]._write() as db:
        db.execute("UPDATE sync_capture_control SET enabled = false WHERE singleton = 1")
    _export(baseline["settings"], baseline["out"])
    third = _watermark(baseline["repo"])

    assert third["captured"] is False
    assert third["exported_through_seq"] == 0
    assert third["exported_snapshot"] != second["exported_snapshot"]


def test_a_scoped_export_leaves_the_watermark_alone_on_postgres(baseline):
    before = _watermark(baseline["repo"])

    report = _export(baseline["settings"], baseline["out"], [baseline["alpha"]])

    assert report.scoped is True and report.watermark_advanced is False
    assert _watermark(baseline["repo"]) == before
