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
import shutil
import uuid
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

    assert observed["status"] == "importing"
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
    # ... and the group the target already had keeps ITS membership list.
    # group_members is seeded only alongside a group this import created; a
    # group that predates the import belongs to this environment, and pushing
    # the source's roster into it would hand people access nobody here granted.
    assert _count(repo, "SELECT COUNT(*) FROM group_members") == 0
    assert report.tables["group_members"].skipped == 1
    reasons = [row for row in report.skipped_rows if row.table == "group_members"]
    assert reasons and "already existed at the target" in reasons[0].reason


def test_a_group_the_target_lacks_is_created(source, target, package):
    with target["repo"]._write() as db:
        db.execute("DELETE FROM group_members")
        db.execute("DELETE FROM groups")

    report = _import(target, package)

    assert report.groups_created == ("shared-team",)
    group = _one(target["repo"], "SELECT id, owner_id FROM groups", ())
    assert group["id"] == "grp-a"
    assert group["owner_id"] == ALICE[1]
    # A group this import CREATED is seeded with its source-side roster: the
    # target has no opinion about a group it has never seen.
    member = _one(target["repo"], "SELECT group_id, user_id FROM group_members", ())
    assert member["group_id"] == "grp-a"
    assert member["user_id"] == ALICE[1]


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
        ".sync-old-" in path.name or path.name.endswith(".sync-tmp")
        for path in storage.rglob("*")
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


# --------------------------------------------- seeding only with the parent


def test_a_revoked_membership_is_not_resurrected_by_the_next_package(
    source, target, package, tmp_path
):
    """The case row-level seed_only alone gets wrong. "Do not overwrite an
    existing row" still INSERTS a row the target deleted, so an administrator
    who revoked a share would find it back after the next sync. Authorization
    rows are seeded only alongside a parent THIS import created."""
    _add_user(target["repo"], "user-b-bob", BOB[1])
    first = _import(target, package)
    repo = target["repo"]
    notebook = source["exported"]
    assert first.tables["notebook_members"].inserted == 1
    assert first.tables["notebook_grants"].inserted == 1
    with repo._write() as db:
        db.execute("DELETE FROM notebook_members WHERE notebook_id=?", (notebook,))
        db.execute("DELETE FROM notebook_grants WHERE notebook_id=?", (notebook,))

    second = _import(target, _export(source, tmp_path / "out2"))

    assert _count(repo, "SELECT COUNT(*) FROM notebook_members") == 0
    assert _count(repo, "SELECT COUNT(*) FROM notebook_grants") == 0
    assert second.tables["notebook_members"].inserted == 0
    assert second.tables["notebook_members"].skipped == 1
    assert second.tables["notebook_grants"].skipped == 1
    reasons = {row.reason for row in second.skipped_rows}
    assert any("already existed at the target" in reason for reason in reasons)


def test_the_parent_created_fact_survives_a_crash_between_groups_and_members(
    source, target, package, monkeypatch
):
    """``groups`` and ``group_members`` are separate transactions, so the fact
    "this import created group G" has to outlive the process. It is recorded
    in sync_import_progress in the same transaction as the group row."""
    from app.migration.sync import import_ as module

    with target["repo"]._write() as db:
        db.execute("DELETE FROM group_members")
        db.execute("DELETE FROM groups")
    real = module._apply_table

    def fail_on_group_members(backend, conn, table, context):
        if table == "group_members":
            raise RuntimeError("simulated crash between groups and its members")
        return real(backend, conn, table, context)

    monkeypatch.setattr(module, "_apply_table", fail_on_group_members)
    with pytest.raises(SyncImportError):
        _import(target, package)
    steps = {
        str(row["table_name"])
        for row in _fetch(target["repo"], "SELECT table_name FROM sync_import_progress")
    }
    assert "__created__:groups:grp-a" in steps

    monkeypatch.setattr(module, "_apply_table", real)
    report = _import(target, package)

    # The resumed run skipped `groups` entirely, so the created-set can only
    # have come from the progress row -- and the members still seed.
    assert report.tables["groups"].resumed is True
    assert report.tables["group_members"].inserted == 1
    assert _count(target["repo"], "SELECT COUNT(*) FROM group_members") == 1


# ------------------------------------------------ the import sentinel is its own


