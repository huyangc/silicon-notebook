"""守卫: 全量导入器 (app.migration.sync.import_) 的 SQLite 泳道。对应
docs/incremental-sync-design.md §8 的导入相位与 §4 的身份映射。

这里跑的是真正的两库端到端: 库 A 种数据 → ``export_notebooks`` → 库 B
(另一个临时目录、另一套 storage) ``import_package``。盯的是相位契约本身 ——
预检拒什么、身份映射把哪些列改写成了 B 的 id、镜像围栏有没有落、文件去了哪、
幂等与断点续跑。PostgreSQL 泳道在 tests/postgres/test_sync_import_pg.py, 那边
只盯按后端分叉的一段 (jsonb/timestamptz/bytea 转换与 upsert 语法)。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core.config import Settings
from app.migration.sync.export import export_notebooks
from app.migration.sync.import_ import (
    SyncImportError,
    _UNMAPPED_POLICY,
    import_package,
)
from app.migration.sync.manifest import SYNC_MANIFEST, SyncClass, synced_tables
from app.migration.sync.package import CHECKSUMS_NAME, MANIFEST_NAME, rows_path
from app.models.schemas import NotebookCreate
from app.repositories.ports import UploadedSourceFile
from app.services.sqlite_repository import SQLiteRepository


SOURCE_ENV = "dev"
TARGET_ENV = "prod"

# Same person, different environment: the ids differ, only the username joins.
ALICE = ("user-a-alice", "user-b-alice", "alice")
# In the source only -- the target has to decide what to do about them.
BOB = ("user-a-bob", "bob")
CAROL = ("user-a-carol", "carol")

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


def _add_user(repo, user_id: str, username: str, role: str = "user") -> None:
    with repo._write() as db:
        db.execute(
            "INSERT INTO users(id,email,display_name,role,status,username,"
            "created_at,updated_at) VALUES(?,?,?,?,'active',?,?,?)",
            (
                user_id,
                f"{user_id}@example.invalid",
                username.title(),
                role,
                username,
                MOMENT,
                MOMENT,
            ),
        )


def _upload(repo, notebook_id: str, name: str) -> None:
    repo.upload_sources(
        notebook_id,
        [
            UploadedSourceFile(
                file_name=name,
                content_type="text/plain",
                content=b"alpha beta gamma\n" * 8,
                doc_type="",
                doc_type_explicit=False,
            )
        ],
    )


def _seed_embedding(repo, notebook_id: str) -> bytes:
    """A real BLOB column, so the package's ``$bytes`` round trip is exercised
    end to end rather than only in the exporter's own suite."""
    vector = bytes(range(32))
    with repo._write() as db:
        chunk = db.execute(
            "SELECT id FROM chunks WHERE notebook_id=? ORDER BY id LIMIT 1",
            (notebook_id,),
        ).fetchone()
        assert chunk is not None, "seed upload produced no chunk"
        db.execute(
            "INSERT INTO chunk_embeddings(chunk_id,notebook_id,vector,created_at) "
            "VALUES(?,?,?,?)",
            (chunk["id"], notebook_id, vector, MOMENT),
        )
    return vector


def _filled_knowhow_table(repo, notebook_id: str, title: str, actor: str) -> str:
    table_id = repo.create_knowhow_table(
        notebook_id,
        title,
        "",
        [{"name": "Topic", "role": "anchor"}],
        created_by=actor,
    )
    columns = repo.get_knowhow_table(table_id)["columns"]
    repo.add_knowhow_row(
        table_id,
        {column["id"]: f"{title} value" for column in columns},
        actor=actor,
    )
    return table_id


