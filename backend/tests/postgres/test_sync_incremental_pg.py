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
from typing import Any

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
    # The closed gate is reported through ``captured``; the seq stays clamped
    # to the previous watermark so the chain never moves backwards.
    assert third["exported_through_seq"] == second["exported_through_seq"]
    assert third["exported_snapshot"] != second["exported_snapshot"]


def test_a_scoped_export_leaves_the_watermark_alone_on_postgres(baseline):
    before = _watermark(baseline["repo"])

    report = _export(baseline["settings"], baseline["out"], [baseline["alpha"]])

    assert report.scoped is True and report.watermark_advanced is False
    assert _watermark(baseline["repo"]) == before


# --------------------------------------------- publishing under contention


def test_the_gate_row_is_locked_until_the_watermark_commits(baseline, monkeypatch):
    """PostgreSQL's half of the same guarantee. SQLite serializes writers with
    ``BEGIN IMMEDIATE``; here the lock that matters is a ROW lock on
    ``sync_capture_control`` (``SELECT ... FOR UPDATE``), because taking
    anything broader would serialize unrelated writers for the length of a
    watermark commit. ``sync capture enable``/``disable`` both write that row,
    so either they land before the re-read or they wait for the commit.

    变异验证: 去掉 ``_capture_gate`` 的 ``FOR UPDATE``, 本条必须报红(探针的
    UPDATE 立刻成功, 没有被挡住)。
    """
    import psycopg
    from app.migration.sync import export as export_module

    probes: list[str] = []
    real_gate = export_module._capture_gate

    def hooked(source, conn, *, lock=False):
        gate = real_gate(source, conn, lock=lock)
        if lock:
            # Probed AFTER the FOR UPDATE has run and while the export's
            # transaction is still open -- that is the whole window the lock
            # exists to close.
            with psycopg.connect(baseline["url"]) as other:
                other.execute("SET LOCAL lock_timeout = '400ms'")
                try:
                    other.execute(
                        "UPDATE sync_capture_control SET enabled = true "
                        "WHERE singleton = 1"
                    )
                    other.commit()
                    probes.append("acquired")
                except psycopg.errors.LockNotAvailable as exc:
                    probes.append(f"blocked: {exc}")
        return gate

    monkeypatch.setattr(export_module, "_capture_gate", hooked)

    report = _export(baseline["settings"], baseline["out"])

    assert probes, "the locked gate read never ran"
    assert probes[0].startswith("blocked"), probes
    assert report.captured is True


def test_an_incremental_export_refuses_to_publish_over_a_moved_watermark_on_pg(
    baseline, monkeypatch
):
    from app.migration.sync import export as export_module
    from app.migration.sync.export import SyncExportError

    repo = baseline["repo"]
    base = _watermark(repo)["package_id"]
    with repo._write() as db:
        db.execute(
            "UPDATE notebooks SET name=%s WHERE id=%s", ("alpha v2", baseline["alpha"])
        )
    real = export_module._advance_watermark

    def hooked(*args, **kwargs):
        with repo._write() as db:
            db.execute(
                "UPDATE sync_export_state SET package_id='another-export' "
                "WHERE target_env=%s",
                (TARGET_ENV,),
            )
        return real(*args, **kwargs)

    monkeypatch.setattr(export_module, "_advance_watermark", hooked)
    before = {path.name for path in baseline["out"].iterdir()}

    with pytest.raises(SyncExportError) as failure:
        _export(baseline["settings"], baseline["out"])

    assert "watermark moved during export" in str(failure.value)
    assert base in str(failure.value)
    assert {path.name for path in baseline["out"].iterdir()} == before
    assert _watermark(repo)["package_id"] == "another-export"


# ------------------------------------------------------------ export lease


def _lease(repo) -> dict:
    with repo._connect() as db:
        row = db.execute(
            "SELECT * FROM sync_export_runs WHERE target_env=%s", (TARGET_ENV,)
        ).fetchone()
    return dict(row) if row is not None else {}


def test_the_export_lease_round_trips_on_postgres(baseline):
    """PostgreSQL's ``timestamptz``/``bigint`` shapes, and the ``FOR UPDATE``
    on the lease row, go through the same claim/refresh/release path as
    SQLite's TEXT/INTEGER ones. A live row refuses the second export; the
    winner's row is gone once its watermark is published."""
    from app.migration.sync.export import SyncExportError

    repo = baseline["repo"]
    with repo._write() as db:
        db.execute(
            "INSERT INTO sync_export_runs(target_env, run_id, package_id, "
            "started_at, heartbeat_at, floor_seq) "
            "VALUES(%s, 'other-run', 'other-run', now(), now(), 0)",
            (TARGET_ENV,),
        )

    with pytest.raises(SyncExportError) as failure:
        _export(baseline["settings"], baseline["out"])
    assert "already running" in str(failure.value)
    assert _lease(repo)["run_id"] == "other-run"

    with repo._write() as db:
        db.execute(
            "UPDATE sync_export_runs SET heartbeat_at = now() - interval '2 hours' "
            "WHERE target_env = %s",
            (TARGET_ENV,),
        )

    report = _export(baseline["settings"], baseline["out"])

    assert [w for w in report.warnings if "dead export lease" in w]
    assert _lease(repo) == {}
    assert report.run_id == report.package_id


