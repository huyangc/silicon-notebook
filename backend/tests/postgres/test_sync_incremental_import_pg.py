"""守卫: 增量导入 (app.migration.sync.import_ 的 PR-3c 相位) 的 PostgreSQL 泳道。

SQLite 泳道 (tests/test_sync_incremental_import.py) 已经钉住分类、链校验、删除
范围与各计数。这里跑的是 **PG 端才成立或才会分叉** 的那几件事: 真正的外键约束下
删除必须子表先于父表、``FOR UPDATE`` 下的笔记本墓碑 + 作业行、以及一条 PG→PG
的端到端 (窗口的行、删除、文件在同一后端往返)。

源库与目标库是两个独立 schema, 两边都活到用例结束 —— 增量需要源端在基线导出之后
继续改动, 这是全量泳道的 ``postgres_package`` 夹具做不到的。
"""

from __future__ import annotations

import json
import os
from contextlib import ExitStack
from pathlib import Path

import pytest

from app.core.config import Settings
from app.migration.sync.export import ExportReport, export_notebooks
from app.migration.sync.import_ import SyncImportError, import_package
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
from tests.postgres.conftest import _isolated_postgres_scope


pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_sync_import"),
]

SOURCE_ENV = "dev"
TARGET_ENV = "prod"
MOMENT = "2026-01-01T00:00:00+00:00"


def _postgres_settings(url: str, storage: Path) -> Settings:
    return Settings(
        database_url=url,
        storage_dir=str(storage),
        postgres_pool_min_size=1,
        postgres_pool_max_size=4,
        postgres_pool_acquire_timeout_seconds=2,
        postgres_statement_timeout_seconds=15,
        postgres_lock_timeout_seconds=2,
    )


def _seed_notebook(repo, name: str) -> str:
    notebook = repo.create_notebook(NotebookCreate(name=name))
    repo.upload_sources(
        notebook.id,
        [
            UploadedSourceFile(
                file_name=f"{name}.txt",
                content_type="text/plain",
                content=f"{name} body\n".encode("utf-8") * 8,
                doc_type="",
                doc_type_explicit=False,
            )
        ],
    )
    table_id = repo.create_knowhow_table(
        notebook.id, f"{name}-table", "",
        [{"name": "Topic", "role": "anchor"}], created_by="user-local",
    )
    columns = repo.get_knowhow_table(table_id)["columns"]
    repo.add_knowhow_row(
        table_id, {column["id"]: f"{name} value" for column in columns},
        actor="user-local",
    )
    return notebook.id


def _enable_capture(repo) -> None:
    with repo._write() as db:
        db.execute(
            "INSERT INTO sync_capture_control (singleton, enabled, enabled_at) "
            "VALUES (1, TRUE, %s) ON CONFLICT (singleton) DO UPDATE SET "
            "enabled = TRUE",
            (MOMENT,),
        )


def _fetch(repo, statement: str, params=()):
    with repo._connect() as db:
        return db.execute(statement, params).fetchall()


def _one(repo, statement: str, params=()):
    rows = _fetch(repo, statement, params)
    return rows[0] if rows else None


def _count(repo, statement: str, params=()) -> int:
    row = _one(repo, statement, params)
    return int(list(row.values())[0]) if row is not None else 0


def _export(settings, out_dir: Path, notebook_ids=None) -> ExportReport:
    return export_notebooks(
        settings,
        target_env=TARGET_ENV,
        out_dir=out_dir,
        notebook_ids=notebook_ids,
        source_env=SOURCE_ENV,
    )


def _deletes(package: Path) -> list[dict]:
    text = (package / DELETES_NAME).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def _write_deletes(package: Path, entries) -> None:
    (package / DELETES_NAME).write_text(
        "".join(json_line(entry) + "\n" for entry in entries), encoding="utf-8"
    )