@pytest.fixture
def source(tmp_path):
    """Library A: one notebook carrying every shape the importer has to map --
    a user column, a group grant, a membership row, an attachment, a mount
    edge onto a notebook that is NOT exported, and a blob."""
    settings = _settings(tmp_path / "a")
    repo = SQLiteRepository(settings)
    try:
        for user_id, username in (
            (ALICE[0], ALICE[2]),
            (BOB[0], BOB[1]),
            (CAROL[0], CAROL[1]),
        ):
            _add_user(repo, user_id, username)

        exported = repo.create_notebook(NotebookCreate(name="mirrored")).id
        base = repo.create_notebook(NotebookCreate(name="base-not-exported")).id
        # Seeded as the notebook's own creator (the service seams check read
        # access), then re-attributed to alice below so the identity mapping
        # has a source id that differs from the target's.
        _upload(repo, exported, "note.txt")
        _filled_knowhow_table(repo, exported, "procedure", "user-local")
        repo.create_memory_candidate(
            exported, "user-local", None, "记忆请求", "记忆标题", "记忆正文", ["t"], "test"
        )
        _seed_embedding(repo, exported)

        asset_dir = Path(settings.storage_dir) / "assets" / exported
        asset_dir.mkdir(parents=True)
        (asset_dir / "picture.png").write_bytes(b"\x89PNG-body")

        with repo._write() as db:
            db.execute(
                "UPDATE notebooks SET created_by=? WHERE id=?", (ALICE[0], exported)
            )
            db.execute(
                "UPDATE sources SET uploaded_by=? WHERE notebook_id=?",
                (ALICE[0], exported),
            )
            db.execute(
                "UPDATE memory_items SET created_by=? WHERE notebook_id=?",
                (ALICE[0], exported),
            )
            db.execute(
                "UPDATE knowhow_tables SET created_by=? WHERE notebook_id=?",
                (ALICE[0], exported),
            )
            db.execute(
                "UPDATE knowhow_changes SET actor=? WHERE table_id IN "
                "(SELECT id FROM knowhow_tables WHERE notebook_id=?)",
                (ALICE[0], exported),
            )
            db.execute(
                "INSERT INTO notebook_members(notebook_id,user_id,role,added_at) "
                "VALUES(?,?,?,?)",
                (exported, BOB[0], "reader", MOMENT),
            )
            db.execute(
                "INSERT INTO notebook_bases(notebook_id,base_notebook_id,created_at,"
                "created_by) VALUES(?,?,?,?)",
                (exported, base, MOMENT, ALICE[0]),
            )
            db.execute(
                "INSERT INTO notebook_assets(id,notebook_id,filename,mime,size,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                ("asset-1", exported, "picture.png", "image/png", 9, ALICE[0], MOMENT),
            )
            db.execute(
                "INSERT INTO groups(id,name,kind,description,created_by,created_at,"
                "updated_at,owner_id) VALUES(?,?,?,?,?,?,?,?)",
                ("grp-a", "shared-team", "team", "", ALICE[0], MOMENT, MOMENT, ALICE[0]),
            )
            db.execute(
                "INSERT INTO group_members(group_id,user_id,role,added_at,added_by) "
                "VALUES(?,?,?,?,?)",
                ("grp-a", ALICE[0], "admin", MOMENT, ALICE[0]),
            )
            db.execute(
                "INSERT INTO notebook_grants(id,notebook_id,principal_type,"
                "principal_id,role,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                ("grant-1", exported, "group", "grp-a", "reader", ALICE[0], MOMENT),
            )
            # The two synced tables the target schema gives NO primary key
            # (design doc §2 registers them, §7 defers adding one). They can
            # only be replaced wholesale, so a second import of the same
            # notebook is exactly where a mistake would double their rows.
            db.execute(
                "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,"
                "owner,payload,evidence,source_id,created_at,updated_at,"
                "last_reviewed) VALUES(?,?,'concept','approved','','{}','[]','',"
                "?,?,'')",
                ("ko-1", exported, MOMENT, MOMENT),
            )
            source_row = db.execute(
                "SELECT id FROM sources WHERE notebook_id=?", (exported,)
            ).fetchone()
            db.execute(
                "INSERT INTO knowledge_object_sources(object_id,source_id,"
                "notebook_id) VALUES(?,?,?)",
                ("ko-1", source_row["id"], exported),
            )
            db.execute(
                "INSERT INTO community_members(canonical_id,notebook_id,level,"
                "community_id,canonical_name,centrality,generation) "
                "VALUES(?,?,0,?,?,0.5,1)",
                ("canon-1", exported, "comm-1", "Canon One"),
            )
        yield {
            "repo": repo,
            "settings": settings,
            "exported": exported,
            "base": base,
        }
    finally:
        repo.close()


