from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import threading
from typing import Any

import pytest

from app.core.config import Settings
from app.models.notebooks import NotebookCreate, NotebookUpdate
from app.repositories.ports import ChunkWrite, SourceElementWrite
from app.repositories.postgres.chunk_store import ChunkStore as PostgresChunkStore
from app.repositories.postgres.identity_store import IdentityStore as PostgresIdentityStore
from app.repositories.postgres.kg_build_job_store import (
    KgBuildAlreadyRunning as PostgresKgBuildAlreadyRunning,
    KgBuildJobStore as PostgresKgBuildJobStore,
)
from app.repositories.postgres.notebook_store import NotebookStore as PostgresNotebookStore
from app.repositories.postgres.schema_manifest import POSTGRES_ROWID_ORDINAL_TABLES
from app.repositories.postgres.sharing_store import SharingStore as PostgresSharingStore
from app.repositories.postgres.source_store import SourceStore as PostgresSourceStore
from app.repositories.sqlite.chunk_store import ChunkStore as SqliteChunkStore
from app.repositories.sqlite.identity_store import IdentityStore as SqliteIdentityStore
from app.repositories.sqlite.kg_build_job_store import (
    KgBuildAlreadyRunning as SqliteKgBuildAlreadyRunning,
    KgBuildJobStore as SqliteKgBuildJobStore,
)
from app.repositories.sqlite.notebook_store import NotebookStore as SqliteNotebookStore
from app.repositories.sqlite.sharing_store import SharingStore as SqliteSharingStore
from app.repositories.sqlite.source_store import SourceStore as SqliteSourceStore
from app.services.notebook_catalog import NotebookSummaryQuery


NOW = "2026-07-22T10:00:00+00:00"


@dataclass
class CoreStores:
    backend: str
    database: Any
    identity: Any
    notebooks: Any
    sharing: Any
    sources: Any
    chunks: Any
    jobs: Any
    already_running: type[RuntimeError]


def _new_id_factory():
    counters: dict[str, int] = {}

    def new_id(prefix: str) -> str:
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}-conformance-{counters[prefix]}"

    return new_id


@pytest.fixture(
    params=(
        "sqlite",
        pytest.param("postgres", marks=pytest.mark.postgres_integration),
    )
)
def core_stores(request, tmp_path) -> CoreStores:
    backend = request.param
    new_id = _new_id_factory()
    now = lambda: NOW
    model_cache: dict[str, object] = {}

    if backend == "sqlite":
        from app.repositories.sqlite.database import SqliteDatabase
        from app.repositories.sqlite.migrations import SqliteMigrator

        settings = Settings(
            database_url=f"sqlite:///{tmp_path / 'core-conformance.db'}",
            storage_dir=str(tmp_path / "storage"),
        )
        database = SqliteDatabase(settings, tmp_path)
        SqliteMigrator(database, settings).initialize()
        stores = CoreStores(
            backend=backend,
            database=database,
            identity=SqliteIdentityStore(database, settings, model_cache),
            notebooks=SqliteNotebookStore(database, new_id=new_id, now=now),
            sharing=SqliteSharingStore(
                database,
                settings,
                now=now,
                insert_row=SqliteSharingStore.insert_row_values,
            ),
            sources=SqliteSourceStore(database, now=now),
            chunks=SqliteChunkStore(database),
            jobs=SqliteKgBuildJobStore(database, new_id=new_id, now=now),
            already_running=SqliteKgBuildAlreadyRunning,
        )
        try:
            yield stores
        finally:
            database.close_local()
        return

    postgres_database = request.getfixturevalue("postgres_database")
    postgres_settings = request.getfixturevalue("postgres_settings")
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 6
    yield CoreStores(
        backend=backend,
        database=postgres_database,
        identity=PostgresIdentityStore(postgres_database, postgres_settings, model_cache),
        notebooks=PostgresNotebookStore(postgres_database, new_id=new_id, now=now),
        sharing=PostgresSharingStore(
            postgres_database,
            postgres_settings,
            now=now,
            insert_row=PostgresSharingStore.insert_row_values,
        ),
        sources=PostgresSourceStore(postgres_database, now=now),
        chunks=PostgresChunkStore(postgres_database),
        jobs=PostgresKgBuildJobStore(postgres_database, new_id=new_id, now=now),
        already_running=PostgresKgBuildAlreadyRunning,
    )


