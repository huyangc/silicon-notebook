"""Pins for ``tests/world_templates.py`` (seeded world templates).

Every rule the helper enforces is driven here through a deliberately broken
world. Most worlds are a raw SQLite file (milliseconds); the cases whose point
is the real repository — reopen normalisation, uploaded-source paths, copy
independence — build through ``SQLiteRepository``.
"""
from __future__ import annotations

import importlib
import os
import sqlite3
from pathlib import Path

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.ports import UploadedSourceFile
from app.services.sqlite_repository import SQLiteRepository
from tests import world_templates as wt
from tests.world_templates import (
    IDENTITY_RESET,
    PINNED,
    PROCESS_CACHE_INVENTORY,
    WorldTemplateError,
    world_digest,
    world_env,
    world_template,
)


APP_ROOT = Path(__file__).resolve().parents[1] / "app"


# ------------------------------------------------------------------ worlds


def _raw_world(root: Path, rows: tuple[str, ...] = ("alpha", "beta")) -> sqlite3.Connection:
    connection = sqlite3.connect(str(root / wt.WORLD_DB_NAME))
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE IF NOT EXISTS items (name TEXT)")
    connection.executemany("INSERT INTO items VALUES (?)", [(row,) for row in rows])
    connection.commit()
    return connection


def _build_raw(root: Path):
    _raw_world(root).close()
    return {"names": ("alpha", "beta")}


def _reopen_nothing(root: Path):
    return None


def _repo(root: Path) -> SQLiteRepository:
    return SQLiteRepository(
        Settings(
            database_url=f"sqlite:///{root / wt.WORLD_DB_NAME}",
            storage_dir=str(root / wt.WORLD_STORAGE_NAME),
        )
    )


def _build_notebook(root: Path):
    repo = SQLiteRepository(Settings())
    try:
        notebook = repo.create_notebook(NotebookCreate(name="Template"))
        return {"notebook_id": notebook.id}
    finally:
        repo.close()


def _reopen_repository(root: Path):
    return SQLiteRepository(Settings())


def _notebook_ids(root: Path) -> list[str]:
    connection = sqlite3.connect(str(root / wt.WORLD_DB_NAME))
    try:
        return sorted(row[0] for row in connection.execute("SELECT id FROM notebooks"))
    finally:
        connection.close()


# ------------------------------------------------------- build and copies


def test_world_is_built_once_per_key_and_every_copy_is_independent(
    tmp_path_factory, tmp_path
):
    builds: list[Path] = []

    def build(root: Path):
        builds.append(root)
        return _build_notebook(root)

    template = world_template(
        tmp_path_factory, "independent-copies", build, _reopen_repository
    )
    assert world_template(
        tmp_path_factory, "independent-copies", build, _reopen_repository
    ) is template
    assert len(builds) == 1

    first, payload = template.materialize(tmp_path, "first")
    assert first == tmp_path / "first"
    assert world_digest(first) == template.digest
    assert _notebook_ids(first) == [payload["notebook_id"]]

    repo = _repo(first)
    try:
        repo.create_notebook(NotebookCreate(name="Only in the first copy"))
    finally:
        repo.close()
    payload["notebook_id"] = "mutated by the first test"
    assert world_digest(first) != template.digest

    second, second_payload = template.materialize(tmp_path, "second")
    assert world_digest(second) == template.digest
    assert _notebook_ids(second) == [second_payload["notebook_id"]]
    assert second_payload == {"notebook_id": _notebook_ids(second)[0]}
    assert len(builds) == 1


def test_failed_build_is_not_cached(tmp_path_factory):
    attempts: list[int] = []

    def build(root: Path):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("seed failed")
        return _build_raw(root)

    with pytest.raises(RuntimeError, match="seed failed"):
        world_template(tmp_path_factory, "failed-build", build, _reopen_nothing)
    template = world_template(tmp_path_factory, "failed-build", build, _reopen_nothing)
    assert len(attempts) == 2
    assert template.payload == {"names": ("alpha", "beta")}


# ------------------------------------------------------------- settings


