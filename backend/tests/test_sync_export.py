"""守卫: 按笔记本的全量导出器 (app.migration.sync.export) 与包格式
(app.migration.sync.package)。对应 docs/incremental-sync-design.md §8 的
``from_seq = 0`` 特例。

本文件是 SQLite 泳道；PostgreSQL 泳道在 tests/postgres/test_sync_export_pg.py,
那边盯的是两端行编码的差异(jsonb → 文本、timestamptz → UTC ISO 文本、bytea →
``$bytes``)。这里盯的是圈定与包结构本身: 哪些笔记本进包、每张表的 scope SQL
圈到了什么、包里有哪些文件、写入顺序、校验和与可复现性。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core.config import Settings
from app.migration.sync import export as export_module
from app.migration.sync.export import (
    ExportReport,
    SyncExportError,
    export_notebooks,
)
from app.migration.sync.manifest import MappingKind, spec_for, synced_tables
from app.migration.sync.package import (
    BYTES_KEY,
    CHECKSUMS_NAME,
    DELETES_NAME,
    KG_EPOCHS_NAME,
    MANIFEST_NAME,
    PACKAGE_FORMAT_VERSION,
    USERS_NAME,
    decode_row,
    decode_value,
    encode_row,
    encode_value,
    rows_path,
    utc_timestamp_text,
)
from app.models.schemas import NotebookCreate
from app.repositories.ports import UploadedSourceFile
from app.services.sqlite_repository import SQLiteRepository


SOURCE_ENV = "dev"
TARGET_ENV = "prod"
MOMENT = "2026-01-01T00:00:00+00:00"
VECTOR = bytes(range(32))


def _settings(tmp_path, monkeypatch) -> Settings:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'sync.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return Settings()


# ------------------------------------------------------------------- seeds


def _upload(repo, notebook_id, *, name="note.txt", body=b"alpha beta gamma\n" * 8):
    return repo.upload_sources(
        notebook_id,
        [
            UploadedSourceFile(
                file_name=name,
                content_type="text/plain",
                content=body,
                doc_type="",
                doc_type_explicit=False,
            )
        ],
    )


def _add_filled_row(repo, notebook_id: str, title: str) -> tuple[str, str]:
    """A knowhow table with one row whose cells are non-empty -- an empty-cell
    row writes no knowhow_cells at all, which would make the two-hop scope
    assertion vacuous. Returns ``(table_id, row_id)``."""
    table_id = repo.create_knowhow_table(
        notebook_id, title, "",
        [{"name": "Topic", "role": "anchor"}], created_by="user-local",
    )
    columns = repo.get_knowhow_table(table_id)["columns"]
    row_id = repo.add_knowhow_row(
        table_id, {column["id"]: f"{title} value" for column in columns},
        actor="user-local",
    )
    return table_id, row_id


def _seed_cell_code(repo, row_id: str, author: str) -> None:
    """knowhow_cell_code is the only synced table whose USER column is named
    ``updated_by``; seeding one keeps the users.jsonl closure test honest."""
    with repo._write() as db:
        column = db.execute(
            "SELECT column_id FROM knowhow_cells WHERE row_id=? LIMIT 1", (row_id,)
        ).fetchone()
        assert column is not None, "seeded row has no cell to attach code to"
        db.execute(
            "INSERT INTO knowhow_cell_code(id, row_id, column_id, code_text, "
            "language, updated_by, cell_content_hash, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (f"code-{row_id}", row_id, column["column_id"], "print(1)", "python",
             author, "hash", MOMENT, MOMENT),
        )


def _seed_embedding(repo, notebook_id: str) -> None:
    """A real blob column the package must carry as ``$bytes``. Written
    directly because the offline test lane has no embedding model; the column
    shape is what matters, not who produced the vector."""
    with repo._write() as db:
        chunk = db.execute(
            "SELECT id FROM chunks WHERE notebook_id=? ORDER BY id LIMIT 1",
            (notebook_id,),
        ).fetchone()
        assert chunk is not None, "seed upload produced no chunk"
        db.execute(
            "INSERT INTO chunk_embeddings(chunk_id, notebook_id, vector, created_at) "
            "VALUES(?,?,?,?)",
            (chunk["id"], notebook_id, VECTOR, MOMENT),
        )


def _seed_user(repo, user_id: str) -> str:
    with repo._write() as db:
        db.execute(
            "INSERT OR IGNORE INTO users(id, email, display_name, role, status, "
            "username, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (user_id, f"{user_id}@example.invalid", user_id.upper(), "member",
             "active", user_id, MOMENT, MOMENT),
        )
    return user_id


def _grant_group(repo, notebook_id: str, group_id: str, member: str) -> None:
    """A group plus the authorization edge that is the ONLY thing tying it to
    a notebook. Written as raw rows: what is under test is the exporter's
    scope rule, and no facade seam creates a grant without going through the
    sharing service's own policy."""
    with repo._write() as db:
        db.execute(
            "INSERT INTO groups(id,name,kind,description,created_by,created_at,"
            "updated_at,owner_id) VALUES(?,?,?,?,?,?,?,?)",
            (group_id, group_id, "team", "", "user-local", MOMENT, MOMENT, member),
        )
        db.execute(
            "INSERT INTO group_members(group_id,user_id,role,added_at,added_by) "
            "VALUES(?,?,?,?,?)",
            (group_id, member, "admin", MOMENT, "user-local"),
        )
        db.execute(
            "INSERT INTO notebook_grants(id,notebook_id,principal_type,principal_id,"
            "role,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
            (f"g-{group_id}", notebook_id, "group", group_id, "reader",
             "user-local", MOMENT),
        )
        db.execute(
            "INSERT INTO notebook_grants(id,notebook_id,principal_type,principal_id,"
            "role,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
            (f"u-{group_id}", notebook_id, "user", member, "reader",
             "user-local", MOMENT),
        )


def _seed_notebook(repo, name: str, *, member: str) -> str:
    """One notebook with material on every scope shape the exporter resolves:
    NOTEBOOK (sources/chunks), PARENT one hop (source_elements), PARENT two
    hops (knowhow_cells, knowhow_cell_code), PARENT via memory_items, and
    GLOBAL over both edges (a group grant, and an object-type reference)."""
    notebook = repo.create_notebook(NotebookCreate(name=name))
    _upload(repo, notebook.id, name=f"{name}.txt")
    _table_id, row_id = _add_filled_row(repo, notebook.id, f"{name}-table")
    _seed_cell_code(repo, row_id, _seed_user(repo, f"{name}-coder"))
    repo.create_memory_candidate(
        notebook.id, "user-local", None, f"{name}-memory-request",
        f"{name} memory", "记忆正文", ["t"], "test",
    )
    _seed_embedding(repo, notebook.id)
    _grant_group(repo, notebook.id, f"grp-{name}", _seed_user(repo, member))
    return notebook.id


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    return _settings(tmp_path, monkeypatch)


@pytest.fixture
def repo(settings):
    repository = SQLiteRepository(settings)
    try:
        yield repository
    finally:
        repository.close()


@pytest.fixture
def seeded(repo, settings):
    """Exported notebook + a second one that must not leak into the package."""
    exported = _seed_notebook(repo, "exported", member="alice")
    other = _seed_notebook(repo, "other", member="bob")
    return {
        "repo": repo,
        "settings": settings,
        "exported": exported,
        "other": other,
    }