def _public_methods(cls: type) -> set[str]:
    return {
        name
        for name, value in cls.__dict__.items()
        if not name.startswith("_") and callable(value)
    }


def _write_sql(
    stores: CoreStores,
    sqlite_sql: str,
    postgres_sql: str,
    params: tuple[object, ...] = (),
) -> None:
    with stores.database.write() as connection:
        connection.execute(
            postgres_sql if stores.backend == "postgres" else sqlite_sql,
            params,
        )


def _fetch_one(
    stores: CoreStores,
    sqlite_sql: str,
    postgres_sql: str,
    params: tuple[object, ...] = (),
):
    with stores.database.connect() as connection:
        return connection.execute(
            postgres_sql if stores.backend == "postgres" else sqlite_sql,
            params,
        ).fetchone()


def _iso(value: object) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value)


class _EmptySummaryQueries:
    """Small neutral projection collaborator for exercising real from_row."""

    @staticmethod
    def visible_source_count(connection, notebook_id):
        return 0

    @staticmethod
    def knowledge_type_count_rows(connection, notebook_id, statuses):
        return []

    @staticmethod
    def mounted_bases_row(connection, notebook_id):
        return []

    @staticmethod
    def notebook_has_kg(connection, notebook_id):
        return False

    @staticmethod
    def visible_pending_kg_source_count(connection, notebook_id):
        return 0


@pytest.mark.parametrize(
    ("sqlite_cls", "postgres_cls"),
    (
        (SqliteIdentityStore, PostgresIdentityStore),
        (SqliteNotebookStore, PostgresNotebookStore),
        (SqliteSharingStore, PostgresSharingStore),
        (SqliteSourceStore, PostgresSourceStore),
        (SqliteChunkStore, PostgresChunkStore),
        (SqliteKgBuildJobStore, PostgresKgBuildJobStore),
    ),
)
def test_postgres_store_public_surfaces_cover_sqlite(sqlite_cls, postgres_cls):
    assert _public_methods(sqlite_cls) <= _public_methods(postgres_cls)


def test_identity_username_password_and_session_semantics(core_stores: CoreStores):
    store = core_stores.identity
    created = store.create_user("a00123456", "correct horse battery staple")
    assert created.username == "a00123456"
    assert store.authenticate_user("a00123456", "wrong") is None
    assert store.authenticate_user("a00123456", "correct horse battery staple").id == created.id
    with pytest.raises(ValueError, match="username already exists"):
        store.create_user("a00123456", "another password")

    token = store.create_session(created.id)
    assert store.resolve_session(token).id == created.id
    store.delete_session(token)
    assert store.resolve_session(token) is None


def test_identity_session_expiry_and_touch_throttle(core_stores: CoreStores):
    user = core_stores.identity.create_user("h00123456", "password-7")
    expired = core_stores.identity.create_session(user.id)
    _write_sql(
        core_stores,
        "UPDATE auth_sessions SET expires_at=? WHERE token=?",
        "UPDATE auth_sessions SET expires_at=%s WHERE token=%s",
        ("2000-01-01T00:00:00+00:00", expired),
    )
    assert core_stores.identity.resolve_session(expired) is None
    assert _fetch_one(
        core_stores,
        "SELECT 1 FROM auth_sessions WHERE token=?",
        "SELECT 1 FROM auth_sessions WHERE token=%s",
        (expired,),
    ) is None

    active = core_stores.identity.create_session(user.id)
    old_seen = "2000-01-01T00:00:00+00:00"
    _write_sql(
        core_stores,
        "UPDATE auth_sessions SET last_seen_at=?,expires_at=? WHERE token=?",
        "UPDATE auth_sessions SET last_seen_at=%s,expires_at=%s WHERE token=%s",
        (old_seen, "2099-01-01T00:00:00+00:00", active),
    )
    assert core_stores.identity.resolve_session(active).id == user.id
    touched = _fetch_one(
        core_stores,
        "SELECT last_seen_at FROM auth_sessions WHERE token=?",
        "SELECT last_seen_at FROM auth_sessions WHERE token=%s",
        (active,),
    )
    assert _iso(touched["last_seen_at"]) > old_seen


