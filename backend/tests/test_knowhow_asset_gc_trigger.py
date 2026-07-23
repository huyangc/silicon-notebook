"""Production trigger for ``sweep_orphan_assets``.

``repo.maintenance.sweep_orphan_assets(notebook_id)`` reclaims
``notebook_assets`` rows (+ files) that no knowhow cell references any more.
It shipped fully implemented and tested (``test_knowhow_asset_gc.py``) but
with **no production caller**, so in a running deployment orphans were never
reclaimed. This module covers the caller added here.

Where the trigger lives and why: the per-table debounced, single-flight
``ProjectionScheduler`` (``app.services.knowhow.api.get_scheduler``) already
runs off the request path in a background job, already collapses rapid edits,
and fires exactly when cell ``content_md`` changed — which is precisely when
an ``asset://`` reference can go stale. So the sweep rides that completion
rather than introducing new scheduling machinery.

Why it must be throttled: ``sweep_orphan_assets`` is an O(assets x cells)
scan — one ``LIKE '%asset://<id>%'`` pass over the notebook's knowhow cells
per asset, with no index usable (leading wildcard). Cheap on a small
notebook, seconds of SQLite work on a large one, so it must never run per
projection. A per-notebook time throttle bounds it regardless of edit rate.
"""
from __future__ import annotations

import time

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.knowhow import api as knowhow_api
from app.services.knowhow.assets import AssetService
from app.services.sqlite_repository import SQLiteRepository


class _FakeMaintenance:
    def __init__(self, *, boom: bool = False) -> None:
        self.calls: list[str] = []
        self.ages: list[float] = []
        self.waivers: list[bool] = []
        self._boom = boom

    def sweep_orphan_assets(
        self,
        notebook_id: str,
        *,
        min_age_seconds: float = 0.0,
        waive_grace_if_no_tables: bool = False,
    ) -> dict:
        self.calls.append(notebook_id)
        self.ages.append(min_age_seconds)
        self.waivers.append(waive_grace_if_no_tables)
        if self._boom:
            raise RuntimeError("sweep exploded")
        return {"removed": 0}


class _FakeRepo:
    def __init__(self, *, boom: bool = False) -> None:
        self.maintenance = _FakeMaintenance(boom=boom)


@pytest.fixture(autouse=True)
def _clear_throttle():
    """Throttle state is process-global; keep tests independent."""
    knowhow_api.reset_asset_sweep_throttle()
    yield
    knowhow_api.reset_asset_sweep_throttle()


def test_first_sweep_for_a_notebook_runs():
    repo = _FakeRepo()
    assert knowhow_api.maybe_sweep_orphan_assets(repo, "nb1", now=1000.0) is True
    assert repo.maintenance.calls == ["nb1"]


def test_second_sweep_within_interval_is_skipped():
    """The hot-path guard: a burst of edits must not mean a burst of scans."""
    repo = _FakeRepo()
    knowhow_api.maybe_sweep_orphan_assets(repo, "nb1", now=1000.0)
    assert knowhow_api.maybe_sweep_orphan_assets(repo, "nb1", now=1000.0 + 1.0) is False
    just_under = 1000.0 + knowhow_api.ASSET_SWEEP_MIN_INTERVAL_SECONDS - 0.001
    assert knowhow_api.maybe_sweep_orphan_assets(repo, "nb1", now=just_under) is False
    assert repo.maintenance.calls == ["nb1"]  # still only the first one


def test_sweep_runs_again_once_the_interval_elapsed():
    repo = _FakeRepo()
    knowhow_api.maybe_sweep_orphan_assets(repo, "nb1", now=1000.0)
    later = 1000.0 + knowhow_api.ASSET_SWEEP_MIN_INTERVAL_SECONDS
    assert knowhow_api.maybe_sweep_orphan_assets(repo, "nb1", now=later) is True
    assert repo.maintenance.calls == ["nb1", "nb1"]


def test_throttle_is_per_notebook():
    """One busy notebook must not starve another notebook's GC."""
    repo = _FakeRepo()
    knowhow_api.maybe_sweep_orphan_assets(repo, "nb1", now=1000.0)
    assert knowhow_api.maybe_sweep_orphan_assets(repo, "nb2", now=1000.0) is True
    assert repo.maintenance.calls == ["nb1", "nb2"]


