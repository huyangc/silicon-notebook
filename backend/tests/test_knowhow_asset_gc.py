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


def test_sweep_reclaims_an_asset_referenced_only_by_a_deleted_rows_code_attachment(repo):
    """Task 13 code review: a naive ``kind NOT IN ('cell_code_put',
    'cell_code_delete')`` exclusion only keeps CODE-ONLY kinds out of the
    history scan — but ``row_delete``'s own payload embeds the deleted row's
    remembered code attachments (a ``code`` array) in the SAME payload as its
    genuine ``cells``. An asset:// substring that lives ONLY inside that
    code array must still not count as a keeper reference once the row
    holding it is deleted, or it becomes permanently unreclaimable (removed
    stays 0 forever — this is the exact scenario the review reproduced)."""
    nb = _mk_notebook(repo)
    asset = _upload(repo, nb)
    _table_id, row_id, column_id = _make_cell(repo, nb, "plain note, no image")
    repo.upsert_knowhow_cell_code(
        row_id, column_id, f"# see asset://{asset['id']}", "python", "u1", "hash1"
    )

    repo.delete_knowhow_row(row_id)  # CASCADEs the code row; row_delete's
    # payload embeds its code_text right alongside the row's genuine cells.

    result = repo.maintenance.sweep_orphan_assets(nb)

    assert result == {"removed": 1}
    assert repo.get_notebook_asset(asset["id"]) is None


def test_sweep_reclaims_an_asset_referenced_only_by_a_deleted_columns_code_attachment(repo):
    """Same gap as the row_delete case above, but for column_delete's own
    ``code`` array (a different, top-level payload shape from row_delete's
    per-row nested one — both must be excluded, not just one)."""
    nb = _mk_notebook(repo)
    asset = _upload(repo, nb)
    _table_id, row_id, column_id = _make_cell(repo, nb, "plain note, no image")
    repo.upsert_knowhow_cell_code(
        row_id, column_id, f"# see asset://{asset['id']}", "python", "u1", "hash1"
    )

    repo.delete_knowhow_column(column_id)

    result = repo.maintenance.sweep_orphan_assets(nb)

    assert result == {"removed": 1}
    assert repo.get_notebook_asset(asset["id"]) is None


def test_classification_and_recheck_share_the_same_reference_predicate(repo, monkeypatch):
    """Task 13 code review: sweep_orphan_assets has TWO reference-determination
    call sites (the classification read, and the in-transaction re-check that
    closes the race against a concurrent cell save) — they must be the exact
    SAME code, not two independently hand-duplicated SQL strings. A prior
    implementation's own mutation test retracted each copy ONE AT A TIME and
    saw both go red, which looks like proof the two agree — it is not: the
    two sites are ANDed together (an asset is deleted only if BOTH say
    "unreferenced"), so with either copy still conservative, retracting only
    the OTHER one can't surface a divergence between them. This test instead
    asserts STRUCTURAL identity: patch the shared predicate and confirm it is
    invoked once per phase (classification, then the write-phase re-check)
    for the SAME asset — impossible if the two sites were separate code."""
    from app.repositories.sqlite import maintenance as maintenance_mod

    nb = _mk_notebook(repo)
    asset = _upload(repo, nb)  # unreferenced: one candidate for both phases

    calls: list[str] = []
    original = maintenance_mod.SQLiteMaintenanceAdapter._is_asset_referenced

    def _spy(self, db, notebook_id, asset_id):
        calls.append(asset_id)
        return original(self, db, notebook_id, asset_id)

    monkeypatch.setattr(
        maintenance_mod.SQLiteMaintenanceAdapter, "_is_asset_referenced", _spy
    )

    result = repo.maintenance.sweep_orphan_assets(nb)

    assert result == {"removed": 1}
    assert calls == [asset["id"], asset["id"]], (
        "expected exactly one classification-phase call and one write-phase "
        "re-check call, both through the SAME shared predicate"
    )


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
# knowhow 表版本管理 Task 13 (spec §7.1): historical references keep an asset
# alive too, not just the currently-live cell content. store/hist mirror the
# same fixture names test_knowhow_history_hooks.py already established (raw
# store, not the facade, so update_knowhow_cell can be called with only its
# own 3 required positional args — no notebook/table plumbing needed).
# ---------------------------------------------------------------------------


@pytest.fixture
def store(repo):
    return repo._runtime.knowhow_store


@pytest.fixture
def hist(repo):
    return repo._runtime.knowhow_history_store


@pytest.fixture
def table(repo, store) -> dict:
    """A fresh notebook + two-column table (anchor + plain) with one row —
    just enough surface for update_knowhow_cell to rewrite ``row_a``'s
    ``plain`` cell back and forth across an asset reference."""
    notebook_id = _mk_notebook(repo)
    table_id = store.create_knowhow_table(
        notebook_id, "T", "",
        [{"name": "概念", "role": "anchor"}, {"name": "备注", "role": "attribute"}],
    )
    detail = store.get_knowhow_table(table_id)
    anchor, plain = detail["columns"][0]["id"], detail["columns"][1]["id"]
    row_a = store.add_knowhow_row(table_id, {anchor: "A"})
    return {
        "id": table_id, "notebook_id": notebook_id,
        "anchor": anchor, "plain": plain, "row_a": row_a,
    }


@pytest.fixture
def asset_id(repo, table) -> str:
    return _upload(repo, table["notebook_id"])["id"]


def test_asset_referenced_only_by_history_is_not_swept(repo, store, table, asset_id):
    store.update_knowhow_cell(
        table["row_a"], table["plain"], f"![图](asset://{asset_id})"
    )
    store.update_knowhow_cell(table["row_a"], table["plain"], "图没了")

    repo.maintenance.sweep_orphan_assets(table["notebook_id"], min_age_seconds=0)

    assert repo._runtime.knowhow_store.get_notebook_asset(asset_id) is not None, (
        "历史里还引用着它——回收了就没法回退回带图的版本"
    )


def test_asset_becomes_collectable_after_history_is_pruned(repo, store, hist, table, asset_id):
    store.update_knowhow_cell(
        table["row_a"], table["plain"], f"![图](asset://{asset_id})"
    )
    store.update_knowhow_cell(table["row_a"], table["plain"], "图没了")
    # 第三次编辑，且这次改动跟这个 asset 毫无关系——prune 永远保留 head 那一条
    # （spec §7.7，否则前置指纹守卫失去参照）。如果 head 恰好停在"图没了"那
    # 条，它自己的 payload 里 before 字段仍然原样嵌着这个引用，prune 再狠也
    # 删不掉它，资产永远不会被判定为可回收——这条测试要证明的是"清理历史之后
    # 真的能回收"，所以 head 必须先挪到一条彻底不提这张图的流水上，prune 才能
    # 真正删掉最后一条还提着它的流水。
    store.update_knowhow_cell(table["row_a"], table["plain"], "跟这张图完全无关的内容")
    with hist.database.write() as db:
        db.execute(
            "UPDATE knowhow_changes SET created_at='2000-01-01T00:00:00' WHERE table_id=?",
            (table["id"],),
        )
    hist.prune(table["id"], "2001-01-01T00:00:00")

    repo.maintenance.sweep_orphan_assets(table["notebook_id"], min_age_seconds=0)

    assert repo._runtime.knowhow_store.get_notebook_asset(asset_id) is None


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
