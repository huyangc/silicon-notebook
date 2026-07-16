"""knowhow-tables PR-2+3 Task 14: asset GC.

Two independent cleanup paths for ``notebook_assets`` (rows) + their on-disk
files (``storage_dir/assets/<notebook_id>/<asset_id>.<ext>`` — see
``app.services.knowhow.assets.AssetService.path_for``):

1. Notebook delete (``NotebookCatalogService.delete_notebook`` via the
   facade) now also removes the whole per-notebook asset directory.
   ``notebook_assets`` ROWS need no explicit delete in that path — they
   already cascade via ``ON DELETE CASCADE`` + ``PRAGMA foreign_keys = ON``
   (verified in ``test_delete_notebook_removes_asset_dir_and_row`` below);
   only the on-disk files needed new code.

2. ``repo.maintenance.sweep_orphan_assets(notebook_id)`` (a maintenance
   primitive — no route this task): removes assets no knowhow cell
   ``content_md`` references any more, one notebook at a time.
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.knowhow.assets import AssetService
from app.services.sqlite_repository import SQLiteRepository


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"knowhow-asset-gc-fixture-bytes" * 5


def _settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 't.db'}",
        storage_dir=str(tmp_path / "storage"),
        event_log_enabled=False,
        llm_log_enabled=False,
    )


@pytest.fixture
def repo(tmp_path) -> SQLiteRepository:
    return SQLiteRepository(_settings(tmp_path))


def _mk_notebook(repo, name: str = "N") -> str:
    return repo.create_notebook(NotebookCreate(name=name)).id


def _upload(repo, notebook_id: str, *, filename: str = "a.png") -> dict:
    return AssetService(repo).save(notebook_id, filename, "image/png", PNG_BYTES, "u1")


def _asset_path(repo, asset: dict):
    return AssetService(repo).path_for(asset)


def _make_cell(repo, notebook_id: str, content_md: str) -> tuple[str, str, str]:
    """Create a one-column table with one row/cell holding ``content_md``.
    Returns (table_id, row_id, column_id)."""
    table_id = repo.create_knowhow_table(
        notebook_id, "T", "", [{"name": "备注", "role": "attribute"}]
    )
    column_id = repo.get_knowhow_table(table_id)["columns"][0]["id"]
    row_id = repo.add_knowhow_row(table_id, {column_id: content_md})
    return table_id, row_id, column_id


# ---------------------------------------------------------------------------
# notebook delete -> asset dir + rows
# ---------------------------------------------------------------------------


def test_delete_notebook_removes_asset_dir_and_row(repo):
    nb = _mk_notebook(repo)
    asset = _upload(repo, nb)
    path = _asset_path(repo, asset)
    assert path.is_file()
    assert repo.get_notebook_asset(asset["id"]) is not None

    repo.delete_notebook(nb)

    assert repo.get_notebook_asset(asset["id"]) is None
    assert not path.exists()
    assert not path.parent.exists()  # whole per-notebook assets/<nb>/ dir gone


def test_delete_notebook_leaves_other_notebooks_assets_intact(repo):
    nb1 = _mk_notebook(repo, "N1")
    nb2 = _mk_notebook(repo, "N2")
    asset1 = _upload(repo, nb1)
    asset2 = _upload(repo, nb2)
    path2 = _asset_path(repo, asset2)

    repo.delete_notebook(nb1)

    assert repo.get_notebook_asset(asset1["id"]) is None
    assert repo.get_notebook_asset(asset2["id"]) is not None
    assert path2.is_file()


def test_delete_notebook_without_any_assets_does_not_raise(repo):
    nb = _mk_notebook(repo)
    # No asset ever uploaded: storage_dir/assets/<nb>/ was never created.
    repo.delete_notebook(nb)  # must not raise


# ---------------------------------------------------------------------------
# sweep_orphan_assets
# ---------------------------------------------------------------------------


def test_sweep_removes_unreferenced_asset_row_and_file(repo):
    nb = _mk_notebook(repo)
    asset = _upload(repo, nb)
    path = _asset_path(repo, asset)

    result = repo.maintenance.sweep_orphan_assets(nb)

    assert result == {"removed": 1}
    assert repo.get_notebook_asset(asset["id"]) is None
    assert not path.exists()


def test_sweep_keeps_asset_referenced_by_a_cell(repo):
    nb = _mk_notebook(repo)
    asset = _upload(repo, nb)
    _make_cell(repo, nb, f"![img](asset://{asset['id']})")

    result = repo.maintenance.sweep_orphan_assets(nb)

    assert result == {"removed": 0}
    assert repo.get_notebook_asset(asset["id"]) is not None
    assert _asset_path(repo, asset).is_file()


def test_sweep_tolerates_file_already_missing_from_disk(repo):
    nb = _mk_notebook(repo)
    asset = _upload(repo, nb)
    _asset_path(repo, asset).unlink()  # simulate a file that vanished earlier

    result = repo.maintenance.sweep_orphan_assets(nb)  # must not raise

    assert result == {"removed": 1}
    assert repo.get_notebook_asset(asset["id"]) is None


def test_sweep_returns_count_and_is_idempotent(repo):
    nb = _mk_notebook(repo)
    keeper = _upload(repo, nb, filename="keep.png")
    orphan1 = _upload(repo, nb, filename="orphan1.png")
    orphan2 = _upload(repo, nb, filename="orphan2.png")
    _make_cell(repo, nb, f"![img](asset://{keeper['id']})")

    result = repo.maintenance.sweep_orphan_assets(nb)
    assert result == {"removed": 2}
    assert repo.get_notebook_asset(keeper["id"]) is not None
    assert repo.get_notebook_asset(orphan1["id"]) is None
    assert repo.get_notebook_asset(orphan2["id"]) is None

    # Re-running finds nothing left to sweep.
    assert repo.maintenance.sweep_orphan_assets(nb) == {"removed": 0}


def test_sweep_does_not_treat_code_attachment_text_as_a_reference(repo):
    """Design boundary (see sweep_orphan_assets docstring): knowhow_cell_code
    is source-code text, not rendered markdown — an asset:// substring
    appearing only in a code attachment must NOT keep the asset alive."""
    nb = _mk_notebook(repo)
    asset = _upload(repo, nb)
    _table_id, row_id, column_id = _make_cell(repo, nb, "plain note, no image")
    repo.upsert_knowhow_cell_code(
        row_id, column_id, f"# see asset://{asset['id']}", "python", "u1", "hash1"
    )

    result = repo.maintenance.sweep_orphan_assets(nb)

    assert result == {"removed": 1}
    assert repo.get_notebook_asset(asset["id"]) is None


def test_sweep_is_scoped_to_the_given_notebook(repo):
    nb1 = _mk_notebook(repo, "N1")
    nb2 = _mk_notebook(repo, "N2")
    orphan_in_nb1 = _upload(repo, nb1)
    orphan_in_nb2 = _upload(repo, nb2)

    result = repo.maintenance.sweep_orphan_assets(nb1)

    assert result == {"removed": 1}
    assert repo.get_notebook_asset(orphan_in_nb1["id"]) is None
    # nb2's own orphan is untouched by a sweep scoped to nb1.
    assert repo.get_notebook_asset(orphan_in_nb2["id"]) is not None
    assert _asset_path(repo, orphan_in_nb2).is_file()


def test_sweep_on_notebook_with_no_assets_returns_zero(repo):
    nb = _mk_notebook(repo)
    assert repo.maintenance.sweep_orphan_assets(nb) == {"removed": 0}


# ---------------------------------------------------------------------------
# real HTTP delete route (review fix)
# ---------------------------------------------------------------------------
# The DELETE /api/notebooks/{id} route reaches NotebookCatalogService DIRECTLY
# via deps.notebook_catalog_repository() (repo._runtime.catalog) — it never
# goes through the SQLiteRepository facade the fixture-level tests above use.
# The first cut of this task wired the asset-dir cleanup as a facade-forwarded
# kwarg, which left the real route without it (review-reproduced live: 204 but
# file left on disk). The cleanup now rides construction injection, so BOTH
# entry points get it; this test pins the actual HTTP path end to end.
# Client/login idioms mirror test_notebook_assets.py.


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from fastapi.testclient import TestClient

    from app.main import app
    return TestClient(app)


def _login(client, username, password="pw123456"):
    client.post("/api/auth/register", json={"username": username, "password": password})
    tok = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["token"]
    return {"Authorization": f"Bearer {tok}"}


def test_http_delete_notebook_route_removes_asset_file_and_dir(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00001401")
    nb = client.post("/api/notebooks", json={"name": "N"}, headers=owner_h).json()["id"]

    upload = client.post(
        f"/api/notebooks/{nb}/assets",
        headers=owner_h,
        files={"file": ("a.png", PNG_BYTES, "image/png")},
    )
    assert upload.status_code == 200, upload.text
    asset_id = upload.json()["id"]

    asset_dir = tmp_path / "s" / "assets" / nb
    asset_file = asset_dir / f"{asset_id}.png"
    assert asset_file.is_file()  # guard: upload really landed on disk

    resp = client.delete(f"/api/notebooks/{nb}", headers=owner_h)

    assert resp.status_code == 204, resp.text
    assert not asset_file.exists()
    assert not asset_dir.exists()