def test_build_starts_from_the_process_environment_not_the_requesting_test(
    tmp_path_factory, monkeypatch
):
    seen: dict[str, str | None] = {}

    def build(root: Path):
        for name in ("EMBED_DIM", "WORLD_TEMPLATE_PROBE", "DATABASE_URL"):
            seen[name] = os.environ.get(name)
        return _build_raw(root)

    monkeypatch.setenv("EMBED_DIM", "999")
    monkeypatch.setenv("WORLD_TEMPLATE_PROBE", "leaked from the first test")
    template = world_template(tmp_path_factory, "baseline-env", build, _reopen_nothing)

    assert seen["EMBED_DIM"] == wt._BASELINE_ENV.get("EMBED_DIM")
    assert seen["WORLD_TEMPLATE_PROBE"] is None
    assert seen["DATABASE_URL"] == world_env(template.pristine)["DATABASE_URL"]
    assert os.environ["EMBED_DIM"] == "999"  # the test's own env is restored

    world_template(
        tmp_path_factory, "explicit-env", build, _reopen_nothing, env={"EMBED_DIM": "8"}
    )
    assert seen["EMBED_DIM"] == "8"


def test_key_reused_with_different_settings_is_rejected(tmp_path_factory):
    builds: list[str | None] = []

    def build(root: Path):
        builds.append(os.environ.get("EMBED_DIM"))
        return _build_raw(root)

    world_template(
        tmp_path_factory, "folded-settings", build, _reopen_nothing, env={"EMBED_DIM": "8"}
    )
    with pytest.raises(WorldTemplateError, match="already built"):
        world_template(
            tmp_path_factory,
            "folded-settings",
            build,
            _reopen_nothing,
            env={"EMBED_DIM": "16"},
        )
    world_template(
        tmp_path_factory,
        "folded-settings-16",
        build,
        _reopen_nothing,
        env={"EMBED_DIM": "16"},
    )
    assert builds == ["8", "16"]


def _closure_builder(rows: tuple[str, ...]):
    def build(root: Path):
        _raw_world(root, rows=rows).close()
        return {}

    return build


class _BoundBuilder:
    def __init__(self, rows: tuple[str, ...]) -> None:
        self.rows = rows

    def build(self, root: Path):
        _raw_world(root, rows=self.rows).close()
        return {}


@pytest.mark.parametrize(
    ("same_callable", "equal_callable", "other_callable"),
    [
        pytest.param(
            _closure_builder(("alpha",)),
            _closure_builder(("alpha",)),
            _closure_builder(("beta",)),
            id="factory-closures",
        ),
        pytest.param(
            _BoundBuilder(("alpha",)).build,
            None,
            _BoundBuilder(("alpha",)).build,
            id="bound-methods",
        ),
    ],
)
def test_key_reuse_compares_callbacks_not_just_their_names(
    tmp_path_factory, same_callable, equal_callable, other_callable, request
):
    """Every closure of one factory (and every bound method of one class)
    has the same qualified name; a name-only check would hand the second
    caller the first caller's world."""
    key = f"callback-identity-{request.node.callspec.id}"
    template = world_template(tmp_path_factory, key, same_callable, _reopen_nothing)
    assert world_template(tmp_path_factory, key, same_callable, _reopen_nothing) is template
    if equal_callable is not None:
        # A fixture that recreates an identical closure per test reuses it.
        assert (
            world_template(tmp_path_factory, key, equal_callable, _reopen_nothing)
            is template
        )
    with pytest.raises(WorldTemplateError, match="different callbacks"):
        world_template(tmp_path_factory, key, other_callable, _reopen_nothing)


def test_build_refuses_a_test_that_already_opened_the_app_repository(
    tmp_path_factory,
):
    from app.api import deps

    repo = deps.repository()
    try:
        with pytest.raises(WorldTemplateError, match="deps.repository"):
            world_template(tmp_path_factory, "cached-deps", _build_raw, _reopen_nothing)
    finally:
        repo.close()
        deps.repository.cache_clear()


# --------------------------------------------------------------- payload


class _Live:
    pass


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"root": Path("/tmp")}, id="path"),
        pytest.param({"repo": _Live()}, id="live-object"),
        pytest.param({1: "notebook"}, id="non-str-key"),
        pytest.param(("ok", {"nested": {"set"}}), id="nested-set"),
    ],
)
def test_payload_must_be_pure_json_data(tmp_path_factory, payload, request):
    def build(root: Path):
        _build_raw(root)
        return payload

    with pytest.raises(WorldTemplateError, match="payload"):
        world_template(
            tmp_path_factory, f"payload-{request.node.callspec.id}", build, _reopen_nothing
        )


# -------------------------------------------------------- close and flush


_LEAKED: list = []