def test_the_stale_copy_sweeper_does_not_reap_an_importing_notebook(
    source, target, package, monkeypatch
):
    """`sweep_stale_copies` deletes every status='copying' notebook older than
    the copy timeout, and a mirror's created_at comes from the SOURCE, so it is
    past any timeout the moment it lands. Sharing the deep copy's sentinel
    would let the sweeper delete a notebook mid-import."""
    from app.migration.sync import import_ as module

    repo = target["repo"]
    store = repo._runtime.sharing_store
    reaped: list[str] = []
    real = module._publish_notebooks

    def sweep_then_publish(backend, context):
        reaped.extend(store.sweep_stale_copies())
        return real(backend, context)

    monkeypatch.setattr(module, "_publish_notebooks", sweep_then_publish)
    report = _import(target, package)

    assert reaped == [], "the sweeper must not see an import as a half-copy"
    assert report.error == ""
    assert _count(
        repo, "SELECT COUNT(*) FROM notebooks WHERE id=?", (source["exported"],)
    ) == 1


def test_the_import_sentinel_is_hidden_by_the_live_predicate():
    """The state the importer parks a notebook in must be one the read-side
    visibility predicate hides, or a half-imported notebook is openable."""
    from app.migration.sync.import_ import _IMPORT_IN_FLIGHT_STATUS
    from app.repositories.postgres import access_sql as pg_access_sql
    from app.repositories.sqlite import access_sql as sqlite_access_sql

    for module in (pg_access_sql, sqlite_access_sql):
        assert f"'{_IMPORT_IN_FLIGHT_STATUS}'" in module.NOTEBOOK_LIVE_SQL
    assert _IMPORT_IN_FLIGHT_STATUS != "copying"


# ------------------------------------------------- interrupted file swaps


def _notebook_storage(target, notebook_id: str) -> Path:
    return Path(target["settings"].storage_dir) / "notebooks" / notebook_id


def _retired(destination: Path, package: Path) -> Path:
    """Where the swap parks the directory it displaces, for the package that
    displaced it. The owning package id is part of the name on purpose."""
    return destination.with_name(
        destination.name + ".sync-old-" + _manifest(package)["package_id"]
    )


def test_a_swap_interrupted_after_both_renames_is_reconciled(
    source, target, package, tmp_path
):
    """Killed after the destination was replaced but before the retired copy
    was dropped: the destination already holds new content and the retired
    copy is the only copy of the original, so it is adopted, not overwritten."""
    _import(target, package)
    destination = _notebook_storage(target, source["exported"])
    second = _export(source, tmp_path / "out2")
    retired = _retired(destination, second)
    retired.mkdir()
    (retired / "original.txt").write_bytes(b"pre-import original")

    report = _import(target, second)

    assert report.error == ""
    assert not retired.exists()
    assert destination.is_dir()
    assert any(path.is_file() for path in destination.rglob("*"))
    assert any("adopted the retired copy" in w for w in report.warnings)


def test_a_swap_interrupted_between_the_renames_is_put_back(
    source, target, package, tmp_path
):
    """Killed after the destination was renamed aside but before the staged
    copy took its place: the original is put back and the run proceeds."""
    _import(target, package)
    destination = _notebook_storage(target, source["exported"])
    second = _export(source, tmp_path / "out2")
    retired = _retired(destination, second)
    shutil.rmtree(destination)
    retired.mkdir()
    (retired / "original.txt").write_bytes(b"pre-import original")

    report = _import(target, second)

    assert report.error == ""
    assert not retired.exists()
    assert destination.is_dir()
    assert any("interrupted mid-swap" in w for w in report.warnings)


def test_a_finished_packages_leftover_is_swept_not_adopted(
    source, target, package, tmp_path
):
    """The leak the commit order closes: a run killed between "replace the
    directory" and "drop what it replaced" leaves a retired copy behind. The
    package that owns it is already recorded done, so nothing will come back
    for it -- a later import removes it, and never rolls back into it."""
    first = _import(target, package)
    destination = _notebook_storage(target, source["exported"])
    orphan = destination.with_name(
        destination.name + ".sync-old-" + first.package_id
    )
    orphan.mkdir()
    (orphan / "stale.txt").write_bytes(b"left by a finished package")

    report = _import(target, _export(source, tmp_path / "out2"))

    assert report.error == ""
    assert not orphan.exists()
    assert any(
        f"removed {orphan.name}" in warning for warning in report.warnings
    )
    assert destination.is_dir()