def test_identity_json_settings_and_monotonic_model_status(core_stores: CoreStores):
    user = core_stores.identity.create_user("o00123456", "password-14")
    initial = {
        "llm": {
            "base_url": "https://model.example/v1",
            "api_key": "secret",
            "model": "model-a",
        }
    }
    core_stores.identity.set_user_model_settings(user.id, initial)
    updated = core_stores.identity.patch_user_model_settings_atomic(
        user.id,
        {"llm": {"base_url": None, "api_key": "", "model": "model-b"}},
    )
    assert updated == {
        "llm": {"base_url": "https://model.example/v1", "model": "model-b"}
    }
    assert core_stores.identity.get_user_model_settings(user.id) == updated

    core_stores.identity.record_model_service_status(
        user.id,
        "llm",
        "new-fingerprint",
        "ok",
        12,
        "",
        "manual_test",
        "2030-01-01T00:02:00+00:00",
    )
    core_stores.identity.record_model_service_status(
        user.id,
        "llm",
        "old-fingerprint",
        "error",
        0,
        "upstream_error",
        "observed_failure",
        "2030-01-01T00:01:00+00:00",
    )
    status = core_stores.identity.get_model_service_statuses(user.id)["llm"]
    assert status == {
        "config_fingerprint": "new-fingerprint",
        "status": "ok",
        "latency_ms": 12,
        "code": "",
        "trigger": "manual_test",
        "checked_at": "2030-01-01T00:02:00.000000+00:00",
    }


def test_notebook_mount_and_sharing_semantics(core_stores: CoreStores):
    owner = core_stores.identity.create_user("b00123456", "password-1")
    reader = core_stores.identity.create_user("c00123456", "password-2")
    personal_id = core_stores.notebooks.create_row(
        NotebookCreate(name="Personal", purpose="  "), owner.id
    )
    base_id = core_stores.notebooks.create_row(
        NotebookCreate(name="Reference", purpose="manual"), owner.id
    )
    core_stores.notebooks.set_tier(base_id, "base")
    core_stores.notebooks.replace_mounts(personal_id, [base_id, base_id], owner.id)
    assert core_stores.notebooks.participant_notebook_ids(personal_id) == [
        personal_id,
        base_id,
    ]
    assert core_stores.notebooks.get_row(personal_id)["purpose_auto"] == 1

    token = core_stores.sharing.set_share_token(personal_id, "shr-first")
    assert token == "shr-first"
    assert core_stores.sharing.set_share_token(personal_id, "shr-second") == token
    core_stores.sharing.add_member(personal_id, reader.id)
    core_stores.sharing.add_member(personal_id, reader.id)
    assert core_stores.sharing.is_member(personal_id, reader.id)
    assert [row["username"] for row in core_stores.sharing.list_members(personal_id)] == [
        "c00123456"
    ]
    core_stores.sharing.clear_share(personal_id)
    assert core_stores.sharing.find_by_token(token) is None
    assert core_stores.sharing.list_members(personal_id) == []


def test_cross_owner_base_visibility_fails_closed_after_downgrade(
    core_stores: CoreStores,
):
    owner = core_stores.identity.create_user("i00123456", "password-8")
    publisher = core_stores.identity.create_user("j00123456", "password-9")
    personal_id = core_stores.notebooks.create_row(NotebookCreate(name="Active"), owner.id)
    base_id = core_stores.notebooks.create_row(
        NotebookCreate(name="Published secret name"), publisher.id
    )
    core_stores.notebooks.set_tier(base_id, "base")
    core_stores.notebooks.replace_mounts(personal_id, [base_id], owner.id)
    assert core_stores.notebooks.participant_notebook_ids(personal_id) == [
        personal_id,
        base_id,
    ]

    core_stores.notebooks.set_tier(base_id, "personal")
    assert core_stores.notebooks.participant_notebook_ids(personal_id) == [personal_id]
    edge = core_stores.notebooks.list_mount_edges_for_notebook(personal_id)[0]
    assert edge["active"] is False
    assert edge["name"] == "已不可用的知识库"