def test_world_left_open_by_the_build_is_rejected(tmp_path_factory):
    def build(root: Path):
        _LEAKED.append(_raw_world(root))  # reachable, never closed
        return {}

    try:
        with pytest.raises(WorldTemplateError, match="still holds file descriptors"):
            world_template(tmp_path_factory, "left-open", build, _reopen_nothing)
    finally:
        while _LEAKED:
            _LEAKED.pop().close()


def test_world_whose_wal_cannot_be_checkpointed_is_rejected(tmp_path_factory):
    def build(root: Path):
        _raw_world(root).close()
        reader = sqlite3.connect(str(root / wt.WORLD_DB_NAME), isolation_level=None)
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM items").fetchone()  # pins a snapshot
        _LEAKED.append(reader)
        writer = _raw_world(root, rows=("gamma",))  # frames after the snapshot
        writer.close()
        return {}

    try:
        with pytest.raises(WorldTemplateError, match="could not be checkpointed"):
            world_template(tmp_path_factory, "unflushed-wal", build, _reopen_nothing)
    finally:
        while _LEAKED:
            _LEAKED.pop().close()


# ------------------------------------------------------- reopen stability


def _build_running_ask_job(root: Path):
    payload = _build_notebook(root)
    connection = sqlite3.connect(str(root / wt.WORLD_DB_NAME))
    with connection:
        connection.execute(
            "INSERT INTO ask_jobs (id, notebook_id, status) VALUES (?, ?, 'running')",
            ("job-running", payload["notebook_id"]),
        )
    connection.close()
    return payload


def _reopen_like_startup(root: Path):
    repo = SQLiteRepository(Settings())
    repo._recover_interrupted_jobs()  # what the server lifespan does on boot
    return repo


def _build_without_builtin_whitelist(root: Path):
    payload = _build_notebook(root)
    connection = sqlite3.connect(str(root / wt.WORLD_DB_NAME))
    with connection:
        connection.execute("DELETE FROM concept_whitelist WHERE note='builtin'")
    connection.close()
    return payload


@pytest.mark.parametrize(
    ("build", "reopen", "rewritten"),
    [
        pytest.param(
            _build_running_ask_job, _reopen_like_startup, "ask_jobs", id="recovered-job"
        ),
        pytest.param(
            _build_without_builtin_whitelist,
            _reopen_repository,
            "concept_whitelist",
            id="seed-insert",
        ),
    ],
)
def test_world_rewritten_by_reopening_is_rejected(
    tmp_path_factory, build, reopen, rewritten, request
):
    with pytest.raises(WorldTemplateError, match=f"not reopen-stable.*{rewritten}"):
        world_template(
            tmp_path_factory, f"reopen-{request.node.callspec.id}", build, reopen
        )


def _reopen_stamping(pragma: str):
    def reopen(root: Path):
        connection = sqlite3.connect(str(root / wt.WORLD_DB_NAME))
        connection.execute(pragma)
        connection.close()
        return None

    return reopen


@pytest.mark.parametrize(
    "pragma",
    [
        pytest.param("PRAGMA user_version = 93", id="user-version"),
        pytest.param("PRAGMA application_id = 7", id="application-id"),
        pytest.param("PRAGMA journal_mode = DELETE", id="journal-mode"),
    ],
)
def test_reopen_that_only_restamps_the_database_header_is_rejected(
    tmp_path_factory, pragma, request
):
    """A migrator that only bumps ``user_version`` on an already-current
    schema changes no row and no DDL; the header must still count."""
    with pytest.raises(WorldTemplateError, match=r"not reopen-stable.*<header>"):
        world_template(
            tmp_path_factory,
            f"header-{request.node.callspec.id}",
            _build_raw,
            _reopen_stamping(pragma),
        )


def test_seeded_admin_credential_rederivation_is_the_only_masked_rewrite(
    tmp_path_factory,
):
    """Every constructor re-hashes the admin password with a fresh salt; that
    alone must not fail an otherwise stable world (it did on every world)."""
    template = world_template(
        tmp_path_factory, "admin-rehash", _build_notebook, _reopen_repository
    )
    assert template.payload["notebook_id"].startswith("nb-")
    assert set(wt.OPEN_TIME_REWRITES) == {"users"}


# ---------------------------------------------------------------- layout