@pytest.fixture
def target(tmp_path):
    """Library B: a separate database and storage root that already knows
    alice (under her own id) and already has a group by the same name."""
    settings = _settings(tmp_path / "b")
    repo = SQLiteRepository(settings)
    try:
        _add_user(repo, ALICE[1], ALICE[2])
        with repo._write() as db:
            db.execute(
                "INSERT INTO groups(id,name,kind,description,created_by,created_at,"
                "updated_at,owner_id) VALUES(?,?,?,?,?,?,?,?)",
                ("grp-b", "shared-team", "team", "", "user-local", MOMENT, MOMENT,
                 "user-local"),
            )
        yield {"repo": repo, "settings": settings}
    finally:
        repo.close()


def _export(source, out_dir: Path) -> Path:
    report = export_notebooks(
        source["settings"],
        target_env=TARGET_ENV,
        out_dir=out_dir,
        notebook_ids=[source["exported"]],
        source_env=SOURCE_ENV,
    )
    return report.package_dir


@pytest.fixture
def package(source, tmp_path):
    return _export(source, tmp_path / "out")


# ---------------------------------------------------------------- helpers


def _count(repo, statement: str, params=()) -> int:
    with repo._connect() as db:
        return int(db.execute(statement, params).fetchone()[0])


def _rows(package: Path, table: str) -> list[dict]:
    text = (package / rows_path(table)).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def _one(repo, statement: str, params=()):
    with repo._connect() as db:
        return db.execute(statement, params).fetchone()


def _import(target, package, **kwargs):
    return import_package(target["settings"], package, **kwargs)


# ------------------------------------------------------------ policy table


def test_unmapped_policy_covers_exactly_the_manifest_mapped_columns():
    """The policy table is the design doc's §3.2 restated as data. A mapped
    column with no rule must fail loudly at import time rather than fall back
    to any one default -- the module refuses to load otherwise, and this
    pins the same equality where a reader can see it."""
    declared = {
        (spec.name, column.name)
        for spec in SYNC_MANIFEST
        if spec.sync_class is not SyncClass.LOCAL
        for column in spec.mapped_columns
    }

    assert set(_UNMAPPED_POLICY) == declared


# ------------------------------------------------------------- round trip


def test_every_table_is_accounted_for_row_by_row(source, target, package):
    report = _import(target, package)

    assert report.error == ""
    assert not report.already_applied
    for table in synced_tables():
        outcome = report.tables[table]
        assert outcome.inserted + outcome.updated + outcome.skipped == len(
            _rows(package, table)
        ), table
        assert _count(
            target["repo"], f"SELECT COUNT(*) FROM {table}"
        ) >= outcome.inserted, table


def test_user_columns_are_rewritten_to_the_target_ids(source, target, package):
    _import(target, package)
    repo = target["repo"]

    notebook = _one(repo, "SELECT created_by FROM notebooks WHERE id=?",
                    (source["exported"],))
    assert notebook["created_by"] == ALICE[1]
    assert _count(
        repo, "SELECT COUNT(*) FROM sources WHERE uploaded_by=?", (ALICE[1],)
    ) == 1
    assert _count(
        repo, "SELECT COUNT(*) FROM memory_items WHERE created_by=?", (ALICE[1],)
    ) == 1
    assert _count(
        repo, "SELECT COUNT(*) FROM knowhow_tables WHERE created_by=?", (ALICE[1],)
    ) == 1
    # ... and not one row anywhere still carries a source-side user id.
    leaked: list[str] = []
    for table in synced_tables():
        for row in _rows(package, table):
            for column, value in row.items():
                if isinstance(value, str) and value in {ALICE[0], BOB[0], CAROL[0]}:
                    found = _count(
                        repo,
                        f"SELECT COUNT(*) FROM {table} WHERE {column}=?",
                        (value,),
                    )
                    if found:
                        leaked.append(f"{table}.{column}={value}")
    assert not leaked, leaked


def test_the_imported_notebook_is_stamped_as_a_mirror(source, target, package):
    _import(target, package)

    # The same read the API's mirror fence makes (deps.py ->
    # notebook_access_repository().notebook_sync_origin).
    store = target["repo"]._runtime.sharing_store
    assert store.notebook_sync_origin(source["exported"]) == SOURCE_ENV
    row = _one(
        target["repo"], "SELECT sync_origin, status FROM notebooks WHERE id=?",
        (source["exported"],),
    )
    assert row["sync_origin"] == SOURCE_ENV
    # status is target-owned: the mirror arrives in the target's own live
    # state, not carrying whatever lifecycle marker the source row held.
    assert row["status"] == "draft"