def _export(settings, out_dir: Path, notebook_ids) -> ExportReport:
    return export_notebooks(
        settings,
        target_env=TARGET_ENV,
        out_dir=out_dir,
        notebook_ids=notebook_ids,
        source_env=SOURCE_ENV,
    )


def _rows(package: Path, table: str) -> list[dict]:
    text = (package / rows_path(table)).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def _manifest(package: Path) -> dict:
    return json.loads((package / MANIFEST_NAME).read_text(encoding="utf-8"))


def _count(repo, statement: str, params=()) -> int:
    with repo._connect() as db:
        return int(db.execute(statement, params).fetchone()[0])


# ------------------------------------------------------------ row encoding


def test_bytes_round_trip_through_the_package_encoding():
    payload = bytes(range(256))
    encoded = encode_value(payload)
    assert set(encoded) == {BYTES_KEY}
    assert isinstance(encoded[BYTES_KEY], str)
    assert decode_value(encoded) == payload


def test_scalars_pass_through_unchanged():
    for value in (None, 0, 1, -7, 3.5, "", "文本", "0"):
        assert encode_value(value) == value
        assert decode_value(value) == value
        assert decode_value(encode_value(value)) == value


def test_booleans_take_the_sqlite_shape():
    assert encode_value(True) == 1
    assert encode_value(False) == 0


def test_encode_value_never_touches_timestamp_looking_text():
    """Timestamp normalization is a COLUMN decision made by the exporter. The
    value encoder must not sniff: a text column whose content happens to be an
    ISO instant is content, not a timestamp."""
    assert encode_value("2026-01-01T08:00:00+08:00") == "2026-01-01T08:00:00+08:00"


def test_a_text_column_holding_a_json_object_is_not_decoded_as_bytes():
    assert decode_value('{"$bytes": "AAA="}') == '{"$bytes": "AAA="}'
    assert decode_value({BYTES_KEY: "AAA=", "other": 1}) == {BYTES_KEY: "AAA=", "other": 1}


def test_row_helpers_are_inverse():
    row = {"id": "a", "vector": b"\x00\x01", "text": "x", "n": None}
    assert decode_row(encode_row(row)) == row


# --------------------------------------------------- timestamp COLUMN rules


def test_offset_timestamps_are_folded_to_utc():
    assert utc_timestamp_text("2026-01-01T08:00:00+08:00") == "2026-01-01T00:00:00+00:00"
    assert utc_timestamp_text("2026-01-01T00:00:00Z") == "2026-01-01T00:00:00+00:00"
    assert utc_timestamp_text(
        datetime(2026, 1, 1, 8, tzinfo=timezone(timedelta(hours=8)))
    ) == "2026-01-01T00:00:00+00:00"


def test_a_missing_instant_stays_null():
    assert utc_timestamp_text(None) is None


def test_a_naive_timestamp_is_never_given_a_zone():
    for value in ("2026-01-01T00:00:00", "2026-01-01 00:00:00", "2026-01-01", ""):
        assert utc_timestamp_text(value) == value


# ------------------------------------------------------------ package shape


def test_package_has_every_declared_file(seeded, tmp_path):
    report = _export(seeded["settings"], tmp_path / "out", [seeded["exported"]])
    package = report.package_dir

    assert package.name.startswith(f"sync-{SOURCE_ENV}-0-0-")
    assert package.name.endswith(report.package_id[:8])
    assert not package.with_name(package.name + ".tmp").exists()
    for name in (MANIFEST_NAME, CHECKSUMS_NAME, USERS_NAME, DELETES_NAME, KG_EPOCHS_NAME):
        assert (package / name).is_file(), name
    for table in synced_tables():
        assert (package / rows_path(table)).is_file(), table
    assert (package / DELETES_NAME).read_text() == ""
    assert (package / KG_EPOCHS_NAME).read_text() == ""


def test_manifest_records_the_full_export_contract(seeded, tmp_path):
    report = _export(seeded["settings"], tmp_path / "out", [seeded["exported"]])
    package = report.package_dir
    manifest = _manifest(package)

    assert manifest["format_version"] == PACKAGE_FORMAT_VERSION
    assert manifest["package_id"] == report.package_id
    assert manifest["source_env"] == SOURCE_ENV
    assert manifest["target_env"] == TARGET_ENV
    assert manifest["from_seq"] == 0 and manifest["to_seq"] == 0
    assert manifest["notebooks"] == [seeded["exported"]]
    assert set(manifest["schema_pair"]) == {"sqlite_version", "postgres_version"}
    assert manifest["embed_runtime_dim"] == seeded["settings"].embed_runtime_dim
    assert set(manifest["tables"]) == set(synced_tables())
    assert manifest["checksums_sha256"] == hashlib.sha256(
        (package / CHECKSUMS_NAME).read_bytes()
    ).hexdigest()
    assert report.mode == "full"


def test_manifest_is_the_last_file_written_and_the_rename_is_atomic(
    seeded, tmp_path, monkeypatch
):
    """The package-complete marker only works if the manifest really is last.
    Recorded at the writer, not inferred from mtimes."""
    order: list[str] = []
    original = export_module._PackageWriter._recorded

    def spy(self, relative, digest, size):
        order.append(relative)
        return original(self, relative, digest, size)

    monkeypatch.setattr(export_module._PackageWriter, "_recorded", spy)
    report = _export(seeded["settings"], tmp_path / "out", [seeded["exported"]])

    assert order[-1] == MANIFEST_NAME
    assert order[-2] == CHECKSUMS_NAME
    assert order.count(MANIFEST_NAME) == 1
    # Assembled elsewhere, then renamed: no staging directory survives, and
    # the final directory never existed half-written.
    assert not report.package_dir.with_name(report.package_dir.name + ".tmp").exists()
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [
        report.package_dir.name
    ]


def test_checksums_match_every_file_but_the_manifest(seeded, tmp_path):
    report = _export(seeded["settings"], tmp_path / "out", [seeded["exported"]])
    package = report.package_dir
    checksums = json.loads((package / CHECKSUMS_NAME).read_text(encoding="utf-8"))

    on_disk = {
        path.relative_to(package).as_posix()
        for path in package.rglob("*")
        if path.is_file()
    }
    assert set(checksums) == on_disk - {MANIFEST_NAME, CHECKSUMS_NAME}
    for relative, digest in checksums.items():
        actual = hashlib.sha256((package / relative).read_bytes()).hexdigest()
        assert actual == digest, relative