def test_sweep_failure_never_propagates_into_projection():
    """GC is opportunistic housekeeping; it must never fail or retry a
    projection run that already succeeded."""
    repo = _FakeRepo(boom=True)
    assert knowhow_api.maybe_sweep_orphan_assets(repo, "nb1", now=1000.0) is False
    assert repo.maintenance.calls == ["nb1"]  # attempted, swallowed


def test_failed_sweep_still_consumes_the_throttle_slot():
    """A persistently failing sweep must not turn into a retry storm on every
    subsequent projection."""
    repo = _FakeRepo(boom=True)
    knowhow_api.maybe_sweep_orphan_assets(repo, "nb1", now=1000.0)
    assert knowhow_api.maybe_sweep_orphan_assets(repo, "nb1", now=1000.0 + 1.0) is False
    assert repo.maintenance.calls == ["nb1"]


def test_missing_notebook_id_is_a_no_op():
    """Defensive only — ``project_table`` always returns an id or raises; this
    guards callers that pass a falsy id from some other path."""
    repo = _FakeRepo()
    assert knowhow_api.maybe_sweep_orphan_assets(repo, None, now=1000.0) is False
    assert repo.maintenance.calls == []


# ---------------------------------------------------------------------------
# End to end: the real projection path reclaims a real orphan.
#
# The unit tests above pin the throttle decision; these drive the actual
# production entry point (``run_projection_and_sweep``, which is exactly what
# the scheduler's ``_project`` closure calls) against a real repository, so a
# refactor that stopped calling the sweep — or a ``project_table`` that stopped
# returning its notebook id — fails here rather than passing silently.
# ---------------------------------------------------------------------------

_PNG = b"\x89PNG\r\n\x1a\n" + b"asset-gc-trigger-fixture" * 5


@pytest.fixture
def immediate_sweep(monkeypatch):
    """Drive the REAL wiring deterministically: run the background job inline and
    drop the age grace (these fixtures create an asset and sweep it in the same
    millisecond, which the production 24h grace would otherwise spare)."""
    monkeypatch.setattr(knowhow_api, "ASSET_SWEEP_MIN_AGE_SECONDS", 0.0)
    def _inline(fn, *args, name=None, notify_pending=False, **kwargs):
        fn(*args, **kwargs)  # same signature shape as background_jobs.submit

    monkeypatch.setattr(knowhow_api.background_jobs, "submit", _inline)


@pytest.fixture
def repo(tmp_path) -> SQLiteRepository:
    return SQLiteRepository(
        Settings(database_path=str(tmp_path / "t.db"), storage_dir=str(tmp_path / "storage"))
    )


def _seed_table(repo, notebook_id: str, content_md: str) -> str:
    table_id = repo.create_knowhow_table(
        notebook_id, "T", "", [{"name": "备注", "role": "attribute"}]
    )
    column_id = repo.get_knowhow_table(table_id)["columns"][0]["id"]
    repo.add_knowhow_row(table_id, {column_id: content_md})
    return table_id


def test_projection_run_reclaims_an_orphan_asset(repo, immediate_sweep):
    nb = repo.create_notebook(NotebookCreate(name="N")).id
    table_id = _seed_table(repo, nb, "no image here")
    orphan = AssetService(repo).save(nb, "a.png", "image/png", _PNG, "u1")

    knowhow_api.run_projection_and_sweep(repo, table_id)

    assert repo.get_notebook_asset(orphan["id"]) is None


def test_projection_run_keeps_an_asset_a_cell_still_references(repo, immediate_sweep):
    nb = repo.create_notebook(NotebookCreate(name="N")).id
    kept = AssetService(repo).save(nb, "a.png", "image/png", _PNG, "u1")
    table_id = _seed_table(repo, nb, f"![img](asset://{kept['id']})")

    knowhow_api.run_projection_and_sweep(repo, table_id)

    assert repo.get_notebook_asset(kept["id"]) is not None