def test_replace_mounts_reuses_one_batch_timestamp(core_stores: CoreStores):
    owner = core_stores.identity.create_user("r00123456", "password-17")
    active_id = core_stores.notebooks.create_row(
        NotebookCreate(name="Mount active"), owner.id
    )
    base_ids = [
        core_stores.notebooks.create_row(NotebookCreate(name=name), owner.id)
        for name in ("Mount base A", "Mount base B")
    ]
    calls: list[str] = []

    def increasing_clock() -> str:
        value = f"2026-07-22T10:00:0{len(calls)}+00:00"
        calls.append(value)
        return value

    store = type(core_stores.notebooks)(
        core_stores.database,
        new_id=_new_id_factory(),
        now=increasing_clock,
    )
    store.replace_mounts(active_id, base_ids, owner.id)

    with core_stores.database.connect() as connection:
        rows = connection.execute(
            (
                "SELECT created_at FROM notebook_bases WHERE notebook_id=%s "
                'ORDER BY base_notebook_id COLLATE "C"'
                if core_stores.backend == "postgres"
                else "SELECT created_at FROM notebook_bases WHERE notebook_id=? "
                "ORDER BY base_notebook_id"
            ),
            (active_id,),
        ).fetchall()
    assert calls == ["2026-07-22T10:00:00+00:00"]
    assert [_iso(row["created_at"]) for row in rows] == [calls[0], calls[0]]


def test_notebook_raw_rows_feed_neutral_summary_json_lists(
    core_stores: CoreStores,
):
    owner = core_stores.identity.create_user("s00123456", "password-18")
    notebook_id = core_stores.notebooks.create_row(
        NotebookCreate(name="Raw JSON boundary"), owner.id
    )
    core_stores.notebooks.update_row(
        notebook_id,
        NotebookUpdate(
            expected_questions=["怎么验证？", "How to verify?"],
            source_types=["pdf", "markdown"],
            taxonomy=["analog", "模拟"],
        ),
    )
    core_stores.sharing.set_share_token(notebook_id, "shr-raw-json")
    summaries = NotebookSummaryQuery(core_stores.database, _EmptySummaryQueries())

    rows = [
        core_stores.notebooks.get_row(notebook_id),
        core_stores.sharing.notebook_row(notebook_id),
    ]
    with core_stores.database.connect() as connection:
        rows.append(core_stores.sharing.notebook_row_on(connection, notebook_id))
        projected = [summaries.from_row(connection, row) for row in rows]

    for summary in projected:
        assert summary.expected_questions == ["怎么验证？", "How to verify?"]
        assert summary.source_types == ["pdf", "markdown"]
        assert summary.taxonomy == ["analog", "模拟"]

    # This sharing-list projection does not expose any JSON notebook columns,
    # so it cannot accidentally cross the raw-row summary boundary.
    shared_rows = core_stores.sharing.list_shared_by_owner(owner.id)
    assert set(shared_rows[0].keys()) == {"id", "name", "share_token"}