def test_another_packages_unfinished_leftover_is_left_alone(
    source, target, package, tmp_path
):
    """A retired copy whose package has NOT finished may still be the only
    thing that run can roll back into. This import does not know what it holds,
    so it neither adopts nor deletes it -- it reports it."""
    _import(target, package)
    destination = _notebook_storage(target, source["exported"])
    foreign = destination.with_name(destination.name + ".sync-old-other-package")
    foreign.mkdir()
    (foreign / "not-mine.txt").write_bytes(b"another run may still need this")

    report = _import(target, _export(source, tmp_path / "out2"))

    assert report.error == ""
    assert (foreign / "not-mine.txt").read_bytes() == b"another run may still need this"
    assert any(
        f"left {foreign.name} alone" in warning for warning in report.warnings
    )
    # ... and it was not treated as this run's own rollback source: the
    # destination holds the package's files, not the foreign directory's.
    assert not (destination / "not-mine.txt").exists()


def test_a_leftover_staging_directory_is_discarded(
    source, target, package, tmp_path
):
    _import(target, package)
    destination = _notebook_storage(target, source["exported"])
    staged = destination.with_name(destination.name + ".sync-tmp")
    staged.mkdir()
    (staged / "half-copied.txt").write_bytes(b"abandoned")

    report = _import(target, _export(source, tmp_path / "out2"))

    assert report.error == ""
    assert not staged.exists()
    assert not any(
        path.name.startswith(destination.name + ".sync-old-")
        for path in destination.parent.iterdir()
    )


# ------------------------------------------------------- group_admins grants


def test_a_group_admins_grant_maps_through_the_group_mapping(
    source, target, tmp_path
):
    """`group_admins` is the same group reached over a narrower edge (only its
    role='admin' members). It has to scope and map exactly like `group`, or
    every admin-only grant arrives pointing at a source-side id."""
    with source["repo"]._write() as db:
        db.execute(
            "INSERT INTO notebook_grants(id,notebook_id,principal_type,"
            "principal_id,role,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
            ("grant-admins", source["exported"], "group_admins", "grp-a",
             "editor", ALICE[0], MOMENT),
        )

    report = _import(target, _export(source, tmp_path / "out3"))

    assert report.error == ""
    grant = _one(
        target["repo"],
        "SELECT principal_type, principal_id FROM notebook_grants WHERE id=?",
        ("grant-admins",),
    )
    assert grant["principal_type"] == "group_admins"
    assert grant["principal_id"] == "grp-b", (
        "a group_admins principal must be remapped like a group principal"
    )


# ------------------------------------------- the source owns a memory's history


def _add_revision(
    repo, memory_id: str, revision: int, summary: str, actor: str
) -> str:
    """One memory revision, the shape both environments mint when somebody
    confirms or edits a memory. ``(memory_id, revision)`` is UNIQUE, so the
    two sides independently producing "revision 2" is exactly the collision
    under test -- same pair, different id."""
    revision_id = f"rev-{summary}"
    with repo._write() as db:
        db.execute(
            "INSERT INTO memory_revisions(id,memory_id,revision,title,content_md,"
            "tags_json,status,promotion_state,changed_by,change_reason,created_at) "
            "VALUES(?,?,?,?,?,'[]','confirmed','none',?,'',?)",
            (revision_id, memory_id, revision, summary, summary, actor, MOMENT),
        )
    return revision_id


def _memory_id(repo, notebook_id: str) -> str:
    return _one(
        repo, "SELECT id FROM memory_items WHERE notebook_id=?", (notebook_id,)
    )["id"]


def test_the_source_owns_the_revision_history_of_the_memories_it_carries(
    source, target, package, tmp_path
):
    """Both environments edit the same mirrored memory, so each mints its own
    revision 2 under a different id. ``ON CONFLICT (id) DO UPDATE`` alone would
    hit uq_memory_revisions_memory_id_revision and fail the whole import; §5
    says the source wins, so the target's is removed first."""
    _import(target, package)
    memory = _memory_id(source["repo"], source["exported"])
    assert memory == _memory_id(target["repo"], source["exported"])
    _add_revision(source["repo"], memory, 2, "source-edit", ALICE[0])
    _add_revision(target["repo"], memory, 2, "target-edit", ALICE[1])

    report = _import(target, _export(source, tmp_path / "out2"))

    assert report.error == ""
    rows = _fetch(
        target["repo"],
        "SELECT id, revision FROM memory_revisions WHERE memory_id=? "
        "ORDER BY revision",
        (memory,),
    )
    assert [str(row["id"]) for row in rows] == [
        str(row["id"])
        for row in _fetch(
            source["repo"],
            "SELECT id FROM memory_revisions WHERE memory_id=? ORDER BY revision",
            (memory,),
        )
    ]
    assert "rev-target-edit" not in {str(row["id"]) for row in rows}
    assert any("the source no longer has" in warning for warning in report.warnings)