def test_second_projection_within_the_interval_does_not_sweep_again(repo, immediate_sweep):
    """The hot-path guard, end to end: editing a table repeatedly must not
    re-run the O(assets x cells) scan on every projection."""
    nb = repo.create_notebook(NotebookCreate(name="N")).id
    table_id = _seed_table(repo, nb, "no image here")
    knowhow_api.run_projection_and_sweep(repo, table_id)  # consumes the slot

    later_orphan = AssetService(repo).save(nb, "b.png", "image/png", _PNG, "u1")
    knowhow_api.run_projection_and_sweep(repo, table_id)

    # Still there: the throttle skipped the sweep, so it survives until the
    # interval elapses (next projection after that reclaims it).
    assert repo.get_notebook_asset(later_orphan["id"]) is not None


def test_projection_run_never_touches_source_derived_images(repo, immediate_sweep):
    """REGRESSION (would be unrecoverable data loss).

    ``notebook_assets`` holds two different kinds of row: knowhow pasted
    images (``source_id IS NULL``, referenced as ``asset://<id>`` inside a
    cell's markdown) and MinerU-extracted source images (``source_id`` set,
    referenced only through ``source_elements.metadata.asset_id`` and served
    for the source view — they never appear as an ``asset://`` substring
    anywhere, see migration 19).

    The sweep's "kept" predicate only looks for ``asset://`` in knowhow cells,
    so without an explicit exclusion EVERY source-derived image looks like an
    orphan. That predicate predates migration 19 and was harmless only because
    nothing ever called the sweep — wiring up a production caller is exactly
    what would arm it, deleting both the row and the file on the first
    projection of any knowhow table in the notebook. Recovering means
    re-parsing the document, which is the very cost the image-retention stack
    exists to avoid. Source images have their own lifecycle (source delete /
    reparse cascade), so the sweep must leave them alone.
    """
    nb = repo.create_notebook(NotebookCreate(name="N")).id
    embedded = AssetService(repo).save_source_image(
        nb, "src-1", "p1.png", "image/png", _PNG, "u1"
    )
    table_id = _seed_table(repo, nb, "no image here")

    knowhow_api.run_projection_and_sweep(repo, table_id)

    assert repo.get_notebook_asset(embedded["id"]) is not None, (
        "source-derived image was swept as an orphan — unrecoverable without a re-parse"
    )


def test_recently_created_assets_are_spared(repo):
    """An image pasted into a cell the user has NOT saved yet lives only in a
    browser draft — the server sees no reference to it at all. Sweeping on age
    keeps a sweep triggered by unrelated activity in the same notebook from
    deleting it out from under the draft."""
    nb = repo.create_notebook(NotebookCreate(name="N")).id
    fresh = AssetService(repo).save(nb, "draft.png", "image/png", _PNG, "u1")

    knowhow_api.maybe_sweep_orphan_assets(repo, nb)  # production grace applies

    assert repo.get_notebook_asset(fresh["id"]) is not None


def test_grace_is_passed_through_to_the_sweep():
    repo = _FakeRepo()
    knowhow_api.maybe_sweep_orphan_assets(repo, "nb1", now=1000.0)
    assert repo.maintenance.ages == [knowhow_api.ASSET_SWEEP_MIN_AGE_SECONDS]


# ---------------------------------------------------------------------------
# Grace period is measured from "first seen unreferenced", not from upload.
# ---------------------------------------------------------------------------


def _sweep(repo, nb, grace):
    return repo.maintenance.sweep_orphan_assets(nb, min_age_seconds=grace)


def test_an_asset_that_just_became_unreferenced_is_only_marked_not_deleted(repo):
    """REGRESSION: keying the grace on upload time deleted a long-lived image
    the instant it was cut from its old cell.

    Moving an image between cells looks exactly like this on the server: the
    source cell is saved without it, and the destination cell's reference still
    lives only in an unsaved browser draft. The asset itself may be months old,
    so an upload-time grace would not protect it at all."""
    nb = repo.create_notebook(NotebookCreate(name="N")).id
    old = AssetService(repo).save(nb, "moved.png", "image/png", _PNG, "u1")
    _seed_table(repo, nb, "the cell that used to hold it, now saved without it")

    assert _sweep(repo, nb, 3600.0) == {"removed": 0}
    assert repo.get_notebook_asset(old["id"]) is not None