@pytest.mark.parametrize("where", ["row", "storage-file", "payload"])
def test_relocatable_world_embedding_its_build_root_is_rejected(
    tmp_path_factory, where
):
    def build(root: Path):
        _raw_world(root, rows=(str(root) if where == "row" else "plain",)).close()
        if where == "storage-file":
            storage = root / wt.WORLD_STORAGE_NAME
            storage.mkdir()
            (storage / "manifest.json").write_text(f'{{"dir": "{root}/x"}}')
        return {"dir": str(root)} if where == "payload" else {}

    with pytest.raises(WorldTemplateError, match="embeds its build root"):
        world_template(tmp_path_factory, f"embedded-{where}", build, _reopen_nothing)


class _NonAsciiBaseTemp:
    """Only ``getbasetemp()`` is read from the factory."""

    def __init__(self, base: Path) -> None:
        self._base = base

    def getbasetemp(self) -> Path:
        return self._base


@pytest.mark.parametrize("where", ["payload", "row"])
def test_non_ascii_build_root_is_still_found(tmp_path, where):
    base = tmp_path / "世界"
    base.mkdir()

    def build(root: Path):
        assert "世界" in str(root)
        _raw_world(root, rows=(str(root) if where == "row" else "plain",)).close()
        return {"dir": str(root)} if where == "payload" else {}

    with pytest.raises(WorldTemplateError, match="embeds its build root"):
        world_template(_NonAsciiBaseTemp(base), f"non-ascii-{where}", build, _reopen_nothing)


def _build_uploaded_source(root: Path):
    repo = SQLiteRepository(Settings())
    try:
        notebook = repo.create_notebook(NotebookCreate(name="Uploads"))
        repo.upload_sources(
            notebook.id,
            [
                UploadedSourceFile(
                    file_name="note.txt",
                    content_type="text/plain",
                    content=b"body\n" * 8,
                    doc_type="",
                    doc_type_explicit=False,
                )
            ],
        )
        return {"notebook_id": notebook.id}
    finally:
        repo.close()


def _source_file_paths(root: Path) -> list[str]:
    connection = sqlite3.connect(str(root / wt.WORLD_DB_NAME))
    try:
        return [row[0] for row in connection.execute("SELECT file_path FROM sources")]
    finally:
        connection.close()


def test_uploaded_sources_need_the_pinned_layout(tmp_path_factory):
    with pytest.raises(WorldTemplateError, match=r"sources\.file_path"):
        world_template(
            tmp_path_factory, "upload-relocatable", _build_uploaded_source, _reopen_repository
        )


def test_pinned_world_restores_live_from_pristine_on_every_materialize(
    tmp_path_factory, tmp_path
):
    template = world_template(
        tmp_path_factory,
        "upload-pinned",
        _build_uploaded_source,
        _reopen_repository,
        layout=PINNED,
    )
    root, _payload = template.materialize(tmp_path)
    assert root == template.live != template.pristine
    (stored,) = _source_file_paths(root)
    assert Path(stored).is_file() and Path(stored).is_relative_to(root)
    assert str(template.pristine) not in stored

    Path(stored).unlink()
    connection = sqlite3.connect(str(root / wt.WORLD_DB_NAME))
    with connection:
        connection.execute("DELETE FROM sources")
    connection.close()

    again, _payload = template.materialize(tmp_path)
    assert again == root
    assert _source_file_paths(again) == [stored]
    assert Path(stored).is_file()
    assert world_digest(again) == template.digest


# ------------------------------------------------------- pristine integrity


@pytest.mark.parametrize("tamper", ["rewrite-database", "add-file", "remove-dir"])
def test_tampered_pristine_is_rejected(tmp_path_factory, tmp_path, tamper):
    def build(root: Path):
        _raw_world(root).close()
        (root / wt.WORLD_STORAGE_NAME / "notebooks").mkdir(parents=True)
        return {}

    template = world_template(
        tmp_path_factory, f"tampered-{tamper}", build, _reopen_nothing
    )
    template.materialize(tmp_path, "before")
    if tamper == "rewrite-database":
        connection = sqlite3.connect(str(template.pristine / wt.WORLD_DB_NAME))
        with connection:
            connection.execute("INSERT INTO items VALUES ('written through a path')")
        connection.close()
    elif tamper == "add-file":
        (template.pristine / wt.WORLD_STORAGE_NAME / "stray.txt").write_text("x")
    else:
        (template.pristine / wt.WORLD_STORAGE_NAME / "notebooks").rmdir()

    with pytest.raises(WorldTemplateError, match="pristine copy was modified"):
        template.materialize(tmp_path, "after")


# --------------------------------------------------------- process caches