def test_a_memory_the_package_does_not_carry_keeps_its_own_history(
    source, target, package, tmp_path
):
    """The prune is scoped to the parents the package carries. A memory the
    target's own users created lives in the same table and must not be
    touched by it (§5: the target's own memory rows are not in the source's
    change log at all)."""
    _import(target, package)
    repo = target["repo"]
    with repo._write() as db:
        db.execute(
            "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,"
            "title,content_md,tags_json,created_at,updated_at) "
            "VALUES(?,?,?,'external_agent','confirmed','本地记忆','正文','[]',?,?)",
            ("mem-local", source["exported"], ALICE[1], MOMENT, MOMENT),
        )
    _add_revision(repo, "mem-local", 1, "local-only", ALICE[1])

    report = _import(target, _export(source, tmp_path / "out2"))

    assert report.error == ""
    assert _count(
        repo, "SELECT COUNT(*) FROM memory_revisions WHERE memory_id=?", ("mem-local",)
    ) == 1
    assert _count(
        repo, "SELECT COUNT(*) FROM memory_items WHERE id=?", ("mem-local",)
    ) == 1


# --------------------------------------------------- an imported group's invite


def test_an_imported_group_arrives_with_no_invitation_link(
    source, target, tmp_path
):
    """An invitation is a live capability to join. Carrying the source's token
    across would let anyone holding a link minted in the OTHER environment walk
    into this one's group."""
    with source["repo"]._write() as db:
        db.execute(
            "UPDATE groups SET invite_token=?, invite_created_at=?, "
            "invite_created_by=? WHERE id=?",
            ("invite-from-source", MOMENT, ALICE[0], "grp-a"),
        )
    with target["repo"]._write() as db:
        db.execute("DELETE FROM group_members")
        db.execute("DELETE FROM groups")
    # Exported AFTER the token exists, so the package really carries it and
    # the assertions below are about the importer, not about an empty column.
    package = _export(source, tmp_path / "out-invite")
    assert _rows(package, "groups")[0]["invite_token"] == "invite-from-source"

    report = _import(target, package)

    assert report.groups_created == ("shared-team",)
    row = _one(
        target["repo"],
        "SELECT invite_token, invite_created_at, invite_created_by FROM groups "
        "WHERE id=?",
        ("grp-a",),
    )
    assert row["invite_token"] is None
    assert row["invite_created_at"] is None
    assert row["invite_created_by"] is None
    # ... and the source's link really is dead here, through the seam a
    # recipient would actually use.
    store = target["repo"]._runtime.groups
    assert store.join_by_invite("invite-from-source", user_id=ALICE[1]) is None


# ------------------------------------------- created users cannot sign in yet


def test_created_users_are_reported_as_needing_a_recovery_grant(
    source, target, package
):
    """Nothing in this repository binds an external identity to a local account
    by matching usernames, so a user this import mints owns rows but cannot be
    signed in to. The report has to say so -- the CLI prints warnings and puts
    them in --json, so one warning covers both surfaces."""
    report = _import(target, package, create_missing_users=True)

    assert report.user_mapping.created
    grant = [w for w in report.warnings if "recover" in w]
    assert grant, report.warnings
    assert "/admin/auth/grants" in grant[0]
    assert grant[0] in json.dumps(report.as_json(), ensure_ascii=False)


def test_the_replaced_directories_are_dropped_before_the_run_is_recorded_done(
    source, target, package, tmp_path, monkeypatch
):
    """Order matters, not just eventual cleanup. Marking the package done
    first would mean a crash in between leaves a done package -- which the
    next run short-circuits as already_applied -- with retired directories
    nothing will ever come back for."""
    from app.migration.sync import import_ as module

    _import(target, package)
    storage = Path(target["settings"].storage_dir)
    seen: list[tuple[str, list[str]]] = []
    real = module._settle

    def observe(backend, context, status, report):
        seen.append(
            (
                status,
                [path.name for path in storage.rglob("*") if ".sync-old-" in path.name],
            )
        )
        return real(backend, context, status, report)

    monkeypatch.setattr(module, "_settle", observe)
    report = _import(target, _export(source, tmp_path / "out2"))

    assert report.error == ""
    assert seen == [("done", [])], (
        "no retired directory may still exist when the run is recorded done"
    )