def test_copy_snapshot_reinsertion_preserves_all_copied_ordinal_table_order(
    core_stores: CoreStores,
):
    owner = core_stores.identity.create_user("t00123456", "password-19")
    source_notebook_id = core_stores.notebooks.create_row(
        NotebookCreate(name="Ordinal source"), owner.id
    )
    destination_notebook_id = core_stores.notebooks.create_row(
        NotebookCreate(name="Ordinal destination"), owner.id
    )
    for source_id, notebook_id in (
        ("src-ordinal-source", source_notebook_id),
        ("src-ordinal-destination", destination_notebook_id),
    ):
        core_stores.sources.insert_source(
            source_id=source_id,
            notebook_id=notebook_id,
            title=source_id,
            source_type="markdown",
            status="parsed",
            parse_status="parsed",
            file_name=f"{source_id}.md",
            file_path=f"uploads/{source_id}.md",
            file_size=1,
            file_hash=source_id,
            summary="",
            doc_type="",
        )

    # Physical/insertion and id order is a,z. The explicit observable order is
    # then reversed to z,a, reproducing imported PostgreSQL rows whose heap
    # order does not match their historical SQLite rowid/ordinal order.
    with core_stores.database.write() as connection:
        core_stores.sources.replace_elements(
            connection,
            "src-ordinal-source",
            [
                SourceElementWrite("el-a", "paragraph", "a", "a", {"rank": 2}),
                SourceElementWrite("el-z", "paragraph", "z", "z", {"rank": 1}),
            ],
            created_at=NOW,
        )
    core_stores.chunks.replace_source_chunks(
        "src-ordinal-source",
        source_notebook_id,
        [
            ChunkWrite("chunk-a", "a", "a", ("el-a",)),
            ChunkWrite("chunk-z", "z", "z", ("el-z",)),
        ],
        created_at=NOW,
    )
    for object_id, rank in (("ko-a", 2), ("ko-z", 1)):
        _write_sql(
            core_stores,
            "INSERT INTO knowledge_objects"
            "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            "INSERT INTO knowledge_objects"
            "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,"
            "created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s)",
            (
                object_id,
                source_notebook_id,
                "concept",
                "approved",
                owner.id,
                f'{{"name":"{object_id}","rank":{rank}}}',
                "[]",
                "src-ordinal-source",
                NOW,
                NOW,
            ),
        )

    for table, a_id, z_id in (
        ("source_elements", "el-a", "el-z"),
        ("chunks", "chunk-a", "chunk-z"),
        ("knowledge_objects", "ko-a", "ko-z"),
    ):
        _write_sql(
            core_stores,
            f"UPDATE {table} SET rowid=? WHERE id=?",
            f"UPDATE {table} SET ordinal=%s WHERE id=%s",
            (800_001, a_id),
        )
        _write_sql(
            core_stores,
            f"UPDATE {table} SET rowid=? WHERE id=?",
            f"UPDATE {table} SET ordinal=%s WHERE id=%s",
            (800_000, z_id),
        )

    snapshot = core_stores.sharing.snapshot_copy_rows(source_notebook_id)
    expected_source_ids = {
        "source_elements": ["el-z", "el-a"],
        "chunks": ["chunk-z", "chunk-a"],
        "knowledge_objects": ["ko-z", "ko-a"],
    }
    assert set(POSTGRES_ROWID_ORDINAL_TABLES) & set(snapshot) == set(
        expected_source_ids
    )
    for table, expected_ids in expected_source_ids.items():
        assert [row["id"] for row in snapshot[table]] == expected_ids

    element_map = {old: f"copy-{old}" for old in expected_source_ids["source_elements"]}
    for table in ("source_elements", "chunks", "knowledge_objects"):
        copied_rows = []
        for row in snapshot[table]:
            copied = dict(row)
            copied["id"] = f"copy-{row['id']}"
            if table == "source_elements":
                copied["source_id"] = "src-ordinal-destination"
            elif table == "chunks":
                copied["notebook_id"] = destination_notebook_id
                copied["source_id"] = "src-ordinal-destination"
                copied["element_ids"] = (
                    f'["{element_map["el-z"]}"]'
                    if row["id"] == "chunk-z"
                    else f'["{element_map["el-a"]}"]'
                )
            else:
                copied["notebook_id"] = destination_notebook_id
                copied["source_id"] = "src-ordinal-destination"
            copied_rows.append(copied)
        core_stores.sharing.insert_copy_rows(table, copied_rows, chunk_size=100)

    for table, expected_ids in expected_source_ids.items():
        where_column = "source_id" if table == "source_elements" else "notebook_id"
        where_value = (
            "src-ordinal-destination"
            if table == "source_elements"
            else destination_notebook_id
        )
        with core_stores.database.connect() as connection:
            rows = connection.execute(
                (
                    f"SELECT id FROM {table} WHERE {where_column}=%s ORDER BY ordinal"
                    if core_stores.backend == "postgres"
                    else f"SELECT id FROM {table} WHERE {where_column}=? ORDER BY rowid"
                ),
                (where_value,),
            ).fetchall()
        assert [row["id"] for row in rows] == [
            f"copy-{old_id}" for old_id in expected_ids
        ]