def _cache_container(entry: str):
    module_name, name = entry.split(":")
    return getattr(importlib.import_module(module_name), name)


def test_materialize_empties_every_identity_keyed_process_cache(
    tmp_path_factory, tmp_path
):
    from app.services.knowhow import api as knowhow_api

    class _Maintenance:
        def sweep_orphan_assets(self, notebook_id, **_options):
            return {"removed": 0}

    class _Repo:
        maintenance = _Maintenance()

    template = world_template(tmp_path_factory, "cache-hygiene", _build_raw, _reopen_nothing)
    identity_keyed = [
        entry for entry, kind in PROCESS_CACHE_INVENTORY.items() if kind == IDENTITY_RESET
    ]
    assert identity_keyed
    for entry in identity_keyed:
        container = _cache_container(entry)
        if isinstance(container, set):
            container.add("nb-from-the-previous-test")
        else:
            container["nb-from-the-previous-test"] = ((0, 0), 1)
    # The behaviour a stale entry changes: the 300 s sweep throttle.
    knowhow_api.reset_asset_sweep_throttle()
    assert knowhow_api.maybe_sweep_orphan_assets(_Repo(), "nb-world", now=1.0) is True
    assert knowhow_api.maybe_sweep_orphan_assets(_Repo(), "nb-world", now=2.0) is False

    template.materialize(tmp_path)

    assert {entry: len(_cache_container(entry)) for entry in identity_keyed} == {
        entry: 0 for entry in identity_keyed
    }
    assert knowhow_api.maybe_sweep_orphan_assets(_Repo(), "nb-world", now=3.0) is True
    knowhow_api.reset_asset_sweep_throttle()


def test_process_cache_inventory_classifies_every_cache_in_backend_app():
    scanned = wt.scan_process_caches(APP_ROOT)
    unclassified = sorted(scanned - set(PROCESS_CACHE_INVENTORY))
    stale = sorted(set(PROCESS_CACHE_INVENTORY) - scanned)
    assert (unclassified, stale) == ([], []), (
        "classify every module-level cache in tests/world_templates.py "
        "PROCESS_CACHE_INVENTORY; an identity-keyed one also needs a reset in "
        f"_IDENTITY_RESETS. unclassified={unclassified} stale={stale}"
    )
    reset_modules = {
        entry.split(":")[0]
        for entry, kind in PROCESS_CACHE_INVENTORY.items()
        if kind == IDENTITY_RESET
    }
    assert reset_modules == set(wt._IDENTITY_RESETS)
    assert set(PROCESS_CACHE_INVENTORY.values()) <= {
        wt.IDENTITY_RESET,
        wt.CONTENT_KEYED,
        wt.OWNER_SCOPED,
        wt.RUNTIME,
        wt.FACTORY,
    }


def test_process_cache_scan_finds_each_container_shape(tmp_path):
    package = tmp_path / "app"
    (package / "sub").mkdir(parents=True)
    (package / "sub" / "__init__.py").write_text("_PACKAGE_MEMO = {}\n")
    (package / "caches.py").write_text(
        "import functools\n"
        "from functools import lru_cache\n"
        "_PLAIN = {}\n"
        "_TYPED: dict[str, int] = {}\n"
        '_QUOTED: "OrderedDict[str, int]" = OrderedDict()\n'
        "_SEEN: set[str] = set()\n"
        "_WEAK = weakref.WeakKeyDictionary()\n"
        "_GROUPED = collections.defaultdict(list)\n"
        "_QUEUE = []\n"
        "TABLE = {'constant': 1}\n"
        "def helper():\n"
        "    local = {}\n"
        "    return local\n"
        "@lru_cache\n"
        "def factory():\n"
        "    return object()\n"
        "@functools.lru_cache(maxsize=1)\n"
        "@staticmethod\n"
        "def stacked():\n"
        "    return object()\n"
        "class Holder:\n"
        "    @functools.cache\n"
        "    def method(self):\n"
        "        return 1\n"
    )
    assert wt.scan_process_caches(package) == {
        "app.sub:_PACKAGE_MEMO",
        "app.caches:_PLAIN",
        "app.caches:_TYPED",
        "app.caches:_QUOTED",
        "app.caches:_SEEN",
        "app.caches:_WEAK",
        "app.caches:_GROUPED",
        "app.caches:_QUEUE",
        "app.caches:factory",
        "app.caches:stacked",
        "app.caches:method",
    }