# ------------------------------------------------ SQLite's lexical index


def _fts_chunk_ids(repo, notebook_id: str) -> set[str]:
    return {
        str(row["chunk_id"])
        for row in _fetch(
            repo, "SELECT chunk_id FROM chunks_fts WHERE notebook_id=?", (notebook_id,)
        )
    }


def test_imported_chunks_are_reachable_by_lexical_search(
    source, target, package
):
    """``chunks_fts`` and ``kg_objects_fts`` have no triggers -- every writer
    maintains them by hand. An importer that skips that lands a mirror with
    vector recall but permanently invisible to lexical search, and nothing
    would ever notice (the vector side has a self-heal probe, FTS has none)."""
    _import(target, package)
    repo = target["repo"]
    notebook = source["exported"]

    chunks = {
        str(row["id"])
        for row in _fetch(repo, "SELECT id FROM chunks WHERE notebook_id=?", (notebook,))
    }
    assert chunks
    assert _fts_chunk_ids(repo, notebook) == chunks
    # ... and the index really answers a MATCH, not just holds rows.
    hits = _fetch(
        repo,
        "SELECT chunk_id FROM chunks_fts WHERE notebook_id=? AND chunks_fts MATCH ?",
        (notebook, "alpha"),
    )
    assert {str(row["chunk_id"]) for row in hits} <= chunks
    assert hits


def test_a_knowledge_object_reaches_the_object_index(source, target, package):
    _import(target, package)
    notebook = source["exported"]

    # ko-1's payload has no name, so it is correctly absent; give the index
    # something to hold and re-import to prove the projection runs.
    with source["repo"]._write() as db:
        db.execute(
            "UPDATE knowledge_objects SET payload=? WHERE id=?",
            ('{"name": "硅晶圆"}', "ko-1"),
        )
    import tempfile

    _import(target, _export(source, Path(tempfile.mkdtemp()) / "out"))

    rows = _fetch(
        target["repo"],
        "SELECT object_id, name FROM kg_objects_fts WHERE notebook_id=?",
        (notebook,),
    )
    assert [(str(row["object_id"]), str(row["name"])) for row in rows] == [
        ("ko-1", "硅晶圆")
    ]


# ---------------------------------------- a full package is a snapshot


def test_rows_the_source_deleted_are_removed_from_the_mirror(
    source, target, package, tmp_path
):
    """Upsert only ever adds and overwrites, so without a reconciliation pass
    a mirror accumulates every chunk, element and knowhow cell the source ever
    deleted. A FULL package is a snapshot: what it does not carry for the
    notebooks it covers is gone."""
    _import(target, package)
    repo = target["repo"]
    notebook = source["exported"]
    doomed_source = _one(
        source["repo"], "SELECT id FROM sources WHERE notebook_id=?", (notebook,)
    )["id"]
    doomed_row = _one(
        source["repo"],
        "SELECT r.id AS id FROM knowhow_rows r JOIN knowhow_tables t "
        "ON t.id=r.table_id WHERE t.notebook_id=?",
        (notebook,),
    )["id"]
    before = _counts(repo, "chunks", "source_elements", "knowhow_rows", "knowhow_cells")
    assert _fts_chunk_ids(repo, notebook)
    with source["repo"]._write() as db:
        db.execute("DELETE FROM sources WHERE id=?", (doomed_source,))
        db.execute("DELETE FROM knowhow_rows WHERE id=?", (doomed_row,))

    report = _import(target, _export(source, tmp_path / "out2"))

    assert report.error == ""
    after = _counts(repo, "chunks", "source_elements", "knowhow_rows", "knowhow_cells")
    assert after["chunks"] == 0 and before["chunks"] > 0
    assert after["source_elements"] == 0 and before["source_elements"] > 0
    assert after["knowhow_rows"] == 0 and before["knowhow_rows"] > 0
    assert after["knowhow_cells"] == 0 and before["knowhow_cells"] > 0
    assert _count(repo, "SELECT COUNT(*) FROM sources WHERE id=?", (doomed_source,)) == 0
    # The lexical index follows the rows, in the same transaction.
    assert _fts_chunk_ids(repo, notebook) == set()
    assert any("no longer carries" in warning for warning in report.warnings)