def test_a_reference_coming_back_clears_the_mark(repo):
    """The destination cell finally gets saved: the reference reappears, so the
    asset must never be deleted even long after the grace elapsed."""
    nb = repo.create_notebook(NotebookCreate(name="N")).id
    moved = AssetService(repo).save(nb, "moved.png", "image/png", _PNG, "u1")
    table_id = _seed_table(repo, nb, "no reference yet")

    _sweep(repo, nb, 0.001)  # marks it
    # destination cell saved -> reference is visible to the server again
    column_id = repo.get_knowhow_table(table_id)["columns"][0]["id"]
    row_id = repo.get_knowhow_table(table_id)["rows"][0]["id"]
    repo.update_knowhow_cell(row_id, column_id, f"![img](asset://{moved['id']})")
    time.sleep(0.01)

    assert _sweep(repo, nb, 0.001) == {"removed": 0}
    assert repo.get_notebook_asset(moved["id"]) is not None


def test_still_unreferenced_after_the_grace_is_finally_deleted(repo):
    """The mark is not a permanent reprieve — a genuinely abandoned asset is
    reclaimed once it has been unreferenced for the whole window."""
    nb = repo.create_notebook(NotebookCreate(name="N")).id
    abandoned = AssetService(repo).save(nb, "gone.png", "image/png", _PNG, "u1")
    _seed_table(repo, nb, "never references it")

    _sweep(repo, nb, 0.001)  # marks
    time.sleep(0.01)

    assert _sweep(repo, nb, 0.001) == {"removed": 1}
    assert repo.get_notebook_asset(abandoned["id"]) is None


def test_min_interval_zero_bypasses_the_throttle():
    """Table deletion is rare and may be the notebook's LAST chance to sweep, so
    it must not be swallowed by a sweep that ran moments earlier."""
    repo = _FakeRepo()
    knowhow_api.maybe_sweep_orphan_assets(repo, "nb1", now=1000.0)
    assert knowhow_api.maybe_sweep_orphan_assets(repo, "nb1", now=1000.5) is False
    assert knowhow_api.maybe_sweep_orphan_assets(repo, "nb1", now=1000.5, min_interval=0) is True
    assert repo.maintenance.calls == ["nb1", "nb1"]


# ---------------------------------------------------------------------------
# Classification and deletion must be atomic w.r.t. concurrent cell saves.
# ---------------------------------------------------------------------------


def test_a_reference_restored_between_scan_and_delete_is_honoured(repo):
    """REGRESSION: the keeper scan runs on a read connection that closes before
    the delete. A cell save committing in that gap would otherwise leave a live
    ``asset://`` link pointing at a deleted row and file (cell references have
    no FK to stop it).

    Simulated by restoring the reference exactly as the write transaction is
    entered — i.e. the last possible moment before deletion."""
    nb = repo.create_notebook(NotebookCreate(name="N")).id
    asset = AssetService(repo).save(nb, "raced.png", "image/png", _PNG, "u1")
    table_id = _seed_table(repo, nb, "not referenced during the scan")
    detail = repo.get_knowhow_table(table_id)
    column_id, row_id = detail["columns"][0]["id"], detail["rows"][0]["id"]

    real_write = repo._runtime.database.write
    restored = {"done": False}

    def racing_write(*args, **kwargs):
        if not restored["done"]:
            restored["done"] = True  # a concurrent save lands just before deletion
            repo.update_knowhow_cell(row_id, column_id, f"![img](asset://{asset['id']})")
        return real_write(*args, **kwargs)

    repo._runtime.database.write = racing_write
    try:
        result = repo.maintenance.sweep_orphan_assets(nb)
    finally:
        repo._runtime.database.write = real_write

    assert result == {"removed": 0}
    assert repo.get_notebook_asset(asset["id"]) is not None, (
        "deleted an asset a committed cell save had just re-referenced"
    )