def test_a_notebook_is_hidden_until_the_run_finishes(source, target, package):
    """A half-imported notebook must not be visible. The row commits with the
    in-flight ``copying`` marker NOTEBOOK_LIVE_SQL hides, and only the finish
    phase publishes it -- so the commit that creates it already carries both
    the marker and sync_origin, with no window in between."""
    from app.migration.sync import import_ as module

    observed: dict = {}
    real = module._publish_notebooks

    def observe(backend, context):
        observed.update(
            dict(
                _one(
                    target["repo"],
                    "SELECT status, sync_origin, is_shared, share_token "
                    "FROM notebooks WHERE id=?",
                    (source["exported"],),
                )
            )
        )
        return real(backend, context)

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(module, "_publish_notebooks", observe)
        _import(target, package)
    finally:
        monkeypatch.undo()

    assert observed["status"] == "copying"
    # sync_origin is stamped by the INSERT itself, not only by the later
    # _stamp_mirrors pass -- a crash in between must never leave a row that
    # reads as a local notebook.
    assert observed["sync_origin"] == SOURCE_ENV
    # Link sharing is the target's own decision; the source's token never
    # rides along.
    assert observed["is_shared"] == 0
    assert observed["share_token"] is None
    assert _one(
        target["repo"], "SELECT status FROM notebooks WHERE id=?",
        (source["exported"],),
    )["status"] == "draft"


def test_rowids_are_assigned_by_the_target(source, target, package):
    """``POSTGRES_ROWID_ORDINAL_TABLES``' ordinal is an environment-local
    paging cursor and never travels (§8). SQLite has no such column at all --
    its rowid is the equivalent -- so what this lane can pin is that the
    package carries no ordinal for the target to adopt."""
    for table in synced_tables():
        for row in _rows(package, table):
            assert "ordinal" not in row, table

    _import(target, package)
    with target["repo"]._connect() as db:
        rowids = [
            int(item[0])
            for item in db.execute("SELECT rowid FROM chunks ORDER BY rowid").fetchall()
        ]
    assert rowids == list(range(1, len(rowids) + 1))


def test_notebook_files_and_attachments_land_under_the_target_storage(
    source, target, package
):
    report = _import(target, package)
    storage = Path(target["settings"].storage_dir)

    for root in ("notebooks", "assets"):
        origin = package / "files" / root / source["exported"]
        installed = storage / root / source["exported"]
        assert installed.is_dir(), root
        for path in origin.rglob("*"):
            if not path.is_file():
                continue
            mirrored = installed / path.relative_to(origin)
            assert mirrored.is_file(), mirrored
            assert hashlib.sha256(mirrored.read_bytes()).hexdigest() == (
                hashlib.sha256(path.read_bytes()).hexdigest()
            )
    assert report.files_copied == 2
    assert not any(path.name.endswith(".sync-tmp") for path in storage.rglob("*"))
    assert not any(path.name.endswith(".sync-old") for path in storage.rglob("*"))


def test_source_file_path_is_re_anchored_on_the_target_storage(
    source, target, package
):
    """The package carries the SOURCE host's absolute path. Left alone, every
    mirrored source would point at a directory that does not exist here --
    the same re-anchoring copy_notebook does inside one environment."""
    _import(target, package)
    storage = Path(target["settings"].storage_dir)

    row = _one(
        target["repo"],
        "SELECT file_path FROM sources WHERE notebook_id=?",
        (source["exported"],),
    )
    path = Path(row["file_path"])
    assert path.is_file(), path
    assert str(path).startswith(str(storage / "notebooks" / source["exported"]))
    assert str(path) != _rows(package, "sources")[0]["file_path"]


def test_blob_columns_survive_the_round_trip(source, target, package):
    _import(target, package)

    row = _one(
        target["repo"],
        "SELECT vector FROM chunk_embeddings WHERE notebook_id=?",
        (source["exported"],),
    )
    assert bytes(row["vector"]) == bytes(range(32))