def test_copy_snapshot_excludes_backend_ordinals_and_serializes_json(
    core_stores: CoreStores,
):
    owner = core_stores.identity.create_user("n00123456", "password-13")
    notebook_id = core_stores.notebooks.create_row(NotebookCreate(name="Copy"), owner.id)
    core_stores.sources.insert_source(
        source_id="src-copy",
        notebook_id=notebook_id,
        title="Copy source",
        source_type="markdown",
        status="parsed",
        parse_status="parsed",
        file_name="copy.md",
        file_path="uploads/copy.md",
        file_size=1,
        file_hash="hash",
        summary="",
        doc_type="",
    )
    with core_stores.database.write() as connection:
        core_stores.sources.replace_elements(
            connection,
            "src-copy",
            [SourceElementWrite("el-copy", "paragraph", "p", "body", {"k": 1})],
            created_at=NOW,
        )
    core_stores.chunks.replace_source_chunks(
        "src-copy",
        notebook_id,
        [ChunkWrite("chunk-copy", "body", "p", ("el-copy",))],
        created_at=NOW,
    )
    snapshot = core_stores.sharing.snapshot_copy_rows(notebook_id)
    assert "ordinal" not in snapshot["source_elements"][0]
    assert "ordinal" not in snapshot["chunks"][0]
    assert isinstance(snapshot["source_elements"][0]["metadata"], str)
    assert isinstance(snapshot["chunks"][0]["element_ids"], str)

    destination = dict(snapshot["notebooks"][0])
    destination.update(id="nb-copy-destination", status="copying")
    core_stores.sharing.insert_copy_rows(
        "notebooks", [destination], chunk_size=100
    )
    assert core_stores.sharing.notebook_row("nb-copy-destination")["status"] == "copying"
    core_stores.sharing.compensate_copy("nb-copy-destination")
    assert core_stores.sharing.notebook_row("nb-copy-destination") is None


def test_source_jsonb_hydration_and_ordinal_sample_order(core_stores: CoreStores):
    owner = core_stores.identity.create_user("d00123456", "password-3")
    notebook_id = core_stores.notebooks.create_row(NotebookCreate(name="Sources"), owner.id)
    core_stores.sources.insert_source(
        source_id="src-core",
        notebook_id=notebook_id,
        title="Core source",
        source_type="markdown",
        status="parsed",
        parse_status="parsed",
        file_name="core.md",
        file_path="uploads/core.md",
        file_size=10,
        file_hash="hash",
        summary="summary",
        doc_type="academic_paper",
    )
    elements = (
        SourceElementWrite("el-z", "paragraph", "first", "first body", {"rank": 1}),
        SourceElementWrite("el-a", "paragraph", "second", "second body", {"rank": 2}),
    )
    with core_stores.database.write() as connection:
        core_stores.sources.replace_elements(
            connection, "src-core", elements, created_at=NOW
        )

    hydrated = {item.id: item for item in core_stores.sources.source_elements("src-core")}
    assert hydrated["el-z"].metadata == {"rank": 1}
    assert hydrated["el-a"].metadata == {"rank": 2}
    assert core_stores.sources.notebook_element_sample(notebook_id, max_chars=1000) == [
        {"location_label": "first", "text": "first body"},
        {"location_label": "second", "text": "second body"},
    ]


def test_source_visibility_physical_count_and_delete_cascade(core_stores: CoreStores):
    owner = core_stores.identity.create_user("k00123456", "password-10")
    notebook_id = core_stores.notebooks.create_row(NotebookCreate(name="Visibility"), owner.id)
    for source_id, source_type in (
        ("src-visible", "markdown"),
        ("src-memory", "memory"),
        ("src-knowhow", "knowhow"),
    ):
        core_stores.sources.insert_source(
            source_id=source_id,
            notebook_id=notebook_id,
            title=source_id,
            source_type=source_type,
            status="parsed",
            parse_status="parsed",
            file_name=f"{source_id}.md",
            file_path=f"uploads/{source_id}.md",
            file_size=1,
            file_hash="hash",
            summary="",
            doc_type="",
        )
    assert [item.id for item in core_stores.sources.list_sources(notebook_id)] == [
        "src-visible"
    ]
    physical = _fetch_one(
        core_stores,
        "SELECT COUNT(*) AS c FROM sources WHERE notebook_id=?",
        "SELECT COUNT(*) AS c FROM sources WHERE notebook_id=%s",
        (notebook_id,),
    )
    assert physical["c"] == 3

    with core_stores.database.write() as connection:
        core_stores.sources.replace_elements(
            connection,
            "src-visible",
            [SourceElementWrite("el-delete", "paragraph", "p", "body", {})],
            created_at=NOW,
        )
    core_stores.chunks.replace_source_chunks(
        "src-visible",
        notebook_id,
        [ChunkWrite("chunk-delete", "body", "p", ("el-delete",))],
        created_at=NOW,
    )
    with core_stores.database.write() as connection:
        core_stores.sources.delete_source_row(connection, "src-visible")
    assert _fetch_one(
        core_stores,
        "SELECT 1 FROM source_elements WHERE id=?",
        "SELECT 1 FROM source_elements WHERE id=%s",
        ("el-delete",),
    ) is None
    assert _fetch_one(
        core_stores,
        "SELECT 1 FROM chunks WHERE id=?",
        "SELECT 1 FROM chunks WHERE id=%s",
        ("chunk-delete",),
    ) is None


