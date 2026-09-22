"""守卫: 全量导入器 (app.migration.sync.import_) 的 PostgreSQL 泳道。

SQLite 泳道 (tests/test_sync_import.py) 已经钉住相位契约、身份映射、幂等与
断点续跑。这里只覆盖**按后端会分叉**的那一段, 而且两个方向各跑一条端到端:

- SQLite 包 → PG 目标: 包里的 "SQLite 形态" 值 (JSON 文本、ISO 时间文本、
  ``$bytes``、0/1 布尔、空串时间哨兵) 必须经 ``transform_sqlite_value`` 落成
  jsonb / timestamptz / bytea / boolean 原生值。
- PG 包 → PG 目标: 同一后端往返, 外加 ``ON CONFLICT (pk) DO UPDATE`` 的 PG
  语法与 ``POSTGRES_ROWID_ORDINAL_TABLES`` 的 identity ``ordinal`` 由目标端
  重新分配 (SQLite 端根本没有这一列, 只有这边测得到)。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from app.core.config import Settings
from app.migration.sync.export import export_notebooks
from app.migration.sync.import_ import SyncImportError, import_package
from app.models.schemas import NotebookCreate
from app.repositories.ports import UploadedSourceFile
from tests.postgres.conftest import _isolated_postgres_scope


pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_sync_import"),
]

SOURCE_ENV = "dev"
TARGET_ENV = "prod"
VECTOR = bytes(range(32))
QUESTIONS = ["问题一", "问题二"]
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


def _sqlite_settings(root: Path) -> Settings:
    root.mkdir(parents=True, exist_ok=True)
    return Settings(
        database_url=f"sqlite:///{root / 'sync.db'}",
        storage_dir=str(root / "storage"),
    )


def _seed(repo, name: str, *, placeholder: str, no_time) -> str:
    """One notebook carrying a value of every type that forks by backend: a
    non-empty jsonb payload, a blob, and a knowledge object whose
    ``last_reviewed`` is the "no time" sentinel each backend spells its own
    way (NULL in PostgreSQL, '' in SQLite)."""
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
    repo.create_memory_candidate(
        notebook.id, "user-local", None, f"{name}-req", f"{name} memory",
        "记忆正文", ["t"], "test",
    )
    mark = placeholder
    with repo._write() as db:
        db.execute(
            f"UPDATE notebooks SET expected_questions = {mark} WHERE id = {mark}",
            (json.dumps(QUESTIONS, ensure_ascii=False), notebook.id),
        )
        chunk = db.execute(
            f"SELECT id FROM chunks WHERE notebook_id={mark} ORDER BY id LIMIT 1",
            (notebook.id,),
        ).fetchone()
        assert chunk is not None, "seed upload produced no chunk"
        db.execute(
            "INSERT INTO chunk_embeddings(chunk_id,notebook_id,vector,created_at) "
            f"VALUES({mark},{mark},{mark},{mark})",
            (chunk["id"], notebook.id, VECTOR, MOMENT),
        )
        db.execute(
            "INSERT INTO knowledge_objects"
            "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,"
            "created_at,updated_at,last_reviewed) "
            f"VALUES({mark},{mark},'concept','approved','',{mark},{mark},'',"
            f"{mark},{mark},{mark})",
            (
                f"ko-{name}",
                notebook.id,
                json.dumps({"canonical": name}, ensure_ascii=False),
                json.dumps([], ensure_ascii=False),
                MOMENT,
                MOMENT,
                no_time,
            ),
        )
        # The OTHER POSTGRES_EMPTY_TIME_SENTINELS column. A row already
        # exists (indexing creates it), so this only clears the instant.
        db.execute(
            f"UPDATE unified_kg_state SET last_rebuild_at={mark} "
            f"WHERE notebook_id={mark}",
            (no_time, notebook.id),
        )
    return notebook.id


@pytest.fixture
def target(postgres_scope, tmp_path):
    """Library B: the PostgreSQL target every case in this file imports into."""
    from app.repositories.postgres.repository import PostgresRepository

    settings = _postgres_settings(postgres_scope.url, tmp_path / "target-storage")
    repo = PostgresRepository(settings)
    try:
        yield {"repo": repo, "settings": settings}
    finally:
        repo.close()


@pytest.fixture
def sqlite_package(tmp_path):
    """A package produced by a SQLite source -- the cross-backend direction."""
    from app.services.sqlite_repository import SQLiteRepository

    settings = _sqlite_settings(tmp_path / "sqlite-source")
    repo = SQLiteRepository(settings)
    try:
        # SQLite spells "no time" as '' (the column is TEXT NOT NULL there);
        # PostgreSQL spells it NULL. POSTGRES_EMPTY_TIME_SENTINELS is the
        # registry of that pair, and both sides of it are exercised here.
        notebook = _seed(repo, "from-sqlite", placeholder="?", no_time="")
        report = export_notebooks(
            settings,
            target_env=TARGET_ENV,
            out_dir=tmp_path / "out-sqlite",
            notebook_ids=[notebook],
            source_env=SOURCE_ENV,
        )
    finally:
        repo.close()
    return {"package": report.package_dir, "notebook": notebook}


@pytest.fixture
def postgres_package(tmp_path):
    """A package produced by a SECOND, isolated PostgreSQL schema -- the
    same-backend direction. A separate scope rather than the target's own, so
    the import really writes rows it did not itself produce."""
    import os

    from app.repositories.postgres.repository import PostgresRepository

    base_url = os.environ.get("TEST_POSTGRES_URL")
    if not base_url:
        pytest.skip("TEST_POSTGRES_URL is not configured")
    with _isolated_postgres_scope(base_url) as scope:
        settings = _postgres_settings(scope.url, tmp_path / "pg-source-storage")
        repo = PostgresRepository(settings)
        try:
            notebook = _seed(repo, "from-postgres", placeholder="%s", no_time=None)
            report = export_notebooks(
                settings,
                target_env=TARGET_ENV,
                out_dir=tmp_path / "out-pg",
                notebook_ids=[notebook],
                source_env=SOURCE_ENV,
            )
        finally:
            repo.close()
    return {"package": report.package_dir, "notebook": notebook}


def _fetch(repo, statement: str, params=()):
    with repo._connect() as db:
        return db.execute(statement, params).fetchall()


def _one(repo, statement: str, params=()):
    rows = _fetch(repo, statement, params)
    return rows[0] if rows else None


# ------------------------------------------------- SQLite package -> PG target


def test_a_sqlite_package_lands_native_postgres_values(
    target, sqlite_package
):
    report = import_package(target["settings"], sqlite_package["package"])
    repo = target["repo"]
    notebook = sqlite_package["notebook"]

    assert report.error == ""
    assert report.tables["notebooks"].inserted == 1

    # jsonb: a real array, not the package's text form.
    row = _one(
        repo,
        "SELECT expected_questions, jsonb_typeof(expected_questions) AS kind, "
        "created_at, sync_origin FROM notebooks WHERE id=%s",
        (notebook,),
    )
    assert row["kind"] == "array"
    assert row["expected_questions"] == QUESTIONS
    # timestamptz: a real instant, not ISO text.
    assert isinstance(row["created_at"], datetime)
    assert row["created_at"].tzinfo is not None
    assert row["sync_origin"] == SOURCE_ENV

    # bytea: the exact bytes, never re-expanded into a float array.
    vector = _one(
        repo, "SELECT vector FROM chunk_embeddings WHERE notebook_id=%s", (notebook,)
    )["vector"]
    assert bytes(vector) == VECTOR

    # The empty-time sentinel: '' in SQLite is NULL here (§8's two registered
    # columns), not the string "''" and not now().
    reviewed = _one(
        repo,
        "SELECT last_reviewed FROM knowledge_objects WHERE notebook_id=%s",
        (notebook,),
    )
    assert reviewed["last_reviewed"] is None


def test_a_sqlite_package_gets_target_assigned_ordinals(target, sqlite_package):
    """``ordinal`` is a PostgreSQL identity column SQLite does not have at
    all, so a SQLite package cannot carry one: the target must mint its own
    from 1 (§8)."""
    import_package(target["settings"], sqlite_package["package"])

    ordinals = [
        int(row["ordinal"])
        for row in _fetch(
            target["repo"],
            "SELECT ordinal FROM chunks WHERE notebook_id=%s ORDER BY ordinal",
            (sqlite_package["notebook"],),
        )
    ]
    assert ordinals
    assert ordinals == list(range(1, len(ordinals) + 1))


# ----------------------------------------------------- PG package -> PG target


def test_a_postgres_package_round_trips_through_its_own_backend(
    target, postgres_package
):
    report = import_package(target["settings"], postgres_package["package"])
    repo = target["repo"]
    notebook = postgres_package["notebook"]

    assert report.error == ""
    row = _one(
        repo,
        "SELECT expected_questions, jsonb_typeof(expected_questions) AS kind, "
        "sync_origin FROM notebooks WHERE id=%s",
        (notebook,),
    )
    assert row["kind"] == "array"
    assert row["expected_questions"] == QUESTIONS
    assert row["sync_origin"] == SOURCE_ENV
    vector = _one(
        repo, "SELECT vector FROM chunk_embeddings WHERE notebook_id=%s", (notebook,)
    )["vector"]
    assert bytes(vector) == VECTOR
    assert _one(
        repo,
        "SELECT last_reviewed FROM knowledge_objects WHERE notebook_id=%s",
        (notebook,),
    )["last_reviewed"] is None


def test_the_source_ordinal_never_travels(target, postgres_package):
    """Both sides HAVE the column here, which is the only configuration in
    which carrying it across would be possible -- and it still must not."""
    package_rows = [
        json.loads(line)
        for line in (
            postgres_package["package"] / "rows" / "chunks.jsonl"
        ).read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert package_rows
    assert all("ordinal" not in row for row in package_rows)

    import_package(target["settings"], postgres_package["package"])

    ordinals = [
        int(row["ordinal"])
        for row in _fetch(
            target["repo"],
            "SELECT ordinal FROM chunks WHERE notebook_id=%s ORDER BY ordinal",
            (postgres_package["notebook"],),
        )
    ]
    assert ordinals == list(range(1, len(ordinals) + 1))


def test_reapplying_the_same_rows_takes_the_do_update_branch(
    target, postgres_package
):
    """``ON CONFLICT (pk) DO UPDATE SET c=excluded.c`` is written once for
    both backends; this is the run that proves the PostgreSQL half of it.
    The package is re-applied by clearing this environment's own bookkeeping
    (what a resumed run after a crash sees), not by editing the package."""
    package = postgres_package["package"]
    notebook = postgres_package["notebook"]
    first = import_package(target["settings"], package)
    before = {
        table: int(
            _one(target["repo"], f"SELECT COUNT(*) AS n FROM {table}")["n"]
        )
        for table in ("notebooks", "sources", "chunks", "chunk_embeddings")
    }
    with target["repo"]._write() as db:
        db.execute("DELETE FROM sync_import_progress WHERE package_id=%s",
                   (first.package_id,))
        db.execute("UPDATE sync_imports SET status='failed' WHERE package_id=%s",
                   (first.package_id,))

    second = import_package(target["settings"], package)

    assert second.tables["notebooks"].updated == 1
    assert second.tables["notebooks"].inserted == 0
    assert second.tables["chunks"].updated == before["chunks"]
    after = {
        table: int(
            _one(target["repo"], f"SELECT COUNT(*) AS n FROM {table}")["n"]
        )
        for table in before
    }
    assert after == before
    assert _one(
        target["repo"], "SELECT sync_origin FROM notebooks WHERE id=%s", (notebook,)
    )["sync_origin"] == SOURCE_ENV


# ----------------------------------------------------- PG package -> SQLite target


def test_a_postgres_package_lands_in_a_sqlite_target(postgres_package, tmp_path):
    """The reverse crossing. PostgreSQL spells "no instant" as NULL; the two
    columns in ``POSTGRES_EMPTY_TIME_SENTINELS`` are ``TEXT NOT NULL`` in
    SQLite, so the package's ``null`` has to land as that schema's empty-string
    sentinel or the insert is rejected outright."""
    from app.services.sqlite_repository import SQLiteRepository

    settings = _sqlite_settings(tmp_path / "sqlite-target")
    repo = SQLiteRepository(settings)
    try:
        report = import_package(settings, postgres_package["package"])
        notebook = postgres_package["notebook"]

        assert report.error == ""
        assert report.tables["notebooks"].inserted == 1
        with repo._connect() as db:
            row = db.execute(
                "SELECT expected_questions, status, sync_origin FROM notebooks "
                "WHERE id=?",
                (notebook,),
            ).fetchone()
            reviewed = db.execute(
                "SELECT last_reviewed FROM knowledge_objects WHERE notebook_id=?",
                (notebook,),
            ).fetchone()
            rebuilt = db.execute(
                "SELECT last_rebuild_at FROM unified_kg_state WHERE notebook_id=?",
                (notebook,),
            ).fetchone()
            vector = db.execute(
                "SELECT vector FROM chunk_embeddings WHERE notebook_id=?",
                (notebook,),
            ).fetchone()
        # jsonb came back as the portable text form SQLite stores.
        assert json.loads(row["expected_questions"]) == QUESTIONS
        assert row["sync_origin"] == SOURCE_ENV
        assert row["status"] == "draft"
        assert reviewed["last_reviewed"] == ""
        assert rebuilt["last_rebuild_at"] == ""
        assert bytes(vector["vector"]) == VECTOR
    finally:
        repo.close()


# ------------------- two source environments racing the same notebook id


def _interleave_at_preflight(monkeypatch, run) -> None:
    """Same seam the SQLite suite uses (tests/test_sync_import.py) to make
    the genuinely-hard-to-schedule race between two source environments'
    preflight-to-claim windows deterministic: run the OTHER import once,
    right after this one's own ``_preflight`` call returns, before it
    proceeds to ``_claim_import``. Sequential simulation rather than real
    threads -- both are legitimate ways to exercise the race, and this one
    is deterministic."""
    from app.migration.sync import import_ as module

    real = module._preflight
    state = {"interleaved": False}

    def preflight_then_interleave(backend, conn, context):
        outcome = real(backend, conn, context)
        if not state["interleaved"]:
            state["interleaved"] = True
            run()
        return outcome

    monkeypatch.setattr(module, "_preflight", preflight_then_interleave)


def _race_library(
    tmp_path: Path, base_url: str, label: str, notebook_id: str, source_env: str
) -> Path:
    """A second, independent PostgreSQL schema whose only notebook carries a
    caller-chosen id -- needed to put two DIFFERENT source environments'
    packages in a race over the SAME not-yet-mirrored notebook id, which
    ``postgres_package``'s randomly-generated notebook id cannot set up.
    ``created_by`` is left NULL (the column is nullable, and the FK it
    carries permits NULL) so this does not also need an identity-mapping
    setup that is irrelevant to the race under test."""
    from app.repositories.postgres.repository import PostgresRepository

    with _isolated_postgres_scope(base_url) as scope:
        settings = _postgres_settings(scope.url, tmp_path / f"{label}-storage")
        repo = PostgresRepository(settings)
        try:
            with repo._write() as db:
                db.execute(
                    "INSERT INTO notebooks(id,name,purpose,primary_domain,status,"
                    "created_by,created_at,updated_at) "
                    "VALUES(%s,%s,%s,%s,%s,NULL,%s,%s)",
                    (notebook_id, label, "", "Semiconductor", "draft", MOMENT, MOMENT),
                )
            report = export_notebooks(
                settings,
                target_env=TARGET_ENV,
                out_dir=tmp_path / f"out-{label}",
                notebook_ids=[notebook_id],
                source_env=source_env,
            )
        finally:
            repo.close()
    return report.package_dir


def _package_id(package: Path) -> str:
    return json.loads(
        (package / "manifest.json").read_text(encoding="utf-8")
    )["package_id"]


def _sync_import_status(repo, package_id: str) -> str | None:
    row = _one(
        repo, "SELECT status FROM sync_imports WHERE package_id=%s", (package_id,)
    )
    return row["status"] if row else None


def test_two_source_environments_racing_the_same_notebook_id_the_loser_is_refused(
    target, tmp_path, monkeypatch
):
    """codex #772 round 14 P1's PostgreSQL half: two DIFFERENT source
    environments' packages racing to import the SAME not-yet-mirrored
    notebook id both pass ``_preflight``'s foreign-notebook check on a
    snapshot that genuinely has no row for it either way, and
    ``_claim_import``'s ``pg_advisory_xact_lock(hashtext(source_env))`` does
    not serialize the two against each other -- it is scoped to ONE source
    environment. ``_reserve_notebooks`` closes it by claiming the notebook id
    itself inside the declaration transaction: whichever package finishes
    first while the other's claim window is still open wins, and the loser's
    own claim must find the id already spoken for and refuse by name rather
    than silently reattribute the winner's mirror."""
    import os

    base_url = os.environ.get("TEST_POSTGRES_URL")
    if not base_url:
        pytest.skip("TEST_POSTGRES_URL is not configured")

    notebook_id = "shared-nb-pg-race"
    package_a = _race_library(tmp_path, base_url, "race-a", notebook_id, "env-a")
    package_b = _race_library(tmp_path, base_url, "race-b", notebook_id, "env-b")

    _interleave_at_preflight(
        monkeypatch, lambda: import_package(target["settings"], package_b)
    )

    with pytest.raises(SyncImportError) as failure:
        import_package(target["settings"], package_a)

    message = str(failure.value)
    assert notebook_id in message
    assert "env-b" in message

    repo = target["repo"]
    row = _one(
        repo, "SELECT sync_origin, status FROM notebooks WHERE id=%s", (notebook_id,)
    )
    assert row["sync_origin"] == "env-b"
    assert row["status"] == "draft"
    assert _sync_import_status(repo, _package_id(package_a)) is None
    assert _sync_import_status(repo, _package_id(package_b)) == "done"