# ------------------------------------------------------------- idempotence


def test_a_second_import_of_the_same_package_is_a_no_op(source, target, package):
    first = _import(target, package)
    before = {
        table: _count(target["repo"], f"SELECT COUNT(*) FROM {table}")
        for table in synced_tables()
    }

    second = _import(target, package)

    assert not first.already_applied
    assert second.already_applied
    assert second.tables == {}
    after = {
        table: _count(target["repo"], f"SELECT COUNT(*) FROM {table}")
        for table in synced_tables()
    }
    assert after == before


def test_dry_run_touches_neither_the_database_nor_the_package(
    source, target, package
):
    listing = sorted(path.name for path in package.iterdir())

    report = _import(target, package, dry_run=True)

    assert report.dry_run
    assert report.tables == {}
    assert _count(target["repo"], "SELECT COUNT(*) FROM notebooks") == 0
    assert _count(target["repo"], "SELECT COUNT(*) FROM sync_imports") == 0
    assert sorted(path.name for path in package.iterdir()) == listing
    # The mapping still resolved, which is the point of a dry run.
    assert report.user_mapping.matched[ALICE[0]] == ALICE[1]
    assert set(report.user_mapping.unmatched) == {BOB[1]}


# -------------------------------------------------------------- identities


def test_an_unmappable_owner_fails_the_import_and_leaves_no_rows(
    source, target, tmp_path
):
    with source["repo"]._write() as db:
        db.execute(
            "UPDATE notebooks SET created_by=? WHERE id=?",
            (CAROL[0], source["exported"]),
        )
    report = export_notebooks(
        source["settings"],
        target_env=TARGET_ENV,
        out_dir=tmp_path / "out2",
        notebook_ids=[source["exported"]],
        source_env=SOURCE_ENV,
    )

    with pytest.raises(SyncImportError) as failure:
        _import(target, report.package_dir)

    assert "notebooks.created_by" in str(failure.value)
    assert _count(target["repo"], "SELECT COUNT(*) FROM notebooks") == 0
    assert _count(target["repo"], "SELECT COUNT(*) FROM sources") == 0
    status = _one(
        target["repo"], "SELECT status FROM sync_imports WHERE package_id=?",
        (report.package_id,),
    )
    assert status["status"] == "failed"


def test_create_missing_users_makes_the_unmatched_ones_local(
    source, target, package
):
    report = _import(target, package, create_missing_users=True)
    repo = target["repo"]

    assert report.user_mapping.unmatched == ()
    created = _one(repo, "SELECT * FROM users WHERE username=?", (BOB[1],))
    assert created is not None
    assert created["id"] != BOB[0], "the target mints its own id"
    assert created["id"].startswith("user-")
    assert created["password_hash"] == "", "an imported user has no credentials"
    assert created["role"] == "user", "an import never grants administrator rights"
    assert created["email"].endswith("@users.silicon-notebook.local")
    member = _one(
        repo, "SELECT user_id FROM notebook_members WHERE notebook_id=?",
        (source["exported"],),
    )
    assert member["user_id"] == created["id"]


def test_an_unmappable_member_row_is_skipped_not_fatal(source, target, package):
    report = _import(target, package)

    assert _count(target["repo"], "SELECT COUNT(*) FROM notebook_members") == 0
    assert report.tables["notebook_members"].skipped == 1
    reasons = [row for row in report.skipped_rows if row.table == "notebook_members"]
    assert reasons and BOB[0] in reasons[0].reason


def test_a_group_matched_by_name_is_reused_not_duplicated(source, target, package):
    report = _import(target, package)
    repo = target["repo"]

    assert _count(repo, "SELECT COUNT(*) FROM groups") == 1
    assert report.groups_created == ()
    grant = _one(repo, "SELECT principal_id FROM notebook_grants WHERE id=?",
                 ("grant-1",))
    assert grant["principal_id"] == "grp-b", (
        "the grant must point at the TARGET's group, not the source's id"
    )
    member = _one(repo, "SELECT group_id, user_id FROM group_members", ())
    assert member["group_id"] == "grp-b"
    assert member["user_id"] == ALICE[1]