def test_two_connections_racing_for_an_empty_lease_row_produce_one_winner(
    baseline,
):
    """The case the previous shape got wrong (codex #784 r2). With NO lease
    row yet, two exports both read nothing -- PostgreSQL cannot lock a row
    that does not exist -- so a read-then-upsert let the second overwrite the
    first's live lease and both runs continued. The claim is now one
    ``INSERT ... ON CONFLICT ... WHERE heartbeat_at < <cutoff>`` and only a
    rowcount of 1 counts: the insert wins outright on an empty table, and the
    second transaction re-evaluates that WHERE against the row the winner
    just committed.

    Two separate ``_Source`` handles, i.e. two connections, in the order that
    pins the outcome.

    变异验证: 去掉 ``DO UPDATE ... WHERE sync_export_runs.heartbeat_at < ?``,
    本条必须报红(第二个导出抢走了活租约)。
    """
    from datetime import datetime, timezone
    from pathlib import Path as _Path

    from app.migration.sync.export import (
        SyncExportError,
        _claim_export_lease,
        _Source,
    )

    repo = baseline["repo"]
    with repo._write() as db:
        db.execute("DELETE FROM sync_export_runs WHERE target_env = %s", (TARGET_ENV,))

    root = _Path(__file__).resolve().parents[3]
    first = _Source(baseline["settings"], root)
    second = _Source(baseline["settings"], root)
    try:
        assert _claim_export_lease(
            first, TARGET_ENV, "run-a", datetime.now(timezone.utc)
        ) == []
        with pytest.raises(SyncExportError) as failure:
            _claim_export_lease(
                second, TARGET_ENV, "run-b", datetime.now(timezone.utc)
            )
    finally:
        first.close()
        second.close()

    assert "already running" in str(failure.value)
    assert "run-a" in str(failure.value)
    # ...and the loser did not overwrite the winner's row.
    assert _lease(repo)["run_id"] == "run-a"


def test_the_lease_records_a_transaction_floor_below_the_export_snapshot(
    baseline, monkeypatch
):
    """``floor_seq`` cannot protect the next export's COMPENSATION window:
    that window re-reads log rows whose ``seq`` is BELOW the watermark, so a
    seq floor says nothing about them. ``floor_xmin`` does, and only because
    of the ORDER -- the lease transaction runs strictly before the export
    opens its read snapshot, so its xmin cannot be above that snapshot's, and
    every transaction still in flight at the snapshot carries a txid at or
    above it.

    Asserted end to end: the lease's ``floor_xmin`` against the xmin of the
    snapshot this very export goes on to publish in
    ``sync_export_state.exported_snapshot``.

    变异验证: 领取租约时不写 ``floor_xmin``(留 NULL), 本条必须报红。
    """
    from app.migration.sync import export as export_module
    from app.migration.sync.export import _Source

    seen: list[Any] = []
    real = export_module._assemble

    def hooked(*args, **kwargs):
        seen.append(_lease(baseline["repo"])["floor_xmin"])
        return real(*args, **kwargs)

    monkeypatch.setattr(export_module, "_assemble", hooked)

    report = _export(baseline["settings"], baseline["out"])

    assert seen and seen[0] is not None, "the lease carries no transaction floor"
    published = _watermark(baseline["repo"])["exported_snapshot"]
    assert published, "the watermark carries no snapshot to compare against"
    assert int(seen[0]) <= _Source.snapshot_xmin(str(published))
    assert report.captured is True


def test_a_superseded_run_does_not_publish_on_postgres(baseline, monkeypatch):
    """PostgreSQL's half of codex #784 r5. Two things are asserted together,
    because they are one guarantee: the publish transaction refuses when the
    lease is no longer this run's, and it holds that row ``FOR UPDATE`` while
    it decides, so the lease cannot be taken over between the check and the
    commit.

    变异验证: 去掉 ``_advance_watermark`` 的 ``_require_lease`` 调用 → 拒绝
    断言报红; 去掉 ``_require_lease`` 里的 ``FOR UPDATE`` → 锁探针断言报红。
    """
    import psycopg

    from app.migration.sync import export as export_module
    from app.migration.sync.export import SyncExportError

    repo = baseline["repo"]
    probes: list[str] = []
    real_lease = export_module._require_lease
    real_advance = export_module._advance_watermark

    def locked(source, conn, target_env, run_id):
        try:
            real_lease(source, conn, target_env, run_id)
        finally:
            # After the FOR UPDATE ran, while the publish transaction is open.
            with psycopg.connect(baseline["url"]) as other:
                other.execute("SET LOCAL lock_timeout = '400ms'")
                try:
                    other.execute(
                        "UPDATE sync_export_runs SET run_id = 'thief' "
                        "WHERE target_env = %s",
                        (TARGET_ENV,),
                    )
                    other.commit()
                    probes.append("acquired")
                except psycopg.errors.LockNotAvailable as exc:
                    probes.append(f"blocked: {exc}")

    def supersede(*args, **kwargs):
        with repo._write() as db:
            db.execute(
                "UPDATE sync_export_runs SET run_id='successor', "
                "package_id='successor' WHERE target_env=%s",
                (TARGET_ENV,),
            )
            db.execute(
                "UPDATE sync_export_state SET package_id='successor-pkg' "
                "WHERE target_env=%s",
                (TARGET_ENV,),
            )
        return real_advance(*args, **kwargs)

    monkeypatch.setattr(export_module, "_require_lease", locked)
    monkeypatch.setattr(export_module, "_advance_watermark", supersede)
    before = {path.name for path in baseline["out"].iterdir()}

    with pytest.raises(SyncExportError) as failure:
        _export(baseline["settings"], baseline["out"], full=True)

    assert "was taken over by run" in str(failure.value)
    assert "successor" in str(failure.value)
    assert {path.name for path in baseline["out"].iterdir()} == before
    assert _watermark(repo)["package_id"] == "successor-pkg"
    assert probes and probes[0].startswith("blocked"), probes