def test_marks_for_vanished_assets_are_pruned(repo):
    """The adapter is process-long-lived; marks for assets that disappear by any
    other path (notably a notebook delete cascade, after which the notebook is
    never swept again) must not accumulate forever."""
    nb = repo.create_notebook(NotebookCreate(name="N")).id
    asset = AssetService(repo).save(nb, "gone.png", "image/png", _PNG, "u1")
    _seed_table(repo, nb, "never references it")
    marks = repo.maintenance._orphan_first_seen

    _sweep(repo, nb, 3600.0)  # marks it
    assert (nb, asset["id"]) in marks

    with repo._runtime.database.write() as db:  # stand in for any external removal
        db.execute("DELETE FROM notebook_assets WHERE id=?", (asset["id"],))
    _sweep(repo, nb, 3600.0)

    assert (nb, asset["id"]) not in marks


def test_concurrent_sweeps_do_not_corrupt_mark_state(repo):
    """REGRESSION: sweeps for different notebooks run in their own background
    jobs, so the mark dict is touched from several threads at once. Iterating it
    unlocked raised "dictionary changed size during iteration", which the caller
    swallowed — silently burning that notebook's throttle slot."""
    import threading

    notebooks = []
    for i in range(6):
        nb = repo.create_notebook(NotebookCreate(name=f"N{i}")).id
        AssetService(repo).save(nb, f"a{i}.png", "image/png", _PNG, "u1")
        _seed_table(repo, nb, "no reference")
        notebooks.append(nb)

    errors: list[BaseException] = []

    def worker(nb):
        try:
            for _ in range(15):
                repo.maintenance.sweep_orphan_assets(nb, min_age_seconds=3600.0)
        except BaseException as exc:  # noqa: BLE001 - the point is to catch it
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(nb,)) for nb in notebooks]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent sweeps corrupted mark state: {errors[:3]}"


# ---------------------------------------------------------------------------
# Saving must not silently persist a link to an already-reclaimed image.
# ---------------------------------------------------------------------------


def test_a_newly_added_reference_is_guarded():
    """The draft-saved-after-reclamation case: this ref is new to the cell, so
    it is handed to the write for an in-transaction existence check."""
    assert knowhow_api.newly_added_asset_refs("", "![x](asset://gone)") == ["gone"]


def test_only_the_image_form_counts_as_a_reference():
    """REGRESSION: a broad `asset://` match rejects valid Markdown.

    The renderer (knowhow-model.ts IMAGE_ASSET_URL_RE) only rewrites
    `![alt](asset://id)`; every other occurrence stays literal text. Guarding
    the broad form meant a cell merely MENTIONING the scheme — prose, a plain
    link, a code sample — demanded an asset by that name and failed the save
    with a missing-image error the user had no way to satisfy."""
    assert knowhow_api.newly_added_asset_refs("", "见 `asset://example` 这种写法") == []
    assert knowhow_api.newly_added_asset_refs("", "[链接](asset://example)") == []
    assert knowhow_api.newly_added_asset_refs("", "裸写 asset://example 不算") == []
    # ... while the real image form still is one.
    assert knowhow_api.newly_added_asset_refs("", "![图](asset://real)") == ["real"]


def test_reference_id_charset_matches_the_renderer():
    """Path-traversal-ish ids are not rewritten by the renderer, so they are not
    references here either (same `[A-Za-z0-9_-]+` pin)."""
    assert knowhow_api.newly_added_asset_refs("", "![x](asset://../../etc/passwd)") == []


def test_an_already_present_reference_is_not_guarded():
    """Only ADDED refs are checked — otherwise one dead image would freeze the
    cell, making the user unable to edit their way out of it."""
    before = "![x](asset://gone) 原文"
    after = "![x](asset://gone) 原文，补一句"
    assert knowhow_api.newly_added_asset_refs(before, after) == []


def test_message_tells_the_user_what_to_do():
    assert "重新插入" in knowhow_api.CELL_ASSET_MISSING_MESSAGE


def _cell(repo, table_id: str) -> tuple[str, str]:
    table = repo.get_knowhow_table(table_id)
    return table["rows"][0]["id"], table["columns"][0]["id"]


def test_update_cell_refuses_a_reference_to_a_deleted_asset(repo):
    """The store — not the caller — is what rejects it, so the existence check
    and the write share one transaction and one write lock."""
    nb = repo.create_notebook(NotebookCreate(name="N")).id
    table_id = _seed_table(repo, nb, "")
    row_id, column_id = _cell(repo, table_id)

    with pytest.raises(ValueError):
        repo.update_knowhow_cell(
            row_id, column_id, "![x](asset://gone)", require_assets=["gone"]
        )

    # ... and the rollback is real: nothing was written.
    assert repo.get_knowhow_table(table_id)["rows"][0]["cells"].get(column_id, "") == ""