def test_latest_extraction_run_uses_rowid_or_ordinal_tie_break(
    core_stores: CoreStores,
):
    owner = core_stores.identity.create_user("l00123456", "password-11")
    notebook_id = core_stores.notebooks.create_row(NotebookCreate(name="Runs"), owner.id)
    core_stores.sources.insert_source(
        source_id="src-runs",
        notebook_id=notebook_id,
        title="Runs",
        source_type="markdown",
        status="extracted",
        parse_status="extracted",
        file_name="runs.md",
        file_path="uploads/runs.md",
        file_size=1,
        file_hash="hash",
        summary="",
        doc_type="",
    )
    for run_id, status, error in (
        ("run-first", "completed", "windows_failed=1/2"),
        ("run-second", "failed", ""),
    ):
        _write_sql(
            core_stores,
            "INSERT INTO extraction_runs"
            "(id,notebook_id,source_id,run_type,status,error_message,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            "INSERT INTO extraction_runs"
            "(id,notebook_id,source_id,run_type,status,error_message,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (run_id, notebook_id, "src-runs", "kg", status, error, NOW, NOW),
        )
    assert core_stores.sources.list_sources(notebook_id)[0].extraction_warning is None


def test_paper_metadata_json_and_author_search_roundtrip(core_stores: CoreStores):
    owner = core_stores.identity.create_user("p00123456", "password-15")
    notebook_id = core_stores.notebooks.create_row(NotebookCreate(name="Paper"), owner.id)
    core_stores.sources.insert_source(
        source_id="src-paper",
        notebook_id=notebook_id,
        title="Imported document",
        source_type="pdf",
        status="parsed",
        parse_status="parsed",
        file_name="paper.pdf",
        file_path="uploads/paper.pdf",
        file_size=1,
        file_hash="hash",
        summary="",
        doc_type="academic_paper",
    )
    core_stores.sources.upsert_paper_meta(
        "src-paper",
        notebook_id,
        {
            "is_paper": True,
            "paper_title": "FinFET Study",
            "venue": "VLSI",
            "pub_year": 2026,
            "doi": "10.1/example",
            "keywords": ["FinFET", "scaling"],
            "model": "test-model",
            "raw_json": '{"is_paper":true}',
            "authors": [
                {"position": 0, "name": "Alice Wu", "affiliation": "Lab"}
            ],
        },
    )
    detail = core_stores.sources.get_source("src-paper")
    assert detail.paper_meta_status == "has_meta"
    assert detail.authors == ["Alice Wu"]
    assert detail.paper_meta is not None
    assert detail.paper_meta.keywords == ["FinFET", "scaling"]
    result = core_stores.sources.list_sources_page(notebook_id, q="alice wu")
    assert result.total_count == 1
    assert result.items[0].id == "src-paper"


def test_chunk_jsonb_and_ordinal_language_probe(core_stores: CoreStores):
    owner = core_stores.identity.create_user("e00123456", "password-4")
    notebook_id = core_stores.notebooks.create_row(NotebookCreate(name="Chunks"), owner.id)
    core_stores.sources.insert_source(
        source_id="src-chunks",
        notebook_id=notebook_id,
        title="Chunk source",
        source_type="markdown",
        status="parsed",
        parse_status="parsed",
        file_name="chunks.md",
        file_path="uploads/chunks.md",
        file_size=1,
        file_hash="h",
        summary="",
        doc_type="",
    )
    rows = tuple(
        ChunkWrite(f"chunk-{index:02d}", f"text-{index:02d}", "section", ("el",))
        for index in range(35)
    )
    core_stores.chunks.replace_source_chunks(
        "src-chunks", notebook_id, rows, created_at=NOW
    )
    with core_stores.database.connect() as connection:
        probe = core_stores.chunks.language_probe_rows(connection, notebook_id)
        stored = core_stores.chunks.rows_by_ids(
            connection, ["chunk-00", "chunk-34"]
        )
    assert {row["text"] for row in probe} == {
        *(f"text-{index:02d}" for index in range(30)),
        *(f"text-{index:02d}" for index in range(5, 35)),
    }
    by_id = {row["id"]: row for row in stored}
    assert by_id["chunk-00"]["element_ids"] in (["el"], '["el"]')