def test_the_reconciliation_never_reaches_another_notebook(
    source, target, package, tmp_path
):
    """The sweep is scoped to the notebooks the package covers. A notebook
    this package says nothing about -- the target's own, or a mirror of a
    different slice -- must be untouched by it."""
    _import(target, package)
    repo = target["repo"]
    with repo._write() as db:
        db.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("nb-local", "本地库", "", "Semiconductor", "draft", "user-local",
             MOMENT, MOMENT),
        )
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,created_at,"
            "updated_at) VALUES(?,?,?,?,?,?)",
            ("src-local", "nb-local", "本地材料", "file", MOMENT, MOMENT),
        )

    _import(target, _export(source, tmp_path / "out2"))

    assert _count(repo, "SELECT COUNT(*) FROM sources WHERE id=?", ("src-local",)) == 1
    assert _count(repo, "SELECT COUNT(*) FROM notebooks WHERE id=?", ("nb-local",)) == 1


def test_the_reconciliation_records_a_resumable_step_per_table(
    source, target, package
):
    _import(target, package)

    steps = {
        str(row["table_name"])
        for row in _fetch(target["repo"], "SELECT table_name FROM sync_import_progress")
    }
    assert "__prune__:chunks" in steps
    # ... and the tables the sweep deliberately skips have no step at all.
    assert "__prune__:memory_items" not in steps
    assert "__prune__:notebooks" not in steps
    assert "__prune__:notebook_grants" not in steps
    assert "__prune__:groups" not in steps


# ------------------------------------------ a failure after the claim settles


def test_a_failure_while_creating_users_still_settles_the_run(
    source, target, package, tmp_path
):
    """Creating users writes to the target and can fail. It happens after the
    sync_imports row is claimed, so it has to be inside the protected phase --
    otherwise the row is stranded at 'running' and every later import from the
    same source environment is refused as a concurrent one."""
    with source["repo"]._write() as db:
        db.execute("UPDATE users SET username='' WHERE id=?", (BOB[0],))
    unnamed = _export(source, tmp_path / "out2")

    with pytest.raises(SyncImportError) as failure:
        _import(target, unnamed, create_missing_users=True)

    assert "no username" in str(failure.value)
    row = _one(
        target["repo"], "SELECT status FROM sync_imports WHERE package_id=?",
        (_manifest(unnamed)["package_id"],),
    )
    assert row["status"] == "failed"
    assert [p for p in unnamed.iterdir() if p.name.startswith("import-report-")]
    # ... and the next import from the same source environment is not refused
    # as a concurrent one.
    report = _import(target, package)
    assert report.error == ""
    assert _count(target["repo"], "SELECT COUNT(*) FROM notebooks") == 1


# ------------------------------------- identity is not only the primary key


def _add_question(repo, chunk_id: str, notebook_id: str, question: str) -> str:
    """One chunk question. ``(chunk_id, question)`` is UNIQUE, so replacing a
    question with an equivalent one under a NEW id is the shape that breaks an
    upsert keyed on the primary key alone."""
    question_id = f"q-{uuid.uuid4().hex[:8]}"
    with repo._write() as db:
        db.execute(
            "INSERT INTO chunk_questions(id,notebook_id,source_id,chunk_id,"
            "question,vector,created_at) "
            "SELECT ?,?,source_id,?,?,?,? FROM chunks WHERE id=?",
            (question_id, notebook_id, chunk_id, question, b"", MOMENT, chunk_id),
        )
    return question_id


def test_a_row_reborn_under_a_new_id_with_the_same_business_key_imports(
    source, target, package, tmp_path
):
    """The reconcile phase has to run BEFORE the upsert. chunk_questions is
    unique on (chunk_id, question): a source that deleted a question and
    re-derived the same text gives it a new primary key and the same business
    key, so upserting first inserts a second row and the unique index rejects
    it. Sweeping first removes the superseded row while it is still alone."""
    chunk = _one(
        source["repo"],
        "SELECT id FROM chunks WHERE notebook_id=? ORDER BY id LIMIT 1",
        (source["exported"],),
    )["id"]
    first_id = _add_question(source["repo"], chunk, source["exported"], "为什么?")
    _import(target, _export(source, tmp_path / "out1"))
    assert _count(
        target["repo"], "SELECT COUNT(*) FROM chunk_questions WHERE id=?", (first_id,)
    ) == 1

    with source["repo"]._write() as db:
        db.execute("DELETE FROM chunk_questions WHERE id=?", (first_id,))
    second_id = _add_question(source["repo"], chunk, source["exported"], "为什么?")
    assert second_id != first_id

    report = _import(target, _export(source, tmp_path / "out2"))

    assert report.error == ""
    rows = _fetch(
        target["repo"],
        "SELECT id, question FROM chunk_questions WHERE chunk_id=?",
        (chunk,),
    )
    assert [(str(row["id"]), str(row["question"])) for row in rows] == [
        (second_id, "为什么?")
    ]