def test_a_group_the_target_lacks_is_created(source, target, package):
    with target["repo"]._write() as db:
        db.execute("DELETE FROM group_members")
        db.execute("DELETE FROM groups")

    report = _import(target, package)

    assert report.groups_created == ("shared-team",)
    group = _one(target["repo"], "SELECT id, owner_id FROM groups", ())
    assert group["id"] == "grp-a"
    assert group["owner_id"] == ALICE[1]


# ------------------------------------------------------------ optional refs


def test_a_mount_edge_onto_an_unexported_notebook_is_skipped(
    source, target, package
):
    """notebook_bases.base_notebook_id is the manifest's only optional_ref:
    exporting the mounting notebook without its base is a legitimate slice,
    and the dangling edge must be dropped rather than fail the import."""
    assert len(_rows(package, "notebook_bases")) == 1

    report = _import(target, package)

    assert _count(target["repo"], "SELECT COUNT(*) FROM notebook_bases") == 0
    assert report.tables["notebook_bases"].skipped == 1
    assert _count(
        target["repo"], "SELECT COUNT(*) FROM notebooks WHERE id=?", (source["base"],)
    ) == 0
    # ... and the rest of the notebook still arrived.
    assert report.tables["notebooks"].inserted == 1


# -------------------------------------------------------------- preflight


def test_a_tampered_row_file_is_refused(source, target, package):
    path = package / rows_path("notebooks")
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(SyncImportError) as failure:
        _import(target, package)

    assert "sha256" in str(failure.value)
    assert _count(target["repo"], "SELECT COUNT(*) FROM notebooks") == 0


def test_a_package_with_an_open_checksum_chain_is_refused(source, target, package):
    document = json.loads((package / MANIFEST_NAME).read_text(encoding="utf-8"))
    document.pop("checksums_sha256", None)
    (package / MANIFEST_NAME).write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )

    with pytest.raises(SyncImportError) as failure:
        _import(target, package)

    assert "checksums_sha256" in str(failure.value)


def test_an_incremental_payload_is_refused_by_this_build(source, target, package):
    (package / "deletes.jsonl").write_text(
        json.dumps({"table": "chunks", "key_json": "{}"}) + "\n", encoding="utf-8"
    )
    _reseal(package)

    with pytest.raises(SyncImportError) as failure:
        _import(target, package)

    assert "full packages only" in str(failure.value)


def test_a_notebook_the_target_owns_locally_is_never_overwritten(
    source, target, package
):
    with target["repo"]._write() as db:
        db.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (source["exported"], "local", "", "Semiconductor", "draft", "user-local",
             MOMENT, MOMENT),
        )

    with pytest.raises(SyncImportError) as failure:
        _import(target, package)

    assert "not mirrors of this package" in str(failure.value)
    assert _one(
        target["repo"], "SELECT name FROM notebooks WHERE id=?", (source["exported"],)
    )["name"] == "local"


def test_an_embedding_dimension_mismatch_is_refused(source, target, package):
    document = json.loads((package / MANIFEST_NAME).read_text(encoding="utf-8"))
    document["embed_runtime_dim"] = int(document["embed_runtime_dim"]) + 1
    (package / MANIFEST_NAME).write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )

    with pytest.raises(SyncImportError) as failure:
        _import(target, package)

    assert "EMBED_RUNTIME_DIM" in str(failure.value)


def _reseal(package: Path) -> None:
    """Recompute checksums.json and the manifest digest after a test edits a
    package file on purpose, so the edit is tested for what it IS rather than
    stopped at the checksum gate."""
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
    (package / MANIFEST_NAME).write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


# ------------------------------------------------------------------ resume


def test_a_crashed_import_resumes_from_the_unfinished_table(
    source, target, package, monkeypatch
):
    from app.migration.sync import import_ as module

    real = module._apply_table

    def fail_on_sources(backend, conn, table, context):
        if table == "sources":
            raise RuntimeError("simulated crash mid-import")
        return real(backend, conn, table, context)

    monkeypatch.setattr(module, "_apply_table", fail_on_sources)
    with pytest.raises(SyncImportError):
        _import(target, package)

    progress_before = {
        str(row["table_name"])
        for row in _fetch(target["repo"], "SELECT table_name FROM sync_import_progress")
    }
    assert "notebooks" in progress_before
    assert "sources" not in progress_before

    monkeypatch.setattr(module, "_apply_table", real)
    report = _import(target, package)

    assert report.error == ""
    assert report.tables["notebooks"].resumed is True
    assert report.tables["sources"].resumed is False
    assert report.tables["sources"].inserted == len(_rows(package, "sources"))
    assert _count(target["repo"], "SELECT COUNT(*) FROM notebooks") == 1
    assert _count(target["repo"], "SELECT COUNT(*) FROM sources") >= 1
    status = _one(
        target["repo"], "SELECT status FROM sync_imports WHERE package_id=?",
        (_manifest(package)["package_id"],),
    )
    assert status["status"] == "done"