def test_batch_update_refuses_a_reference_to_a_deleted_asset(repo):
    """The merged-cell path must not be a way around the single-cell guard."""
    nb = repo.create_notebook(NotebookCreate(name="N")).id
    table_id = _seed_table(repo, nb, "")
    _, column_id = _cell(repo, table_id)
    repo.add_knowhow_row(table_id, {})
    row_ids = [r["id"] for r in repo.get_knowhow_table(table_id)["rows"]]

    with pytest.raises(ValueError):
        repo.update_knowhow_cells(
            row_ids, column_id, "![x](asset://gone)", require_assets=["gone"]
        )

    # All-or-nothing: not one of the rows kept the dead reference.
    for row in repo.get_knowhow_table(table_id)["rows"]:
        assert row["cells"].get(column_id, "") == ""


def test_batch_guard_covers_a_ref_new_to_only_one_row(repo):
    """The editor saves shared cells through the batch path, so the guarded set
    is the UNION over target rows — a ref already present in row 0 is still new
    to row 1, and checking only the first row would have missed it."""
    nb = repo.create_notebook(NotebookCreate(name="N")).id
    table_id = _seed_table(repo, nb, "旧")
    row_id, column_id = _cell(repo, table_id)
    # New rows now validate every rendered ref. Model a pre-existing legacy
    # dead link directly so this test remains about per-target edit deltas.
    with repo._runtime.database.write() as db:
        db.execute(
            "UPDATE knowhow_cells SET content_md=? WHERE row_id=? AND column_id=?",
            ("![x](asset://gone) 旧", row_id, column_id),
        )
    repo.add_knowhow_row(table_id, {})

    added = sorted(
        {
            asset_id
            for row in repo.get_knowhow_table(table_id)["rows"]
            for asset_id in knowhow_api.newly_added_asset_refs(
                row["cells"].get(column_id, ""), "![x](asset://gone) 新"
            )
        }
    )
    assert added == ["gone"]


def test_deleting_the_last_table_reclaims_immediately(repo, monkeypatch):
    """REGRESSION: assets from a deleted final table were kept forever.

    The delete pass normally only MARKS, expecting a later projection to finish
    the job — but with no table left, no projection can ever fire, so the marks
    never matured and the files survived until the notebook was deleted. Note
    this fixture does NOT use ``immediate_sweep``: the production grace period
    is in force, and the route itself must be what drops it."""
    from app.api import knowhow_routes as routes_mod

    monkeypatch.setattr(routes_mod, "repository", lambda: repo)
    monkeypatch.setattr(
        knowhow_api.background_jobs,
        "submit",
        lambda fn, *a, name=None, notify_pending=False, **kw: fn(*a, **kw),
    )
    nb = repo.create_notebook(NotebookCreate(name="N")).id
    table_id = _seed_table(repo, nb, "no image here")
    orphan = AssetService(repo).save(nb, "a.png", "image/png", _PNG, "u1")["id"]

    routes_mod.delete_knowhow_table(nb, table_id)

    assert repo.list_knowhow_tables(nb) == []
    assert repo.get_notebook_asset(orphan) is None


def test_deleting_one_of_several_tables_still_respects_the_grace(repo, monkeypatch):
    """The immediate reclaim is justified ONLY by "no cell left to save a draft
    into". While another table survives, a draft can still be saved, so the
    grace period must stay in force."""
    from app.api import knowhow_routes as routes_mod

    monkeypatch.setattr(routes_mod, "repository", lambda: repo)
    monkeypatch.setattr(
        knowhow_api.background_jobs,
        "submit",
        lambda fn, *a, name=None, notify_pending=False, **kw: fn(*a, **kw),
    )
    nb = repo.create_notebook(NotebookCreate(name="N")).id
    doomed = _seed_table(repo, nb, "no image here")
    _seed_table(repo, nb, "another table survives")
    asset = AssetService(repo).save(nb, "a.png", "image/png", _PNG, "u1")["id"]

    routes_mod.delete_knowhow_table(nb, doomed)

    # Marked, not removed — a draft holding this image can still be saved.
    assert repo.get_notebook_asset(asset) is not None