def test_the_unique_key_roster_is_read_from_the_target_catalog():
    """Read from the catalog, not from a list in this module -- and partial
    indexes are skipped, because their constraint only binds rows satisfying a
    predicate the importer does not evaluate."""
    from app.migration.sync.import_ import _Backend, _unique_keys

    import tempfile

    root = Path(tempfile.mkdtemp())
    settings = _settings(root)
    repo = SQLiteRepository(settings)
    try:
        backend = _Backend(settings, Path(__file__).resolve().parents[2])
        try:
            with backend.read() as conn:
                assert _unique_keys(backend, conn, "chunk_questions", ("id",)) == (
                    ("chunk_id", "question"),
                )
                assert _unique_keys(backend, conn, "knowhow_cells", ("id",)) == (
                    ("row_id", "column_id"),
                )
                assert _unique_keys(backend, conn, "chunks", ("id",)) == ()
                # Partial: notebooks.share_token and sources.memory_id.
                assert _unique_keys(backend, conn, "notebooks", ("id",)) == ()
                assert _unique_keys(backend, conn, "sources", ("id",)) == ()
        finally:
            backend.close()
    finally:
        repo.close()


def test_an_unresolvable_business_key_collision_is_named_not_raised_as_sql(
    source, target, package, tmp_path
):
    """A collision the sweep is not allowed to resolve -- here forced by
    pointing the target's row at a chunk the sweep protects -- has to come
    back as a sentence naming both rows, not as a driver IntegrityError."""
    from app.migration.sync import import_ as module

    chunk = _one(
        source["repo"],
        "SELECT id FROM chunks WHERE notebook_id=? ORDER BY id LIMIT 1",
        (source["exported"],),
    )["id"]
    _add_question(source["repo"], chunk, source["exported"], "同一个问题")
    _import(target, package)
    second = _export(source, tmp_path / "out2")
    # Stand in for "the sweep may not touch this row" by disabling the sweep
    # for this table only; what is under test is the collision REPORT.
    monkeypatch = pytest.MonkeyPatch()
    real = module._prune_table

    def skip_questions(backend, conn, table, context):
        return 0 if table == "chunk_questions" else real(backend, conn, table, context)

    with target["repo"]._write() as db:
        db.execute(
            "INSERT INTO chunk_questions(id,notebook_id,source_id,chunk_id,"
            "question,vector,created_at) "
            "SELECT ?,?,source_id,?,?,?,? FROM chunks WHERE id=?",
            ("q-target-own", source["exported"], chunk, "同一个问题", b"", MOMENT,
             chunk),
        )
    try:
        monkeypatch.setattr(module, "_prune_table", skip_questions)
        with pytest.raises(SyncImportError) as failure:
            _import(target, second)
    finally:
        monkeypatch.undo()

    message = str(failure.value)
    assert "chunk_questions" in message
    assert "q-target-own" in message
    assert "('chunk_id', 'question')" in message


# ------------------------- a target-owned memory's derived closure survives