def test_manifest_table_digest_matches_the_rows_file(seeded, tmp_path):
    report = _export(seeded["settings"], tmp_path / "out", [seeded["exported"]])
    manifest = _manifest(report.package_dir)
    for table, entry in manifest["tables"].items():
        path = report.package_dir / rows_path(table)
        assert entry["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest(), table
        assert entry["rows"] == len(_rows(report.package_dir, table)), table
        assert entry["rows"] == report.table_counts[table], table


def test_two_exports_of_one_quiescent_database_are_byte_identical(seeded, tmp_path):
    """Row order must come from the primary key, not from the planner: an
    operator comparing two packages, or re-running a failed export, has to see
    the same table digests."""
    first = _export(seeded["settings"], tmp_path / "out", [seeded["exported"]])
    second = _export(seeded["settings"], tmp_path / "out", [seeded["exported"]])

    assert first.package_id != second.package_id
    left, right = _manifest(first.package_dir), _manifest(second.package_dir)
    assert {t: e["sha256"] for t, e in left["tables"].items()} == {
        t: e["sha256"] for t, e in right["tables"].items()
    }
    assert left["tables"].keys() == right["tables"].keys()
    # The only intentionally per-run fields.
    for key in ("package_id", "created_at", "checksums_sha256"):
        left.pop(key), right.pop(key)
    assert left == right


# ------------------------------------------------------------------- scope


@pytest.mark.parametrize(
    ("table", "statement"),
    [
        ("notebooks", "SELECT COUNT(*) FROM notebooks WHERE id=?"),
        ("sources", "SELECT COUNT(*) FROM sources WHERE notebook_id=?"),
        ("chunks", "SELECT COUNT(*) FROM chunks WHERE notebook_id=?"),
        (
            "source_elements",
            "SELECT COUNT(*) FROM source_elements e JOIN sources s "
            "ON s.id=e.source_id WHERE s.notebook_id=?",
        ),
        ("knowhow_tables", "SELECT COUNT(*) FROM knowhow_tables WHERE notebook_id=?"),
        (
            "knowhow_rows",
            "SELECT COUNT(*) FROM knowhow_rows r JOIN knowhow_tables t "
            "ON t.id=r.table_id WHERE t.notebook_id=?",
        ),
        (
            "knowhow_cell_code",
            "SELECT COUNT(*) FROM knowhow_cell_code c JOIN knowhow_rows r "
            "ON r.id=c.row_id JOIN knowhow_tables t ON t.id=r.table_id "
            "WHERE t.notebook_id=?",
        ),
        ("memory_items", "SELECT COUNT(*) FROM memory_items WHERE notebook_id=?"),
        (
            "memory_provenance",
            "SELECT COUNT(*) FROM memory_provenance p JOIN memory_items m "
            "ON m.id=p.memory_id WHERE m.notebook_id=?",
        ),
        ("chunk_embeddings", "SELECT COUNT(*) FROM chunk_embeddings WHERE notebook_id=?"),
        ("notebook_grants", "SELECT COUNT(*) FROM notebook_grants WHERE notebook_id=?"),
    ],
)
def test_table_row_count_matches_the_same_scope_queried_directly(
    seeded, tmp_path, table, statement
):
    report = _export(seeded["settings"], tmp_path / "out", [seeded["exported"]])
    expected = _count(seeded["repo"], statement, (seeded["exported"],))

    assert expected > 0, "the seed must actually produce rows for this table"
    assert report.table_counts[table] == expected
    assert len(_rows(report.package_dir, table)) == expected


def test_the_unexported_notebook_contributes_no_row_anywhere(seeded, tmp_path):
    report = _export(seeded["settings"], tmp_path / "out", [seeded["exported"]])
    other = seeded["other"]

    leaked: list[str] = []
    for table in synced_tables():
        for row in _rows(report.package_dir, table):
            if other in {value for value in row.values() if isinstance(value, str)}:
                leaked.append(f"{table}: {row}")
    assert not leaked, leaked
    assert report.table_counts["notebooks"] == 1


def test_parent_scoped_knowhow_cells_follow_the_two_hop_chain(seeded, tmp_path):
    """knowhow_cells is the deepest PARENT chain in the manifest:
    cells -> rows -> tables -> notebook_id."""
    repo = seeded["repo"]
    report = _export(seeded["settings"], tmp_path / "out", [seeded["exported"]])
    expected = _count(
        repo,
        "SELECT COUNT(*) FROM knowhow_cells c JOIN knowhow_rows r ON r.id=c.row_id "
        "JOIN knowhow_tables t ON t.id=r.table_id WHERE t.notebook_id=?",
        (seeded["exported"],),
    )
    everything = _count(repo, "SELECT COUNT(*) FROM knowhow_cells")

    assert expected > 0, "the seed must actually produce cells to scope"
    assert everything > expected, "the other notebook must have cells of its own"
    assert report.table_counts["knowhow_cells"] == expected


def _seed_kos_and_community_members(repo, notebook_id: str, suffix: str) -> None:
    """One row each in the two tables PR-3a/0064 gives no PostgreSQL primary
    key -- SQLite backs their registered ``TableSyncSpec.key`` with a UNIQUE
    index instead (v84), which ``_Source.sync_key`` reads as their row
    identity. No FK enforces object_id/source_id/community_id, so the values
    only need to be unique per test, not resolvable rows elsewhere."""
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_object_sources(object_id, source_id, "
            "notebook_id) VALUES (?, ?, ?)",
            (f"obj-{suffix}", f"src-{suffix}", notebook_id),
        )
        db.execute(
            "INSERT INTO community_members(canonical_id, notebook_id, level, "
            "community_id, canonical_name, centrality) VALUES (?, ?, ?, ?, ?, ?)",
            (f"can-{suffix}", notebook_id, 0, f"comm-{suffix}", "name", 0.0),
        )


def test_two_no_pg_pk_tables_now_use_the_keyset_path(seeded, tmp_path):
    """knowledge_object_sources and community_members still have no
    PostgreSQL-shaped primary key in the SQLite catalog (v84 gives them a
    UNIQUE index instead -- SQLite cannot add a primary key to an existing
    table in place), but PR-3a's ``TableSyncSpec.key`` fallback makes
    ``_Source.sync_key`` resolve one anyway, so they now page through the
    SAME keyset code path (``_scan``) as every other synced table rather
    than the old one-notebook-at-a-time streaming branch. Scoping and file
    output must still be correct."""
    _seed_kos_and_community_members(seeded["repo"], seeded["exported"], "keep")
    _seed_kos_and_community_members(seeded["repo"], seeded["other"], "drop")
    report = _export(seeded["settings"], tmp_path / "out", [seeded["exported"]])

    for table in ("knowledge_object_sources", "community_members"):
        with seeded["repo"]._connect() as db:
            keyed = [
                row for row in db.execute(f'PRAGMA table_info("{table}")').fetchall()
                if int(row["pk"]) > 0
            ]
        assert not keyed, (
            f"{table} gained a real PostgreSQL-style primary key in the "
            "SQLite catalog -- update this test's framing"
        )
        expected = _count(
            seeded["repo"], f"SELECT COUNT(*) FROM {table} WHERE notebook_id=?",
            (seeded["exported"],),
        )
        assert expected == 1, "seed must produce exactly one row for the exported notebook"
        assert report.table_counts[table] == expected
        rows = _rows(report.package_dir, table)
        assert len(rows) == expected
        assert (report.package_dir / rows_path(table)).is_file()


@pytest.mark.parametrize(
    "table,index_name",
    [
        ("knowledge_object_sources", "uq_knowledge_object_sources_sync_key"),
        ("community_members", "uq_community_members_sync_key"),
    ],
)
def test_sync_key_export_refuses_when_the_backing_unique_index_is_dropped(
    seeded, tmp_path, table, index_name
):
    """Mutation verification for ``_Source.sync_key``'s catalog check: this
    table has no PostgreSQL-shaped primary key, so its row identity comes
    entirely from ``TableSyncSpec.key`` PLUS the live catalog actually
    enforcing that column set is unique (v84's UNIQUE index). Drop the index
    that backs it -- the manifest claim is now unenforced -- and the export
    must refuse by name rather than silently exporting rows keyed by a
    column set the database itself does not guarantee is unique."""
    with seeded["repo"]._write() as db:
        db.execute(f"DROP INDEX {index_name}")

    with pytest.raises(SyncExportError) as excinfo:
        _export(seeded["settings"], tmp_path / "out", [seeded["exported"]])

    assert table in str(excinfo.value)
    assert "unique" in str(excinfo.value).lower()


def test_sync_key_refuses_when_a_registered_key_disagrees_with_the_catalog_pk(
    seeded, monkeypatch
):
    """Mutation verification for ``_Source.sync_key``'s other guard: a table
    that HAS a catalog primary key but whose (hypothetically misregistered)
    ``TableSyncSpec.key`` names different columns must refuse rather than
    silently prefer one over the other -- capture triggers, the exporter and
    the importer all have to agree on ONE row identity, and a manifest that
    disagrees with the live schema is a drift nothing downstream can safely
    guess its way past."""
    from app.migration.sync import manifest as manifest_module

    real_spec_for = manifest_module.spec_for
    real_sources_spec = real_spec_for("sources")
    bogus_sources_spec = dataclasses.replace(
        real_sources_spec, key=("not_a_real_key_column",)
    )
    monkeypatch.setattr(
        export_module,
        "spec_for",
        lambda table: bogus_sources_spec if table == "sources" else real_spec_for(table),
    )

    source = export_module._Source(seeded["settings"], Path(__file__).resolve().parents[2])
    try:
        with source.read() as conn:
            with pytest.raises(SyncExportError) as excinfo:
                source.sync_key(conn, "sources")
    finally:
        source.close()

    assert "sources" in str(excinfo.value)
    assert "disagrees" in str(excinfo.value)


def test_sync_key_refuses_a_registered_key_that_only_reorders_the_catalog_pk(
    seeded, monkeypatch
):
    """Same column SET, different ORDER, and it still has to refuse.

    Order is part of the key: the capture triggers build ``key_json`` by
    naming the columns in their order, so ``{"notebook_id": …,
    "element_id": …}`` and ``{"element_id": …, "notebook_id": …}`` are two
    different identities for one row. A set-only comparison would wave this
    through and the target would then match nothing. The message prints both
    tuples so the reader can see which side moved.
    """
    from app.migration.sync import manifest as manifest_module

    real_spec_for = manifest_module.spec_for
    real_spec = real_spec_for("chunk_elements")
    reordered = dataclasses.replace(
        real_spec, key=("element_id", "notebook_id", "chunk_id")
    )
    monkeypatch.setattr(
        export_module,
        "spec_for",
        lambda table: reordered if table == "chunk_elements" else real_spec_for(table),
    )

    source = export_module._Source(seeded["settings"], Path(__file__).resolve().parents[2])
    try:
        with source.read() as conn:
            catalog = source._catalog_primary_key(conn, "chunk_elements")
            with pytest.raises(SyncExportError) as excinfo:
                source.sync_key(conn, "chunk_elements")
    finally:
        source.close()

    # The mutation really is a pure reorder, not a different column set.
    assert set(catalog) == set(reordered.key) and tuple(catalog) != reordered.key
    message = str(excinfo.value)
    assert "chunk_elements" in message and "disagrees" in message
    assert str(tuple(catalog)) in message
    assert str(reordered.key) in message


@pytest.mark.parametrize(
    "ddl,columns,why",
    [
        (
            "CREATE UNIQUE INDEX uq_probe_partial ON chunk_questions "
            "(notebook_id, chunk_id) WHERE source_id != ''",
            ("notebook_id", "chunk_id"),
            "partial: uniqueness only holds for rows matching the predicate",
        ),
        (
            "CREATE UNIQUE INDEX uq_probe_expression ON chunk_questions "
            "(notebook_id, lower(chunk_id))",
            ("notebook_id", "chunk_id"),
            "expression key: lower(chunk_id) being unique is not chunk_id "
            "being unique",
        ),
    ],
)
def test_has_unique_surface_rejects_an_index_that_does_not_enforce_the_set(
    seeded, ddl, columns, why
):
    """``_has_unique_surface`` must answer for the column set the caller is
    about to match rows by, not for anything the catalog merely calls
    UNIQUE. Both indexes below exist, are flagged unique, and mention exactly
    the wanted columns -- and neither guarantees those columns identify one
    row."""
    with seeded["repo"]._write() as db:
        db.execute(ddl)

    source = export_module._Source(seeded["settings"], Path(__file__).resolve().parents[2])
    try:
        with source.read() as conn:
            assert source._has_unique_surface(conn, "chunk_questions", columns) is False
    finally:
        source.close()


def test_has_unique_surface_accepts_a_plain_unique_index(seeded):
    """The positive control for the two rejections above: the same columns,
    a plain total unique index, and it is accepted -- so those tests fail for
    the reason they claim and not because the probe answers False for
    everything."""
    with seeded["repo"]._write() as db:
        db.execute(
            "CREATE UNIQUE INDEX uq_probe_plain ON chunk_questions "
            "(notebook_id, chunk_id)"
        )

    source = export_module._Source(seeded["settings"], Path(__file__).resolve().parents[2])
    try:
        with source.read() as conn:
            assert (
                source._has_unique_surface(
                    conn, "chunk_questions", ("notebook_id", "chunk_id")
                )
                is True
            )
    finally:
        source.close()


def test_current_snapshot_is_none_on_sqlite(seeded):
    """SQLite has no transaction snapshot to record, and ``None`` is the
    honest answer rather than a placeholder: one writer at a time means
    ``sync_change_log.seq`` order IS commit order, so the incremental window
    has no in-flight gap to compensate for (§7)."""
    source = export_module._Source(seeded["settings"], Path(__file__).resolve().parents[2])
    try:
        with source.read() as conn:
            assert source.current_snapshot(conn) is None
    finally:
        source.close()


@pytest.mark.parametrize(
    "snapshot,expected",
    [
        ("100:100:", 100),
        ("12:34:", 12),
        ("12:34:12,20,33", 12),
        ("0:0:", 0),
    ],
)
def test_snapshot_xmin_reads_the_first_field(snapshot, expected):
    """The stored ``pg_snapshot`` text is ``xmin:xmax:xip1,xip2,...`` and the
    compensation window's lower bound is its first field -- with or without an
    in-progress list."""
    assert export_module._Source.snapshot_xmin(snapshot) == expected


@pytest.mark.parametrize(
    "snapshot",
    [
        "",
        "100",
        "100:200",
        "100:200:300:400",
        "abc:200:",
        "100:xyz:",
        "100:200:300,bad",
        "100:200: 300",
    ],
)
def test_snapshot_xmin_refuses_anything_that_is_not_a_snapshot(snapshot):
    """A watermark row whose snapshot text is corrupt must stop the export,
    not silently yield a bound that would skip changes: every field is
    checked, not just the one that gets returned."""
    with pytest.raises(SyncExportError):
        export_module._Source.snapshot_xmin(snapshot)


def test_ordinal_is_never_exported(seeded, tmp_path):
    report = _export(seeded["settings"], tmp_path / "out", [seeded["exported"]])
    manifest = _manifest(report.package_dir)

    for table, entry in manifest["tables"].items():
        assert "ordinal" not in entry["columns"], table
    for table in synced_tables():
        for row in _rows(report.package_dir, table):
            assert "ordinal" not in row, table


def test_blob_columns_travel_as_the_bytes_object(seeded, tmp_path):
    report = _export(seeded["settings"], tmp_path / "out", [seeded["exported"]])
    rows = _rows(report.package_dir, "chunk_embeddings")

    assert rows, "seeded notebook must carry one chunk embedding"
    for row in rows:
        assert set(row["vector"]) == {BYTES_KEY}
        assert decode_value(row["vector"]) == VECTOR


def test_timestamp_columns_are_utc_but_text_columns_are_untouched(seeded, tmp_path):
    """The two halves of the column-not-value rule, on one row: a notebook
    whose NAME is an offset-carrying instant keeps that name verbatim, while
    its created_at is folded to +00:00."""
    repo = seeded["repo"]
    literal = "2026-01-01T08:00:00+08:00"
    notebook = repo.create_notebook(NotebookCreate(name=literal))

    report = _export(seeded["settings"], tmp_path / "out", [notebook.id])
    row = _rows(report.package_dir, "notebooks")[0]

    assert row["name"] == literal
    assert row["created_at"].endswith("+00:00")
    assert datetime.fromisoformat(row["created_at"]).utcoffset() == timedelta(0)
    with repo._connect() as db:
        stored = db.execute(
            "SELECT created_at FROM notebooks WHERE id=?", (notebook.id,)
        ).fetchone()[0]
    assert datetime.fromisoformat(stored) == datetime.fromisoformat(row["created_at"])


# -------------------------------------------------------------- GLOBAL scope


def test_global_scope_registry_tracks_the_manifest():
    """export.py's GLOBAL scoping registry and the manifest's GLOBAL set are
    two halves of one decision. A table reclassified to GLOBAL without a
    scoping rule here would otherwise fail only at export time, on the one
    notebook that happened to reference it."""
    from app.migration.sync.manifest import SYNC_MANIFEST, ScopeKind, SyncClass

    manifest_global = {
        spec.name
        for spec in SYNC_MANIFEST
        if spec.sync_class in (SyncClass.SYNCED, SyncClass.SYNCED_WITH_MAPPING)
        and spec.scope is not None
        and spec.scope.kind is ScopeKind.GLOBAL
    }
    assert set(export_module._GLOBAL_SCOPES) == manifest_global


def test_every_global_scope_names_a_key_set_that_exists():
    named = {key_set for _column, key_set in export_module._GLOBAL_SCOPES.values()}
    assert named == set(export_module._GLOBAL_KEY_QUERIES), (
        "a GLOBAL table pointing at a key set with no query would silently "
        "export zero rows"
    )


def test_groups_travel_by_the_authorization_edge_only(seeded, tmp_path):
    """groups/group_members are GLOBAL: a group belongs to no single notebook,
    so which ones travel is decided by the group grants of the notebooks being
    exported -- and a group granted only elsewhere must stay behind."""
    report = _export(seeded["settings"], tmp_path / "out", [seeded["exported"]])
    package = report.package_dir

    assert {row["id"] for row in _rows(package, "groups")} == {"grp-exported"}
    assert {row["group_id"] for row in _rows(package, "group_members")} == {
        "grp-exported"
    }


def _reference_object_type(repo, notebook_id: str, object_type: str) -> None:
    with repo._write() as db:
        db.execute(
            "INSERT INTO notebook_object_schemas"
            "(notebook_id, object_type, created_by, created_at, updated_at) "
            "VALUES(?,?,?,?,?)",
            (notebook_id, object_type, "user-local", MOMENT, MOMENT),
        )


def test_object_schemas_travels_by_object_type_reference_not_by_notebook(
    seeded, tmp_path
):
    """object_schemas is GLOBAL: every row's own notebook_id has been '' since
    v47, so a notebook-scoped filter would export nothing at all."""
    repo = seeded["repo"]
    _reference_object_type(repo, seeded["exported"], "concept")
    _reference_object_type(repo, seeded["other"], "formula")

    report = _export(seeded["settings"], tmp_path / "out", [seeded["exported"]])
    exported_types = {
        row["object_type"] for row in _rows(report.package_dir, "object_schemas")
    }

    assert "concept" in exported_types
    assert "formula" not in exported_types
    with repo._connect() as db:
        every = {
            str(row[0])
            for row in db.execute("SELECT object_type FROM object_schemas").fetchall()
        }
    assert exported_types < every, "a GLOBAL table must not export its whole self"


def test_no_referenced_object_type_exports_no_object_schema(seeded, tmp_path):
    report = _export(seeded["settings"], tmp_path / "out", [seeded["exported"]])

    assert report.table_counts["object_schemas"] == 0


# ------------------------------------------------------------ users, files


def test_users_is_closed_over_every_identity_column_in_the_package(seeded, tmp_path):
    """The import maps identities by username, so any user id a package's rows
    reference and users.jsonl omits is an unresolvable row at the target."""
    report = _export(seeded["settings"], tmp_path / "out", [seeded["exported"]])
    package = report.package_dir
    projected = {
        json.loads(line)["id"]
        for line in (package / USERS_NAME).read_text(encoding="utf-8").splitlines()
        if line
    }

    referenced: set[str] = set()
    for table in synced_tables():
        spec = spec_for(table)
        user_columns = [
            c.name for c in spec.mapped_columns if c.kind is MappingKind.USER
        ]
        principal_columns = [
            c.name for c in spec.mapped_columns if c.kind is MappingKind.PRINCIPAL
        ]
        for row in _rows(package, table):
            for name in user_columns:
                if row.get(name):
                    referenced.add(row[name])
            if principal_columns and row.get("principal_type") == "user":
                for name in principal_columns:
                    if row.get(name):
                        referenced.add(row[name])

    assert referenced, "the seed must reference at least one user"
    assert referenced <= projected, sorted(referenced - projected)
    assert "exported-coder" in referenced, "knowhow_cell_code.updated_by must be seen"
    assert "alice" in referenced, "notebook_grants principal_type=user must be seen"
    assert _manifest(package)["users"] == len(projected)


def test_users_rows_carry_exactly_the_projection(seeded, tmp_path):
    report = _export(seeded["settings"], tmp_path / "out", [seeded["exported"]])
    users = [
        json.loads(line)
        for line in (report.package_dir / USERS_NAME).read_text(
            encoding="utf-8"
        ).splitlines()
        if line
    ]

    assert users
    assert all(set(user) == {"id", "username", "display_name", "role"} for user in users)


def test_notebook_files_are_copied_into_the_package(seeded, tmp_path):
    report = _export(seeded["settings"], tmp_path / "out", [seeded["exported"]])
    package = report.package_dir
    origin = Path(seeded["settings"].storage_dir) / "notebooks" / seeded["exported"]
    copied = package / "files" / "notebooks" / seeded["exported"]

    assert origin.is_dir(), "seed upload wrote no notebook file directory"
    expected = sorted(
        path.relative_to(origin).as_posix() for path in origin.rglob("*") if path.is_file()
    )
    actual = sorted(
        path.relative_to(copied).as_posix() for path in copied.rglob("*") if path.is_file()
    )
    assert actual == expected and expected
    assert report.file_count == len(expected)
    assert _manifest(package)["files"] == len(expected)
    for relative in expected:
        assert (copied / relative).read_bytes() == (origin / relative).read_bytes()


def test_notebook_attachment_bodies_are_copied_too(seeded, tmp_path):
    """notebook_assets rows carry attachment METADATA; the bytes live under
    storage/assets/<id>/ and must ride along, or every mirrored attachment is
    a dangling row at the target."""
    storage = Path(seeded["settings"].storage_dir)
    asset_dir = storage / "assets" / seeded["exported"]
    asset_dir.mkdir(parents=True)
    (asset_dir / "picture.png").write_bytes(b"\x89PNG-body")
    other_assets = storage / "assets" / seeded["other"]
    other_assets.mkdir(parents=True)
    (other_assets / "left-behind.png").write_bytes(b"nope")

    report = _export(seeded["settings"], tmp_path / "out", [seeded["exported"]])
    copied = report.package_dir / "files" / "assets" / seeded["exported"]

    assert (copied / "picture.png").read_bytes() == b"\x89PNG-body"
    assert not (report.package_dir / "files" / "assets" / seeded["other"]).exists()
    assert report.file_count == 2
    assert _manifest(report.package_dir)["files"] == 2
    checksums = json.loads(
        (report.package_dir / CHECKSUMS_NAME).read_text(encoding="utf-8")
    )
    relative = f"files/assets/{seeded['exported']}/picture.png"
    assert checksums[relative] == hashlib.sha256(b"\x89PNG-body").hexdigest()


def test_an_asset_directory_is_optional(seeded, tmp_path):
    report = _export(seeded["settings"], tmp_path / "out", [seeded["exported"]])

    assert not (report.package_dir / "files" / "assets").exists()
    assert report.file_count == 1


def test_a_notebook_without_files_is_not_an_error(repo, settings, tmp_path):
    notebook = repo.create_notebook(NotebookCreate(name="empty"))
    report = _export(settings, tmp_path / "out", [notebook.id])

    assert report.notebooks == (notebook.id,)
    assert report.file_count == 0
    assert report.missing_files == ()
    assert not (report.package_dir / "files").exists()


def test_a_file_that_vanishes_mid_copy_is_reported_not_raised(
    seeded, tmp_path, monkeypatch
):
    origin_root = Path(seeded["settings"].storage_dir) / "notebooks"
    real_open = Path.open

    def vanishing(self, *args, **kwargs):
        if origin_root in self.parents:
            raise FileNotFoundError(self)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", vanishing)
    report = _export(seeded["settings"], tmp_path / "out", [seeded["exported"]])

    assert report.file_count == 0
    assert len(report.missing_files) == 1
    assert report.missing_files[0].startswith(f"files/notebooks/{seeded['exported']}/")
    # ... and the package is still complete and self-consistent.
    checksums = json.loads(
        (report.package_dir / CHECKSUMS_NAME).read_text(encoding="utf-8")
    )
    assert report.missing_files[0] not in checksums
    assert (report.package_dir / MANIFEST_NAME).is_file()


# -------------------------------------------------------- selection, state


def test_a_mirror_notebook_is_skipped_and_reported(seeded, tmp_path):
    repo = seeded["repo"]
    # No user-facing route sets this; a mirror is stamped by the importer, so
    # the test reaches the store the same way the other sync_origin guards in
    # this suite do.
    repo._runtime.sharing_store.set_notebook_sync_origin(seeded["other"], SOURCE_ENV)

    report = _export(seeded["settings"], tmp_path / "out", None)

    assert seeded["other"] in report.skipped
    assert "mirror" in report.skipped[seeded["other"]]
    assert seeded["other"] not in report.notebooks
    assert seeded["exported"] in report.notebooks


@pytest.mark.parametrize("status", ["copying", "deleting"])
def test_a_notebook_mid_copy_or_mid_delete_is_skipped(seeded, tmp_path, status):
    """Its rows are half-written or half-removed; no snapshot of them is a
    coherent notebook, so it must be reported, not exported."""
    repo = seeded["repo"]
    with repo._write() as db:
        db.execute(
            "UPDATE notebooks SET status=? WHERE id=?", (status, seeded["other"])
        )

    report = _export(seeded["settings"], tmp_path / "out", None)

    assert report.skipped[seeded["other"]] == f"status={status!r}"
    assert seeded["other"] not in report.notebooks
    assert report.table_counts["notebooks"] == 1


def test_an_unknown_notebook_id_is_reported_not_raised(repo, settings, tmp_path):
    report = _export(settings, tmp_path / "out", ["no-such-notebook"])

    assert report.notebooks == ()
    assert report.skipped == {"no-such-notebook": "no such notebook"}


def test_an_empty_selection_is_rejected(repo, settings, tmp_path):
    """None means "everything"; [] is a selection that lost its contents
    upstream and must not quietly become an empty package."""
    with pytest.raises(SyncExportError):
        _export(settings, tmp_path / "out", [])
    with pytest.raises(SyncExportError):
        _export(settings, tmp_path / "out", ())


def test_none_exports_every_live_notebook(seeded, tmp_path):
    report = _export(seeded["settings"], tmp_path / "out", None)

    assert set(report.notebooks) == {seeded["exported"], seeded["other"]}
    assert report.table_counts["notebooks"] == 2


def _aged(path: Path, seconds: float) -> Path:
    moment = time.time() - seconds
    os.utime(path, (moment, moment))
    return path


def _staging(out_dir: Path, tag: str, *, root_age: float, beat_age: float | None) -> Path:
    """A staging directory in a chosen state of liveness. ``beat_age=None``
    means no ``.heartbeat`` at all -- debris from before the marker existed."""
    path = out_dir / f"sync-{SOURCE_ENV}-0-0-{tag}.tmp"
    (path / "rows").mkdir(parents=True)
    (path / "rows" / "chunks.jsonl").write_text("{}\n")
    if beat_age is not None:
        heartbeat = path / export_module._HEARTBEAT_NAME
        heartbeat.touch()
        _aged(heartbeat, beat_age)
    _aged(path, root_age)
    return path


OLD = 3600 + 60
NEW = 5.0


def test_an_abandoned_staging_directory_is_swept_and_reported(seeded, tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    stale = _staging(out_dir, "deadbeef", root_age=OLD, beat_age=OLD)
    foreign = out_dir / "sync-elsewhere-0-0-cafebabe.tmp"
    foreign.mkdir()
    _aged(foreign, OLD)

    report = _export(seeded["settings"], out_dir, [seeded["exported"]])

    assert not stale.exists()
    assert foreign.exists(), "another source environment's debris is not ours to remove"
    assert len(report.warnings) == 1
    assert stale.name in report.warnings[0]


def test_a_long_running_export_is_not_swept_despite_an_old_root_mtime(
    seeded, tmp_path
):
    """The finding this replaced a root-mtime check for: an export writes into
    rows/ and files/, which never updates the staging ROOT's mtime. An export
    running longer than the staleness window therefore looks abandoned by that
    measure, and a second export would delete it mid-run. The heartbeat is
    what distinguishes 'old directory' from 'no longer running'."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    running = _staging(out_dir, "beefcafe", root_age=OLD, beat_age=NEW)

    report = _export(seeded["settings"], out_dir, [seeded["exported"]])

    assert running.is_dir(), "a beating export must survive the sweep"
    assert (running / "rows" / "chunks.jsonl").read_text() == "{}\n"
    assert report.warnings == ()


def test_a_dead_export_is_swept_even_though_its_root_mtime_is_fresh(seeded, tmp_path):
    """The mirror case: a recently-created staging directory whose heartbeat
    stopped an hour ago is debris, however young the root looks."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    dead = _staging(out_dir, "0badf00d", root_age=NEW, beat_age=OLD)

    report = _export(seeded["settings"], out_dir, [seeded["exported"]])

    assert not dead.exists()
    assert len(report.warnings) == 1
    assert dead.name in report.warnings[0]


def test_pre_heartbeat_debris_still_falls_back_to_the_root_mtime(seeded, tmp_path):
    """Staging left by a version older than the marker has no .heartbeat. It
    must still be sweepable, and a fresh one must still be spared."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    legacy_old = _staging(out_dir, "11111111", root_age=OLD, beat_age=None)
    legacy_new = _staging(out_dir, "22222222", root_age=NEW, beat_age=None)

    report = _export(seeded["settings"], out_dir, [seeded["exported"]])

    assert not legacy_old.exists()
    assert legacy_new.is_dir()
    assert len(report.warnings) == 1


def test_a_concurrent_runs_staging_directory_is_left_alone(seeded, tmp_path):
    """Two exports of one source environment into one output directory is a
    thing operators do. A freshly-touched .tmp is another run's working
    directory, not debris, and deleting it would corrupt that run."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    live = out_dir / f"sync-{SOURCE_ENV}-0-0-beefcafe.tmp"
    (live / "rows").mkdir(parents=True)
    (live / "rows" / "chunks.jsonl").write_text("{}\n")

    report = _export(seeded["settings"], out_dir, [seeded["exported"]])

    assert live.is_dir()
    assert (live / "rows" / "chunks.jsonl").read_text() == "{}\n"
    assert report.warnings == ()


def test_a_failed_export_leaves_no_staging_directory(seeded, tmp_path, monkeypatch):
    out_dir = tmp_path / "out"

    def explode(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(export_module, "_write_users", explode)
    with pytest.raises(RuntimeError, match="boom"):
        _export(seeded["settings"], out_dir, [seeded["exported"]])

    assert list(out_dir.iterdir()) == []


def test_export_writes_the_target_watermark(seeded, tmp_path):
    report = _export(seeded["settings"], tmp_path / "out", [seeded["exported"]])

    with seeded["repo"]._connect() as db:
        row = db.execute(
            "SELECT * FROM sync_export_state WHERE target_env=?", (TARGET_ENV,)
        ).fetchone()
    assert row is not None
    assert row["exported_through_seq"] == 0
    assert row["package_id"] == report.package_id
    # The capture gate is off by default on every freshly migrated database
    # (app.migration.sync.capture's own docstring): there is nothing safe to
    # call "captured through" yet, so both the report and the watermark it
    # wrote must read 0, never a stale high-water mark.
    assert report.captured_through_seq == 0


def test_export_watermark_reads_the_change_log_high_water_mark_when_the_gate_is_open(
    seeded, tmp_path
):
    """docs/incremental-sync-design.md §7: with capture ON, the watermark
    this export writes -- and the report it returns -- must be the change
    log's MAX(seq) as of this export's own read snapshot, not 0."""
    with seeded["repo"]._write() as db:
        db.execute(
            "INSERT INTO sync_capture_control (singleton, enabled, enabled_at) "
            "VALUES (1, 1, ?)",
            (MOMENT,),
        )
        for _ in range(4):
            db.execute(
                "INSERT INTO sync_change_log "
                "(table_name, key_json, operation, changed_at) "
                "VALUES ('notebooks', '{}', 'upsert', ?)",
                (MOMENT,),
            )

    report = _export(seeded["settings"], tmp_path / "out", [seeded["exported"]])

    assert report.captured_through_seq == 4
    with seeded["repo"]._connect() as db:
        row = db.execute(
            "SELECT exported_through_seq FROM sync_export_state WHERE target_env=?",
            (TARGET_ENV,),
        ).fetchone()
    assert row["exported_through_seq"] == 4


def test_export_watermark_ignores_change_log_rows_written_after_the_snapshot(
    seeded, tmp_path, monkeypatch
):
    """A row inserted into the log after this export's read() snapshot was
    taken must not bump the watermark it writes -- the whole point of
    reading MAX(seq) inside the same snapshot as the row scan (see
    _captured_through_seq's docstring)."""
    with seeded["repo"]._write() as db:
        db.execute(
            "INSERT INTO sync_capture_control (singleton, enabled, enabled_at) "
            "VALUES (1, 1, ?)",
            (MOMENT,),
        )
        db.execute(
            "INSERT INTO sync_change_log "
            "(table_name, key_json, operation, changed_at) "
            "VALUES ('notebooks', '{}', 'upsert', ?)",
            (MOMENT,),
        )

    real_captured_through_seq = export_module._captured_through_seq

    def fake_captured_through_seq(source, conn):
        # Simulate a concurrent writer landing a new change-log row between
        # this export's snapshot being opened and this read -- the read
        # itself must still only ever see what the snapshot saw.
        with seeded["repo"]._write() as db:
            db.execute(
                "INSERT INTO sync_change_log "
                "(table_name, key_json, operation, changed_at) "
                "VALUES ('notebooks', '{}', 'upsert', ?)",
                (MOMENT,),
            )
        return real_captured_through_seq(source, conn)

    monkeypatch.setattr(
        export_module, "_captured_through_seq", fake_captured_through_seq
    )

    report = _export(seeded["settings"], tmp_path / "out", [seeded["exported"]])

    # The read() snapshot was opened (and every other table already scanned)
    # before the concurrent insert above landed, so REPEATABLE READ / SQLite's
    # WAL snapshot must still report only the one row that existed then.
    assert report.captured_through_seq == 1
    # Evidence the hook really ran (otherwise the assertion above would hold
    # vacuously): the concurrent insert IS in the log after the export.
    with seeded["repo"]._connect() as db:
        log_rows = db.execute("SELECT COUNT(*) FROM sync_change_log").fetchone()[0]
    assert log_rows == 2


def test_a_second_full_export_overwrites_the_watermark(seeded, tmp_path):
    first = _export(seeded["settings"], tmp_path / "out", [seeded["exported"]])
    second = _export(seeded["settings"], tmp_path / "out", [seeded["exported"]])

    assert second.package_id != first.package_id
    assert second.mode == "full"
    with seeded["repo"]._connect() as db:
        rows = db.execute(
            "SELECT package_id FROM sync_export_state WHERE target_env=?", (TARGET_ENV,)
        ).fetchall()
    assert [row["package_id"] for row in rows] == [second.package_id]


def test_keyset_paging_covers_every_row_exactly_once(seeded, tmp_path, monkeypatch):
    """The seeds are far smaller than one page, so the paging loop would never
    run at its real size. Shrink the page instead of seeding thousands of rows:
    what needs proving is that the ``(pk) > (last pk)`` cursor neither skips a
    row at a page boundary nor repeats one."""
    repo = seeded["repo"]
    notebook = seeded["exported"]
    with repo._write() as db:
        source_id = db.execute(
            "SELECT id FROM sources WHERE notebook_id=? LIMIT 1", (notebook,)
        ).fetchone()["id"]
        for index in range(25):
            db.execute(
                "INSERT INTO chunks(id, notebook_id, source_id, text, section_path, "
                "element_ids, created_at) VALUES(?,?,?,?,?,?,?)",
                (f"chunk-page-{index:03d}", notebook, source_id, f"body {index}",
                 "", "[]", MOMENT),
            )

    monkeypatch.setattr(export_module, "_PAGE_ROWS", 4)
    report = _export(seeded["settings"], tmp_path / "out", [notebook])
    ids = [row["id"] for row in _rows(report.package_dir, "chunks")]
    expected = _count(repo, "SELECT COUNT(*) FROM chunks WHERE notebook_id=?", (notebook,))

    assert len(ids) == expected > 4, "the page size must be smaller than the table"
    assert len(set(ids)) == len(ids), "a row was written twice across a page boundary"
    assert ids == sorted(ids), "keyset pages must come out in primary-key order"
    assert report.table_counts["chunks"] == expected


def test_a_page_sized_export_still_matches_a_single_page_one(seeded, tmp_path, monkeypatch):
    """Paging must not change the bytes: the same rows in the same order."""
    whole = _export(seeded["settings"], tmp_path / "whole", [seeded["exported"]])
    monkeypatch.setattr(export_module, "_PAGE_ROWS", 1)
    paged = _export(seeded["settings"], tmp_path / "paged", [seeded["exported"]])

    assert {t: e["sha256"] for t, e in _manifest(whole.package_dir)["tables"].items()} == {
        t: e["sha256"] for t, e in _manifest(paged.package_dir)["tables"].items()
    }


def test_an_export_refuses_to_run_inside_a_request_read_budget(settings, repo):
    """The budgeted wrapper prepends its own statement to every execute and
    re-times each one against a request deadline. On PostgreSQL that also
    steals the first-statement slot SET TRANSACTION needs. An export is an
    offline operation, so this must fail loudly rather than quietly produce a
    package truncated by a serving timeout."""
    import time as _time

    from app.repositories.read_budget import read_budget

    source = export_module._Source(
        settings, Path(__file__).resolve().parents[2]
    )
    try:
        # The deadline is an absolute time.monotonic() value, not a duration.
        with read_budget(_time.monotonic() + 30.0):
            with pytest.raises(SyncExportError, match="read budget"):
                with source.read():
                    pass
        # ... and without one it opens normally.
        with source.read() as conn:
            assert conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        source.close()


def test_a_write_failure_on_the_target_aborts_the_export(seeded, tmp_path, monkeypatch):
    """A full or read-only output volume is not 'this file vanished': the
    package cannot be produced at all, so the run must fail and clean up
    rather than report a missing file and hand over a package that silently
    lacks it."""
    out_dir = tmp_path / "out"
    real_open = Path.open
    attempted: list[str] = []

    def failing(self, mode="r", *args, **kwargs):
        # Only the COPY's target side: inside the staging package, under
        # files/, opened for writing. The source file opens normally.
        if mode == "wb" and "/files/notebooks/" in str(self) and ".tmp/" in str(self):
            attempted.append(str(self))
            raise OSError(28, "No space left on device")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing)
    with pytest.raises(OSError, match="No space left"):
        _export(seeded["settings"], out_dir, [seeded["exported"]])

    assert attempted, "the test never reached the copy's target-side open"
    assert list(out_dir.iterdir()) == [], "a doomed package must not be left behind"


def test_missing_files_travels_in_the_manifest(seeded, tmp_path, monkeypatch):
    origin_root = Path(seeded["settings"].storage_dir) / "notebooks"
    real_open = Path.open

    def vanishing(self, *args, **kwargs):
        if origin_root in self.parents:
            raise FileNotFoundError(self)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", vanishing)
    report = _export(seeded["settings"], tmp_path / "out", [seeded["exported"]])
    manifest = _manifest(report.package_dir)

    assert manifest["missing_files"] == list(report.missing_files)
    assert manifest["missing_files"], "the import side reads incompleteness from here"
    assert manifest["files"] == 0


def test_a_complete_package_reports_no_missing_files(seeded, tmp_path):
    report = _export(seeded["settings"], tmp_path / "out", [seeded["exported"]])

    assert _manifest(report.package_dir)["missing_files"] == []


def test_a_group_reachable_only_through_a_group_admins_edge_still_travels(
    seeded, tmp_path
):
    """`group_admins` is the same group reached over a narrower edge (only its
    role='admin' members -- app.repositories.group_rows.GROUP_PRINCIPAL_TYPES).
    Scoping GLOBAL groups by `principal_type = 'group'` alone would leave a
    notebook shared only with a group's administrators exporting no group row
    at all, and the grant would land at the target pointing at nothing."""
    repo = seeded["repo"]
    moment = "2026-01-01T00:00:00+00:00"
    with repo._write() as db:
        db.execute(
            "INSERT INTO groups(id,name,kind,description,created_by,created_at,"
            "updated_at,owner_id) VALUES(?,?,?,?,?,?,?,?)",
            ("grp-admins-only", "admins-only", "team", "", "user-local", moment,
             moment, "user-local"),
        )
        db.execute(
            "INSERT INTO group_members(group_id,user_id,role,added_at,added_by) "
            "VALUES(?,?,?,?,?)",
            ("grp-admins-only", "user-local", "admin", moment, "user-local"),
        )
        db.execute(
            "INSERT INTO notebook_grants(id,notebook_id,principal_type,"
            "principal_id,role,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
            ("g-admins-only", seeded["exported"], "group_admins",
             "grp-admins-only", "editor", "user-local", moment),
        )

    report = _export(seeded["settings"], tmp_path / "out", [seeded["exported"]])
    package = report.package_dir

    assert "grp-admins-only" in {row["id"] for row in _rows(package, "groups")}
    assert "grp-admins-only" in {
        row["group_id"] for row in _rows(package, "group_members")
    }


def test_a_running_export_keeps_its_heartbeat_and_ships_without_it(
    seeded, tmp_path, monkeypatch
):
    """The sweep's liveness signal is only real if the exporter actually
    maintains it -- and the marker must not survive into the package."""
    beats: list[int] = []
    seen: list[bool] = []
    original_beat = export_module._PackageWriter.beat
    original_users = export_module._write_users

    def spy_beat(self, *, force=False):
        beats.append(1)
        return original_beat(self, force=force)

    def spy_users(source, conn, writer, user_ids):
        # Mid-export: the staging directory must be claimed.
        seen.append((writer.root / export_module._HEARTBEAT_NAME).is_file())
        return original_users(source, conn, writer, user_ids)

    monkeypatch.setattr(export_module._PackageWriter, "beat", spy_beat)
    monkeypatch.setattr(export_module, "_write_users", spy_users)
    report = _export(seeded["settings"], tmp_path / "out", [seeded["exported"]])

    assert seen == [True], "the export never claimed its staging directory"
    assert len(beats) > len(synced_tables()), "the marker is not being refreshed"
    assert not (report.package_dir / export_module._HEARTBEAT_NAME).exists()
    assert export_module._HEARTBEAT_NAME not in json.loads(
        (report.package_dir / CHECKSUMS_NAME).read_text(encoding="utf-8")
    )
    assert not any(
        path.name == export_module._HEARTBEAT_NAME
        for path in report.package_dir.rglob("*")
    )