def _fetch(repo, statement: str, params=()):
    with repo._connect() as db:
        return db.execute(statement, params).fetchall()


def _manifest(package: Path) -> dict:
    return json.loads((package / MANIFEST_NAME).read_text(encoding="utf-8"))


# ------------------------------------------------------------------ report


def test_the_report_lands_beside_the_package(source, target, package):
    report = _import(target, package)

    written = [
        path for path in package.iterdir() if path.name.startswith("import-report-")
    ]
    assert len(written) == 1
    document = json.loads(written[0].read_text(encoding="utf-8"))
    assert document["package_id"] == report.package_id
    assert document["source_env"] == SOURCE_ENV
    assert document["notebooks"] == [source["exported"]]
    assert document["tables"]["notebooks"]["inserted"] == 1
    assert document["user_mapping"]["matched"] >= 1


def test_the_import_row_records_the_run(source, target, package):
    report = _import(target, package)

    row = _one(
        target["repo"], "SELECT * FROM sync_imports WHERE package_id=?",
        (report.package_id,),
    )
    assert row["status"] == "done"
    assert row["source_env"] == SOURCE_ENV
    assert row["finished_at"]
    assert json.loads(row["report_json"])["package_id"] == report.package_id


# ------------------------------------------------- a second package, same library


def _counts(repo, *tables) -> dict[str, int]:
    return {table: _count(repo, f"SELECT COUNT(*) FROM {table}") for table in tables}


def test_a_second_package_refreshes_without_clobbering_the_target(
    source, target, package, tmp_path
):
    """The re-sync case the whole design turns on: a mirror the target has
    been living with for a while gets a newer package. Content must move;
    everything the target owns must survive; and the two tables with no
    primary key must be replaced rather than accumulated."""
    first = _import(target, package)
    repo = target["repo"]
    notebook = source["exported"]
    with repo._write() as db:
        # Link sharing is the target's own decision (§5 target_owned_columns).
        db.execute(
            "UPDATE notebooks SET is_shared=1, share_token='tok-b' WHERE id=?",
            (notebook,),
        )
        # A membership the TARGET granted after the first import. The package
        # knows nothing about it, and seed_only must leave it alone.
        db.execute(
            "INSERT INTO notebook_members(notebook_id,user_id,role,added_at) "
            "VALUES(?,?,?,?)",
            (notebook, ALICE[1], "editor", MOMENT),
        )
        # ... and an authorization edge the target re-graded. Also seed_only.
        db.execute("UPDATE notebook_grants SET role='editor' WHERE id=?", ("grant-1",))
    before = _counts(
        repo, "knowledge_object_sources", "community_members", "notebook_members"
    )
    with source["repo"]._write() as db:
        db.execute("UPDATE notebooks SET name=? WHERE id=?", ("renamed", notebook))

    second = _import(target, _export(source, tmp_path / "out2"))

    assert second.package_id != first.package_id
    row = _one(
        repo,
        "SELECT name, is_shared, share_token, status, sync_origin "
        "FROM notebooks WHERE id=?",
        (notebook,),
    )
    # The DO UPDATE branch really ran: a column that is NOT target-owned moved.
    assert row["name"] == "renamed"
    assert second.tables["notebooks"].updated == 1
    assert second.tables["notebooks"].inserted == 0
    # ... and every target-owned column on the same row did not.
    assert row["is_shared"] == 1
    assert row["share_token"] == "tok-b"
    assert row["status"] == "draft"
    assert row["sync_origin"] == SOURCE_ENV
    # seed_only rows are never rewritten once they exist.
    assert _one(repo, "SELECT role FROM notebook_grants WHERE id=?", ("grant-1",))[
        "role"
    ] == "editor"
    assert _one(
        repo,
        "SELECT role FROM notebook_members WHERE notebook_id=? AND user_id=?",
        (notebook, ALICE[1]),
    )["role"] == "editor"
    # The primary-key-less tables were replaced, not appended to.
    after = _counts(
        repo, "knowledge_object_sources", "community_members", "notebook_members"
    )
    assert after == before
    assert before["knowledge_object_sources"] == 1
    assert before["community_members"] == 1