def _materialize_local_memory(repo, notebook_id: str) -> dict[str, str]:
    """What ``ingest_memory_source`` leaves behind when a user confirms a
    memory on a mirror: a synthetic source pointing at that memory, plus the
    element/chunk/vector/object closure derived from it. Written as raw rows
    because the service seam needs a model to embed with."""
    ids = {
        "memory": "mem-target-own",
        "source": "src-memory-synthetic",
        "element": "el-memory-synthetic",
        "chunk": "ch-memory-synthetic",
        "object": "ko-memory-synthetic",
    }
    with repo._write() as db:
        db.execute(
            "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,"
            "title,content_md,tags_json,created_at,updated_at) "
            "VALUES(?,?,?,'external_agent','confirmed','本地记忆','正文','[]',?,?)",
            (ids["memory"], notebook_id, ALICE[1], MOMENT, MOMENT),
        )
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,memory_id,"
            "created_at,updated_at) VALUES(?,?,?,'memory',?,?,?)",
            (ids["source"], notebook_id, "本地记忆", ids["memory"], MOMENT, MOMENT),
        )
        db.execute(
            "INSERT INTO source_elements(id,source_id,element_type,"
            "location_label,text,created_at) VALUES(?,?,'paragraph','','正文',?)",
            (ids["element"], ids["source"], MOMENT),
        )
        db.execute(
            "INSERT INTO chunks(id,notebook_id,source_id,text,created_at) "
            "VALUES(?,?,?,?,?)",
            (ids["chunk"], notebook_id, ids["source"], "正文", MOMENT),
        )
        db.execute(
            "INSERT INTO chunks_fts(chunk_id,notebook_id,text) VALUES(?,?,?)",
            (ids["chunk"], notebook_id, "正文"),
        )
        db.execute(
            "INSERT INTO chunk_embeddings(chunk_id,notebook_id,vector,created_at) "
            "VALUES(?,?,?,?)",
            (ids["chunk"], notebook_id, bytes(range(8)), MOMENT),
        )
        db.execute(
            "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,owner,"
            "payload,evidence,source_id,created_at,updated_at,last_reviewed) "
            "VALUES(?,?,'concept','approved','','{}','[]',?,?,?,'')",
            (ids["object"], notebook_id, ids["source"], MOMENT, MOMENT),
        )
        db.execute(
            "INSERT INTO knowledge_object_sources(object_id,source_id,notebook_id) "
            "VALUES(?,?,?)",
            (ids["object"], ids["source"], notebook_id),
        )
    return ids


def test_a_target_owned_memorys_derived_rows_survive_the_reconciliation(
    source, target, package, tmp_path
):
    """Confirming a memory on a mirror materializes a synthetic source with a
    whole closure hanging off it. Those rows are in the package's notebooks
    and are not in the package, so a naive sweep destroys a local memory's
    entire retrievable form on every single sync."""
    _import(target, package)
    repo = target["repo"]
    notebook = source["exported"]
    local = _materialize_local_memory(repo, notebook)
    doomed_source = _one(
        source["repo"],
        "SELECT id FROM sources WHERE notebook_id=? AND source_type<>'memory'",
        (notebook,),
    )["id"]
    with source["repo"]._write() as db:
        db.execute("DELETE FROM sources WHERE id=?", (doomed_source,))

    report = _import(target, _export(source, tmp_path / "out2"))

    assert report.error == ""
    # Everything derived from the target's own memory is still there ...
    for table, column, value in (
        ("sources", "id", local["source"]),
        ("source_elements", "id", local["element"]),
        ("chunks", "id", local["chunk"]),
        ("chunk_embeddings", "chunk_id", local["chunk"]),
        ("knowledge_objects", "id", local["object"]),
        ("knowledge_object_sources", "object_id", local["object"]),
        ("memory_items", "id", local["memory"]),
    ):
        assert _count(
            repo, f"SELECT COUNT(*) FROM {table} WHERE {column}=?", (value,)
        ) == 1, table
    assert local["chunk"] in _fts_chunk_ids(repo, notebook)
    # ... and the ordinary source the SOURCE deleted is still swept.
    assert _count(
        repo, "SELECT COUNT(*) FROM sources WHERE id=?", (doomed_source,)
    ) == 0
    assert _count(
        repo, "SELECT COUNT(*) FROM chunks WHERE source_id=?", (doomed_source,)
    ) == 0
    assert any("target-owned memory source" in w for w in report.warnings)


def test_a_synthetic_source_for_a_MIRRORED_memory_is_not_protected(
    source, target, package, tmp_path
):
    """The protection is "synthetic AND its memory is not one the package
    carries". A synthetic source for a memory the package DOES carry came from
    the package and is reconciled like anything else -- otherwise the sweep
    could never retire one the source deleted."""
    _import(target, package)
    repo = target["repo"]
    notebook = source["exported"]
    mirrored_memory = _memory_id(repo, notebook)
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,memory_id,"
            "created_at,updated_at) VALUES(?,?,?,'memory',?,?,?)",
            ("src-mirrored-memory", notebook, "镜像记忆", mirrored_memory,
             MOMENT, MOMENT),
        )

    _import(target, _export(source, tmp_path / "out2"))

    assert _count(
        repo, "SELECT COUNT(*) FROM sources WHERE id=?", ("src-mirrored-memory",)
    ) == 0