def test_kg_build_single_flight_conditional_updates_and_latest_ordinal(
    core_stores: CoreStores,
):
    owner = core_stores.identity.create_user("f00123456", "password-5")
    notebook_id = core_stores.notebooks.create_row(NotebookCreate(name="Jobs"), owner.id)
    first = core_stores.jobs.create_job(notebook_id, owner.id, "incremental", 2)
    with pytest.raises(core_stores.already_running):
        core_stores.jobs.create_job(notebook_id, owner.id, "rebuild", 2)
    assert core_stores.jobs.record_source_result(first["id"], succeeded=True)
    assert core_stores.jobs.finish(first["id"], "succeeded")
    assert not core_stores.jobs.record_source_result(first["id"], succeeded=True)

    second = core_stores.jobs.create_job(notebook_id, owner.id, "rebuild", 1)
    assert second["id"] != first["id"]
    assert core_stores.jobs.latest(notebook_id)["id"] == second["id"]


def test_kg_build_single_flight_is_atomic_across_two_connections(
    core_stores: CoreStores,
):
    owner = core_stores.identity.create_user("m00123456", "password-12")
    notebook_id = core_stores.notebooks.create_row(NotebookCreate(name="Race"), owner.id)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    failures: list[BaseException] = []
    lock = threading.Lock()

    def create(mode: str) -> None:
        try:
            barrier.wait(timeout=2)
            core_stores.jobs.create_job(notebook_id, owner.id, mode, 1)
            result = "created"
        except core_stores.already_running:
            result = "conflict"
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)
            return
        with lock:
            outcomes.append(result)

    workers = [
        threading.Thread(target=create, args=("incremental",)),
        threading.Thread(target=create, args=("rebuild",)),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)
    assert not failures
    assert not any(worker.is_alive() for worker in workers)
    assert sorted(outcomes) == ["conflict", "created"]


def test_notebook_updates_store_json_without_backend_leak(core_stores: CoreStores):
    owner = core_stores.identity.create_user("g00123456", "password-6")
    notebook_id = core_stores.notebooks.create_row(NotebookCreate(name="Before"), owner.id)
    core_stores.notebooks.update_row(
        notebook_id,
        NotebookUpdate(
            name="After",
            expected_questions=["Q1", "Q2"],
            source_types=["pdf"],
            taxonomy=["analog"],
        ),
    )
    row = core_stores.notebooks.get_row(notebook_id)
    assert row["name"] == "After"
    assert row["expected_questions"] in (["Q1", "Q2"], '["Q1", "Q2"]')


def test_notebook_delete_returns_paths_and_removes_orphan_embeddings(
    core_stores: CoreStores,
):
    owner = core_stores.identity.create_user("q00123456", "password-16")
    notebook_id = core_stores.notebooks.create_row(NotebookCreate(name="Delete"), owner.id)
    core_stores.sources.insert_source(
        source_id="src-delete-notebook",
        notebook_id=notebook_id,
        title="Delete",
        source_type="pdf",
        status="parsed",
        parse_status="parsed",
        file_name="delete.pdf",
        file_path="uploads/delete.pdf",
        file_size=1,
        file_hash="hash",
        summary="",
        doc_type="",
    )
    vector: object = b"\x00\x01" if core_stores.backend == "postgres" else "[1.0]"
    _write_sql(
        core_stores,
        "INSERT INTO knowledge_embeddings(object_id,notebook_id,vector,created_at) "
        "VALUES (?,?,?,?)",
        "INSERT INTO knowledge_embeddings(object_id,notebook_id,vector,created_at) "
        "VALUES (%s,%s,%s,%s)",
        ("ko-delete", notebook_id, vector, NOW),
    )
    assert core_stores.notebooks.delete_row_and_orphan_embeddings(notebook_id) == [
        "uploads/delete.pdf"
    ]
    assert _fetch_one(
        core_stores,
        "SELECT 1 FROM notebooks WHERE id=?",
        "SELECT 1 FROM notebooks WHERE id=%s",
        (notebook_id,),
    ) is None
    assert _fetch_one(
        core_stores,
        "SELECT 1 FROM knowledge_embeddings WHERE object_id=?",
        "SELECT 1 FROM knowledge_embeddings WHERE object_id=%s",
        ("ko-delete",),
    ) is None