def test_a_table_created_during_the_sweep_restores_the_grace(repo):
    """REGRESSION (data loss): the no-table waiver must be read under the write
    lock, not by the caller.

    The delete route decides to sweep while the notebook has no tables, but the
    sweep runs later in a background job. If another tab creates a table and
    uploads an image in that gap, that fresh image exists only in an unsaved
    draft — waiving the grace on the stale observation would delete it. Here the
    table appears between classification and the write transaction."""
    nb = repo.create_notebook(NotebookCreate(name="N")).id
    fresh = AssetService(repo).save(nb, "a.png", "image/png", _PNG, "u1")["id"]
    assert repo.list_knowhow_tables(nb) == []  # the waiver would apply right now

    real_write = repo._runtime.database.write
    created: list[str] = []

    def racing_write():
        # Latch BEFORE seeding: _seed_table opens write() itself and would
        # otherwise re-enter this hook forever.
        if not created:
            created.append("racing")
            _seed_table(repo, nb, "another tab was faster")
        return real_write()

    repo._runtime.database.write = racing_write
    try:
        repo.maintenance.sweep_orphan_assets(
            nb, min_age_seconds=86400.0, waive_grace_if_no_tables=True
        )
    finally:
        repo._runtime.database.write = real_write

    assert created, "the racing table was never created — test would be vacuous"
    assert repo.get_notebook_asset(fresh) is not None


def test_marks_survive_sweeps_of_other_notebooks(repo):
    """REGRESSION (GC never completes): marks used to be age-pruned across
    notebooks, but sweeps are edge-triggered — an old mark means "not edited
    since", not "stale". Dropping it restarted the grace from zero, so a
    notebook edited less often than the grace window never reclaimed anything.

    Here notebook A marks an orphan, notebook B is swept repeatedly, and A's
    mark must still authorize deletion when A is finally swept again."""
    nb_a = repo.create_notebook(NotebookCreate(name="A")).id
    nb_b = repo.create_notebook(NotebookCreate(name="B")).id
    _seed_table(repo, nb_a, "no image here")
    _seed_table(repo, nb_b, "no image here")
    orphan = AssetService(repo).save(nb_a, "a.png", "image/png", _PNG, "u1")["id"]

    grace = 0.3
    repo.maintenance.sweep_orphan_assets(nb_a, min_age_seconds=grace)  # marks only
    assert repo.get_notebook_asset(orphan) is not None

    # Age the mark past TWICE the grace before B is swept: that is exactly the
    # window the old age-based pruning used to treat as "stale", so a sweep of
    # an unrelated notebook would drop A's mark here.
    time.sleep(2 * grace + 0.1)
    for _ in range(3):
        repo.maintenance.sweep_orphan_assets(nb_b, min_age_seconds=grace)

    # A's mark is now well past its own grace, so this sweep must DELETE. If the
    # mark had been pruned above, this would only re-mark and the asset would
    # survive — the "never reclaims" failure mode.
    repo.maintenance.sweep_orphan_assets(nb_a, min_age_seconds=grace)
    assert repo.get_notebook_asset(orphan) is None


def _route_repo(monkeypatch, repo):
    """Call the route bodies directly against a real repo. The point is to
    exercise the ROUTES' own wiring — the store-level tests above pass even if
    a route forgets to pass ``require_assets`` at all."""
    from app.api import knowhow_routes as routes_mod

    monkeypatch.setattr(routes_mod, "repository", lambda: repo)
    return routes_mod


def test_single_cell_route_rejects_a_draft_whose_image_was_reclaimed(repo, monkeypatch):
    from fastapi import HTTPException

    from app.models.schemas import KnowhowCellPatch

    routes_mod = _route_repo(monkeypatch, repo)
    nb = repo.create_notebook(NotebookCreate(name="N")).id
    table_id = _seed_table(repo, nb, "")
    row_id, column_id = _cell(repo, table_id)

    with pytest.raises(HTTPException) as err:
        routes_mod.patch_knowhow_cell(
            nb, table_id, row_id, column_id, KnowhowCellPatch(content_md="![x](asset://gone)")
        )
    assert err.value.status_code == 400
    assert err.value.detail == knowhow_api.CELL_ASSET_MISSING_MESSAGE