def _reseal(package: Path) -> None:
    import hashlib

    checksums = json.loads((package / CHECKSUMS_NAME).read_text(encoding="utf-8"))
    for relative in checksums:
        checksums[relative] = hashlib.sha256(
            (package / relative).read_bytes()
        ).hexdigest()
    (package / CHECKSUMS_NAME).write_text(
        json.dumps(checksums, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    document = json.loads((package / MANIFEST_NAME).read_text(encoding="utf-8"))
    document["checksums_sha256"] = hashlib.sha256(
        (package / CHECKSUMS_NAME).read_bytes()
    ).hexdigest()
    for table, entry in (document.get("tables") or {}).items():
        digest = checksums.get(rows_path(table))
        if digest is not None:
            entry["sha256"] = digest
    (package / MANIFEST_NAME).write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )


@pytest.fixture
def mirrored(postgres_scope, tmp_path):
    """Two live PostgreSQL schemas: a source with capture on and two
    notebooks, and a target that has already applied the full baseline."""
    from app.repositories.postgres.repository import PostgresRepository

    base_url = os.environ.get("TEST_POSTGRES_URL")
    if not base_url:
        pytest.skip("TEST_POSTGRES_URL is not configured")
    with ExitStack() as stack:
        source_scope = stack.enter_context(_isolated_postgres_scope(base_url))
        source_settings = _postgres_settings(
            source_scope.url, tmp_path / "source-storage"
        )
        target_settings = _postgres_settings(
            postgres_scope.url, tmp_path / "target-storage"
        )
        source = PostgresRepository(source_settings)
        stack.callback(source.close)
        target = PostgresRepository(target_settings)
        stack.callback(target.close)

        alpha = _seed_notebook(source, "alpha")
        beta = _seed_notebook(source, "beta")
        _enable_capture(source)
        out = tmp_path / "out"
        full = _export(source_settings, out)
        assert full.mode == MODE_FULL
        report = import_package(target_settings, full.package_dir)
        assert report.error == ""

        yield {
            "source": source,
            "source_settings": source_settings,
            "target": target,
            "target_settings": target_settings,
            "out": out,
            "alpha": alpha,
            "beta": beta,
            "full": full,
        }


def _window(mirrored) -> ExportReport:
    report = _export(mirrored["source_settings"], mirrored["out"])
    assert report.mode == MODE_INCREMENTAL
    return report


def _import(mirrored, package_dir: Path, **kwargs):
    return import_package(mirrored["target_settings"], package_dir, **kwargs)


# ------------------------------------------------------------- end to end


def test_a_window_reaches_a_postgres_mirror(mirrored):
    """Rows, deletes and files in one window, same backend on both ends."""
    source = mirrored["source"]
    chunk = _one(
        source, "SELECT id FROM chunks WHERE notebook_id=%s LIMIT 1",
        (mirrored["alpha"],),
    )["id"]
    with source._write() as db:
        db.execute(
            "UPDATE notebooks SET name='alpha v2' WHERE id=%s", (mirrored["alpha"],)
        )
        db.execute("DELETE FROM chunk_elements WHERE chunk_id=%s", (chunk,))
        db.execute("DELETE FROM chunks WHERE id=%s", (chunk,))
    source.upload_sources(
        mirrored["alpha"],
        [
            UploadedSourceFile(
                file_name="second.txt",
                content_type="text/plain",
                content=b"second body\n" * 8,
                doc_type="",
                doc_type_explicit=False,
            )
        ],
    )

    window = _window(mirrored)
    report = _import(mirrored, window.package_dir)

    assert report.error == ""
    assert report.mode == MODE_INCREMENTAL
    assert report.base_package_id == mirrored["full"].package_id
    assert report.deletes_applied >= 1
    target = mirrored["target"]
    assert _one(
        target, "SELECT name FROM notebooks WHERE id=%s", (mirrored["alpha"],)
    )["name"] == "alpha v2"
    assert _count(
        target, "SELECT COUNT(*) AS c FROM chunks WHERE id=%s", (chunk,)
    ) == 0
    mirrored_paths = [
        Path(str(row["file_path"]))
        for row in _fetch(
            target, "SELECT file_path FROM sources WHERE notebook_id=%s",
            (mirrored["alpha"],),
        )
    ]
    assert len(mirrored_paths) == 2
    assert all(path.is_file() for path in mirrored_paths)


def test_children_are_deleted_before_their_parents(mirrored):
    """Phase 3c walks ``reversed(synced_tables())`` so a child goes before the
    parent it points at. On PostgreSQL every one of these edges is a real
    ``ON DELETE CASCADE`` foreign key, which makes the wrong order OBSERVABLE
    rather than merely risky: deleting ``knowhow_rows`` first cascades its
    ``knowhow_cells`` away, so the cells' own delete entries then find nothing
    and land in ``deletes_absent`` instead of ``deletes_applied``.

    变异验证: 把 ``_delete_replay_tables`` 的 ``reversed`` 去掉, 本条报红
    (applied 少了 cells 那几条, absent 多了同样多条)。"""
    source = mirrored["source"]
    row_id = _one(
        source,
        "SELECT r.id FROM knowhow_rows r JOIN knowhow_tables t ON t.id=r.table_id "
        "WHERE t.notebook_id=%s LIMIT 1",
        (mirrored["alpha"],),
    )["id"]
    with source._write() as db:
        db.execute("DELETE FROM knowhow_cells WHERE row_id=%s", (row_id,))
        db.execute("DELETE FROM knowhow_rows WHERE id=%s", (row_id,))

    window = _window(mirrored)
    entries = [
        entry
        for entry in _deletes(window.package_dir)
        if entry["table"] in ("knowhow_cells", "knowhow_rows")
    ]
    assert {entry["table"] for entry in entries} == {"knowhow_cells", "knowhow_rows"}

    report = _import(mirrored, window.package_dir)

    assert report.error == ""
    assert report.deletes_applied == len(_deletes(window.package_dir))
    assert report.deletes_absent == 0
    assert _count(
        mirrored["target"],
        "SELECT COUNT(*) AS c FROM knowhow_cells WHERE row_id=%s", (row_id,),
    ) == 0


def test_a_deleted_notebook_is_tombstoned_and_queued(mirrored):
    """The CAS and the job-row INSERT are one transaction, written out here
    rather than called through ``notebook_delete_job_store.request`` -- so the
    PostgreSQL lane is where the ``FOR UPDATE`` read, the placeholder
    translation and the real ``notebook_delete_jobs`` constraints are proven.

    变异验证: 去掉 3d 的作业行 INSERT, 本条报红。"""
    with mirrored["source"]._write() as db:
        db.execute("DELETE FROM notebooks WHERE id=%s", (mirrored["beta"],))

    window = _window(mirrored)
    report = _import(mirrored, window.package_dir)

    assert report.error == ""
    assert report.notebooks_deleted == (mirrored["beta"],)
    target = mirrored["target"]
    assert _one(
        target, "SELECT status FROM notebooks WHERE id=%s", (mirrored["beta"],)
    )["status"] == "deleting"
    job = _one(
        target,
        "SELECT id,status,phase,lease_token,attempts FROM notebook_delete_jobs "
        "WHERE notebook_id=%s",
        (mirrored["beta"],),
    )
    assert job is not None
    assert (job["status"], job["phase"], job["lease_token"], job["attempts"]) == (
        "queued", "mark", "", 0,
    )
    assert str(job["id"]).startswith("ndj-")


def test_a_delete_aimed_at_another_notebooks_row_refuses_the_import(mirrored):
    """The ownership check, on the backend where a bad DELETE would be
    committed by a real transaction rather than a file-backed one.

    变异验证: 去掉 ``_replay_delete_table`` 的归属校验, 本条报红。"""
    with mirrored["source"]._write() as db:
        db.execute(
            "UPDATE notebooks SET name='alpha v2' WHERE id=%s", (mirrored["alpha"],)
        )
    window = _window(mirrored)
    victim = _one(
        mirrored["target"],
        "SELECT id FROM chunks WHERE notebook_id=%s LIMIT 1", (mirrored["beta"],),
    )["id"]
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
        _import(mirrored, window.package_dir)

    assert mirrored["beta"] in str(failure.value)
    assert _count(
        mirrored["target"], "SELECT COUNT(*) AS c FROM chunks WHERE id=%s", (victim,)
    ) == 1


def test_a_deleted_notebook_reclaimed_by_another_source_aborts_the_replay(
    mirrored, monkeypatch
):
    """codex #788 r4 P2, on the backend where the fix's ``FOR UPDATE`` half
    actually does something: a deleted notebook id the target does not have
    stays in ``plan.allowed``, but nothing this run did CLAIMS that id --
    ``_reserve_notebooks`` only reserves ``manifest.notebooks``. A concurrent
    import of a DIFFERENT source environment can mirror that id between this
    run's transactions, and phase 3c would then delete its rows; phase 3d
    only refuses afterwards, with 3c already committed.

    The intruder is written on its OWN psycopg connection -- not through the
    target repository, which refuses a nested ``write()`` -- so this really
    is two writers on one database rather than a reentrant call.

    变异验证: 去掉 ``_assert_notebooks_still_ours`` 里覆盖 deleted_notebooks
    的那段, 本条报红 -- 外来镜像的 knowhow_tables 行被删掉。"""
    source = mirrored["source"]
    target = mirrored["target"]
    with source._write() as db:
        db.execute(
            "UPDATE notebooks SET name='alpha v2' WHERE id=%s", (mirrored["alpha"],)
        )
        db.execute("DELETE FROM notebooks WHERE id=%s", (mirrored["beta"],))
    window = _window(mirrored)
    assert window.deleted_notebooks == (mirrored["beta"],)
    # A row-level delete this window carries FOR beta. Ids are exported
    # verbatim and never reissued, so two environments mirroring the same
    # upstream content can hold the same key -- which is how the intruder's
    # brand-new mirror ends up in this window's line of fire.
    doomed = next(
        entry["key"]["id"]
        for entry in _deletes(window.package_dir)
        if entry["table"] == "knowhow_tables"
        and entry["notebook_id"] == mirrored["beta"]
    )
    with target._write() as db:
        db.execute("DELETE FROM notebooks WHERE id=%s", (mirrored["beta"],))

    from app.migration.sync import import_ as import_module

    original = import_module._replay_delete_table
    planted = {"done": False}

    def claim_then_replay(backend, conn, table, context, plan):
        if not planted["done"]:
            planted["done"] = True
            _as_another_importer(
                mirrored["target_settings"],
                (
                    "INSERT INTO notebooks(id,name,created_by,status,"
                    "sync_origin,created_at,updated_at) "
                    "VALUES(%s,%s,'user-local','draft','other-env',%s,%s)",
                    (mirrored["beta"], "theirs", MOMENT, MOMENT),
                ),
                (
                    "INSERT INTO knowhow_tables(id,notebook_id,title,"
                    "description,mutation_seq,hidden_source_id,created_by,"
                    "created_at,updated_at) "
                    "VALUES(%s,%s,'theirs','',0,'','user-local',%s,%s)",
                    (doomed, mirrored["beta"], MOMENT, MOMENT),
                ),
            )
        return original(backend, conn, table, context, plan)

    monkeypatch.setattr(import_module, "_replay_delete_table", claim_then_replay)

    with pytest.raises(SyncImportError) as failure:
        _import(mirrored, window.package_dir)

    assert planted["done"]
    message = str(failure.value)
    assert mirrored["beta"] in message
    assert "other-env" in message
    assert _count(
        target, "SELECT COUNT(*) AS c FROM knowhow_tables WHERE id=%s", (doomed,)
    ) == 1
    assert _one(
        target, "SELECT sync_origin FROM notebooks WHERE id=%s", (mirrored["beta"],)
    )["sync_origin"] == "other-env"


def _as_another_importer(settings: Settings, *statements) -> None:
    """Run statements on a connection of their own, committed immediately --
    what a concurrent import of a DIFFERENT source environment looks like
    from this run's point of view."""
    import psycopg

    with psycopg.connect(str(settings.database_url), autocommit=True) as conn:
        for sql, params in statements:
            conn.execute(sql, params)


def _add_revision(repo, memory: str, revision: int, row_id: str, title: str):
    with repo._write() as db:
        db.execute(
            "INSERT INTO memory_revisions(id,memory_id,revision,title,"
            "content_md,tags_json,status,promotion_state,changed_by,"
            "change_reason,created_at) "
            "VALUES(%s,%s,%s,%s,'body','[]','candidate','none','user-local',"
            "'edit',%s)",
            (row_id, memory, revision, title, MOMENT),
        )


def test_both_sides_editing_one_memory_resolves_instead_of_deadlocking(
    mirrored
):
    """codex #788 r5 P1 on PostgreSQL, where ``UNIQUE(memory_id, revision)``
    is a real constraint the upsert would hit rather than something this
    module merely checks for.

    变异验证: 去掉 ``resolve_collisions`` 分支, 本条报红。"""
    source = mirrored["source"]
    target = mirrored["target"]
    source.create_memory_candidate(
        mirrored["alpha"], "user-local", None, "记忆请求", "alpha 记忆",
        "正文", ["t"], "test",
    )
    memory = _one(
        source, "SELECT id FROM memory_items WHERE notebook_id=%s LIMIT 1",
        (mirrored["alpha"],),
    )["id"]
    _add_revision(source, memory, 2, "rev-2-shared", "shared history")
    carrier = _window(mirrored)
    assert _import(mirrored, carrier.package_dir).error == ""
    assert _count(
        target, "SELECT COUNT(*) AS c FROM memory_revisions WHERE id=%s",
        ("rev-2-shared",),
    ) == 1

    # A real edit also rewrites the item row, so the window carries the
    # parent -- the shape a snapshot prune would act on.
    _add_revision(source, memory, 3, "rev-3-source", "the source's edit")
    with source._write() as db:
        db.execute(
            "UPDATE memory_items SET content_md='the source edit' WHERE id=%s",
            (memory,),
        )
    _add_revision(target, memory, 3, "rev-3-target", "this target's own edit")

    report = _import(mirrored, _window(mirrored).package_dir)

    assert report.error == ""
    surviving = {
        str(row["id"]): int(row["revision"])
        for row in _fetch(
            target,
            "SELECT id, revision FROM memory_revisions WHERE memory_id=%s",
            (memory,),
        )
    }
    # First: the history neither side touched survives -- a targeted
    # resolution, not a snapshot prune.
    assert surviving.get("rev-2-shared") == 2
    assert surviving.get("rev-3-source") == 3
    assert "rev-3-target" not in surviving
    assert report.source_authoritative_collisions_resolved >= 1
