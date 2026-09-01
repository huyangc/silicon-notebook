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

    # 批 3·W1 PR-3:DELETE 现在是 202(tombstone CAS 立即返回),资产目录清理由
    # 后台删除作业的相位 5 完成——drain 等它跑完,再断言磁盘产物真的没了。
    assert resp.status_code == 202, resp.text
    from app.services import background_jobs
    background_jobs._drain_maintenance_executors_for_tests(timeout=10.0)
    assert not asset_file.exists()
    assert not asset_dir.exists()


# ---------------------------------------------------------------------------
# 生产热点整改批 E:单遍扫描替代「逐资产前导通配 LIKE」。
# 语义面(留/删的判定)必须逐位不变——上面全部既有用例就是那份合同;这里钉的
# 是**成本形状**与「过匹配只会保留、绝不误删」的边界。
# ---------------------------------------------------------------------------


def _count_keeper_scans(repo, monkeypatch):
    """统计一次 sweep 里的判定次数,分别按「整体判定」与「历史扫描」计。"""
    import app.repositories.sqlite.maintenance as maint_mod

    adapter = maint_mod.SQLiteMaintenanceAdapter
    seen: dict[str, list] = {"decide": [], "history": []}
    real_decide = adapter._surviving_asset_refs
    real_history = adapter._history_ref_tokens

    def counting_decide(self, db, notebook_id, asset_ids):
        seen["decide"].append(notebook_id)
        return real_decide(self, db, notebook_id, asset_ids)

    def counting_history(self, db, notebook_id):
        seen["history"].append(notebook_id)
        return real_history(self, db, notebook_id)

    monkeypatch.setattr(adapter, "_surviving_asset_refs", counting_decide)
    monkeypatch.setattr(adapter, "_history_ref_tokens", counting_history)
    return seen


def test_sweep_scan_count_does_not_grow_with_the_number_of_assets(repo, monkeypatch):
    """判定扫描的次数由**阶段**决定(分类一次 + 写事务复核一次),不随资产数增长。

    变异锚点:把判定改回逐资产调用(每个 asset 一次 ``_surviving_asset_refs``)
    → 12 个资产会让计数变成 24 而不是 2。"""
    nb = _mk_notebook(repo)
    for _ in range(12):
        _upload(repo, nb)
    _make_cell(repo, nb, "没有图片")
    scans = _count_keeper_scans(repo, monkeypatch)

    assert repo.maintenance.sweep_orphan_assets(nb) == {"removed": 12}
    # 分类一次 + 写事务复核一次 —— 与资产数无关。
    assert len(scans["decide"]) == 2, scans


def test_sweep_single_pass_keeps_every_referenced_asset_among_many(repo):
    """一次扫描要同时对全部资产给出正确答案:被引用的一个都不能少留、
    没被引用的一个都不能漏删。"""
    nb = _mk_notebook(repo)
    referenced = [_upload(repo, nb) for _ in range(3)]
    orphans = [_upload(repo, nb) for _ in range(4)]
    body = "\n".join(f"![图{i}](asset://{a['id']})" for i, a in enumerate(referenced))
    _make_cell(repo, nb, body)

    assert repo.maintenance.sweep_orphan_assets(nb) == {"removed": 4}

    for asset in referenced:
        assert repo.get_notebook_asset(asset["id"]) is not None
    for asset in orphans:
        assert repo.get_notebook_asset(asset["id"]) is None


def test_sweep_keeps_an_asset_mentioned_in_prose_not_only_as_an_image(repo):
    """扫描仍是**宽松子串**匹配,不是「只认渲染出来的图片语法」:过匹配只会
    保留、绝不误删,这条不对称是这条路径一直以来的安全论证本体。"""
    nb = _mk_notebook(repo)
    asset = _upload(repo, nb)
    _make_cell(repo, nb, f"这张图的地址是 asset://{asset['id']} ,先留着")

    assert repo.maintenance.sweep_orphan_assets(nb) == {"removed": 0}
    assert repo.get_notebook_asset(asset["id"]) is not None


def test_sweep_skips_the_history_scan_when_live_cells_account_for_every_asset(
    repo, monkeypatch
):
    """活格子已经把全部候选判为「保留」时,历史那趟根本不发 —— 这是旧判据
    ``if live is not None: return True`` 短路的单遍形态。

    没有它,「资产少、历史长」这个**常见**形态(历史默认永久保留,见
    sweep_orphan_assets 的 KNOWN COST)每次清扫都要把整条变更流水解析一遍,
    只为重新确认一个已经得出的结论。

    变异锚点:删掉 ``_surviving_asset_refs`` 里的 ``if kept == candidates:``
    短路 → 历史查询次数从 0 变成 2,本用例红(既有的留/删语义用例全绿,
    因为短路是纯优化——所以必须由这条计数用例来钉)。"""
    nb = _mk_notebook(repo)
    assets = [_upload(repo, nb) for _ in range(3)]
    body = "\n".join(f"![图{i}](asset://{a['id']})" for i, a in enumerate(assets))
    _table_id, row_id, column_id = _make_cell(repo, nb, body)
    # 制造一段确实提到资产的历史,证明「跳过」不是因为历史里根本没东西可扫。
    repo.update_knowhow_cell(row_id, column_id, body + "\n补一句")
    scans = _count_keeper_scans(repo, monkeypatch)

    assert repo.maintenance.sweep_orphan_assets(nb) == {"removed": 0}

    assert len(scans["decide"]) == 1        # 分类一趟;无候选可删,不进写事务复核
    assert scans["history"] == [], scans    # 活格子已定案 → 历史一次都没扫


def test_sweep_still_consults_history_when_a_candidate_is_not_live_referenced(
    repo, monkeypatch
):
    """反向护栏:只要有一个候选活格子里找不到,历史那趟必须照发 —— 否则
    「历史里还引用着」的资产会被误删(短路不能变成跳过历史)。"""
    nb = _mk_notebook(repo)
    asset = _upload(repo, nb)
    _table_id, row_id, column_id = _make_cell(repo, nb, f"![图](asset://{asset['id']})")
    repo.update_knowhow_cell(row_id, column_id, "图没了")
    scans = _count_keeper_scans(repo, monkeypatch)

    assert repo.maintenance.sweep_orphan_assets(nb) == {"removed": 0}

    assert scans["history"], "活格子判不下来时必须继续扫历史"
    assert repo.get_notebook_asset(asset["id"]) is not None