def test_batch_route_rejects_a_draft_whose_image_was_reclaimed(repo, monkeypatch):
    """The bypass this closes: merged/shared cells save through the batch
    endpoint, so guarding only the single-cell route left a normal editor path
    able to persist a dead asset:// link."""
    from fastapi import HTTPException

    from app.models.schemas import KnowhowCellsBatchPatch

    routes_mod = _route_repo(monkeypatch, repo)
    nb = repo.create_notebook(NotebookCreate(name="N")).id
    table_id = _seed_table(repo, nb, "")
    _, column_id = _cell(repo, table_id)
    repo.add_knowhow_row(table_id, {})
    row_ids = [r["id"] for r in repo.get_knowhow_table(table_id)["rows"]]

    with pytest.raises(HTTPException) as err:
        routes_mod.patch_knowhow_cells_batch(
            nb,
            table_id,
            KnowhowCellsBatchPatch(
                column_id=column_id, row_ids=row_ids, content_md="![x](asset://gone)"
            ),
        )
    assert err.value.status_code == 400
    assert err.value.detail == knowhow_api.CELL_ASSET_MISSING_MESSAGE

    for row in repo.get_knowhow_table(table_id)["rows"]:
        assert row["cells"].get(column_id, "") == ""


def test_batch_route_still_saves_when_the_image_is_alive(repo, monkeypatch):
    """The guard must not block the normal merged-cell save."""
    from app.models.schemas import KnowhowCellsBatchPatch

    routes_mod = _route_repo(monkeypatch, repo)
    nb = repo.create_notebook(NotebookCreate(name="N")).id
    table_id = _seed_table(repo, nb, "")
    _, column_id = _cell(repo, table_id)
    repo.add_knowhow_row(table_id, {})
    row_ids = [r["id"] for r in repo.get_knowhow_table(table_id)["rows"]]
    live = AssetService(repo).save(nb, "a.png", "image/png", _PNG, "u1")["id"]

    routes_mod.patch_knowhow_cells_batch(
        nb,
        table_id,
        KnowhowCellsBatchPatch(
            column_id=column_id, row_ids=row_ids, content_md=f"![x](asset://{live})"
        ),
    )
    for row in repo.get_knowhow_table(table_id)["rows"]:
        assert live in row["cells"][column_id]


def test_an_asset_from_another_notebook_is_refused(repo):
    """Scoped, not global: the asset endpoint only serves a file to its OWN
    notebook, so a cross-notebook reference would save fine and then render as a
    permanently broken image. Reachable by simply copying a cell's markdown from
    one notebook into another."""
    mine = repo.create_notebook(NotebookCreate(name="Mine")).id
    theirs = repo.create_notebook(NotebookCreate(name="Theirs")).id
    table_id = _seed_table(repo, mine, "")
    row_id, column_id = _cell(repo, table_id)
    foreign = AssetService(repo).save(theirs, "a.png", "image/png", _PNG, "u1")["id"]

    with pytest.raises(ValueError):
        repo.update_knowhow_cell(
            row_id, column_id, f"![x](asset://{foreign})", require_assets=[foreign]
        )

    assert repo.get_knowhow_table(table_id)["rows"][0]["cells"].get(column_id, "") == ""
    # The foreign asset is untouched — refusing a reference must not reclaim it.
    assert repo.get_notebook_asset(foreign) is not None


def test_a_live_asset_passes_the_in_transaction_check(repo):
    nb = repo.create_notebook(NotebookCreate(name="N")).id
    table_id = _seed_table(repo, nb, "")
    row_id, column_id = _cell(repo, table_id)
    live = AssetService(repo).save(nb, "a.png", "image/png", _PNG, "u1")["id"]

    repo.update_knowhow_cell(
        row_id, column_id, f"![x](asset://{live})", require_assets=[live]
    )
    saved = repo.get_knowhow_table(table_id)["rows"][0]["cells"][column_id]
    assert live in saved