# ------------------------------------------------------- concurrency and resume


def _open_running_row(repo, package_id: str, *, started_at: str) -> None:
    with repo._write() as db:
        db.execute(
            "INSERT INTO sync_imports(package_id,source_env,from_seq,to_seq,status,"
            "started_at,finished_at,report_json) VALUES(?,?,0,0,'running',?,NULL,'{}')",
            (package_id, SOURCE_ENV, started_at),
        )


def test_a_concurrent_import_of_the_same_source_is_refused(
    source, target, package
):
    _open_running_row(
        target["repo"], "other-package", started_at=_now_text()
    )

    with pytest.raises(SyncImportError) as failure:
        _import(target, package)

    assert "still running" in str(failure.value)
    assert _count(target["repo"], "SELECT COUNT(*) FROM notebooks") == 0


def test_the_same_package_does_not_resume_a_running_row_by_default(
    source, target, package
):
    """A row still marked 'running' for THIS package means either a live
    process or a dead one, and the importer cannot tell. Guessing "dead"
    would interleave two live imports, so it refuses until told."""
    _open_running_row(
        target["repo"], _manifest(package)["package_id"], started_at=_now_text()
    )

    with pytest.raises(SyncImportError) as failure:
        _import(target, package)

    assert "--resume" in str(failure.value)
    assert _count(target["repo"], "SELECT COUNT(*) FROM notebooks") == 0

    report = _import(target, package, resume=True)

    assert report.error == ""
    assert _count(target["repo"], "SELECT COUNT(*) FROM notebooks") == 1
    assert any("resume" in warning for warning in report.warnings)


def test_a_long_dead_running_row_is_taken_over_with_a_warning(
    source, target, package
):
    stale = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
    _open_running_row(target["repo"], "abandoned-package", started_at=stale)

    report = _import(target, package)

    assert report.error == ""
    assert any("abandoned-package" in warning for warning in report.warnings)


def _now_text() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- file phase


def test_the_file_phase_runs_after_the_rows_and_rolls_back(
    source, target, package, tmp_path, monkeypatch
):
    """Files are installed only once the rows they belong to are committed,
    and a failure after that puts the target's previous directory back."""
    from app.migration.sync import import_ as module

    _import(target, package)
    storage = Path(target["settings"].storage_dir)
    installed = sorted(
        (storage / "notebooks" / source["exported"]).rglob("*")
    )
    assert installed, "the first import must have installed a file to displace"
    installed[0].write_bytes(b"target-side edit")

    def explode(backend, context):
        raise RuntimeError("simulated failure after the file swap")

    monkeypatch.setattr(module, "_publish_notebooks", explode)
    with pytest.raises(SyncImportError):
        _import(target, _export(source, tmp_path / "out2"))

    assert installed[0].read_bytes() == b"target-side edit"
    assert not any(
        path.name.endswith((".sync-old", ".sync-tmp")) for path in storage.rglob("*")
    )
    # The rows of the failed run are still there: they are atomic per table
    # and were never what got rolled back.
    assert _count(target["repo"], "SELECT COUNT(*) FROM notebooks") == 1


def test_the_file_phase_records_its_own_progress_step(source, target, package):
    _import(target, package)

    steps = {
        str(row["table_name"])
        for row in _fetch(target["repo"], "SELECT table_name FROM sync_import_progress")
    }
    assert "__files__" in steps
    assert "notebooks" in steps


def test_verify_files_rehashes_the_installed_copy(source, target, package):
    report = _import(target, package, verify_files=True)

    assert report.error == ""
    assert report.files_copied == 2


def test_a_sqlite_target_is_told_to_quiesce(source, target, package):
    report = _import(target, package)

    assert any("single writer" in warning for warning in report.warnings)
