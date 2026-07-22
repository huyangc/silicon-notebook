# backend/tests/test_backfill_knowhow_md.py
"""knowhow-md-normalize Task 6: one-time backfill CLI for existing (存量)
knowhow cells (``scripts/backfill_knowhow_md.py``), dry-run by default.

Loads the script via ``importlib`` from its on-disk path (mirroring
``test_merge_dbs.py``'s exact technique for testing a ``scripts/*.py`` file
that is not on ``sys.path``/importable as a normal package), rather than
reimplementing its logic here.

``repo`` builds a real ``SQLiteRepository`` through explicit env vars (same
pattern as ``test_knowhow_projection.py``'s ``repo_factory``) rather than
passing ``Settings(...)`` kwargs directly, so that ``main()``'s own internal
bare ``SQLiteRepository(Settings())`` construction (it must run outside of
this test process in production, reading real env/`.env`) resolves to the
exact same on-disk database within a single test — needed for the CLI-level
(``main(argv)``) tests below to observe the same rows the fixture wrote.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sqlite3
import sys

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import RecordingModelProvider

_SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "backfill_knowhow_md.py"
_spec = importlib.util.spec_from_file_location("backfill_knowhow_md", _SCRIPT)
bkmd = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules["backfill_knowhow_md"] = bkmd
_spec.loader.exec_module(bkmd)

plan_backfill = bkmd.plan_backfill
apply_reviewed_plan = bkmd.apply_reviewed_plan
reproject_changed_tables = bkmd.reproject_changed_tables
main = bkmd.main


def _apply_rules_plan(repo, notebook_id, plan):
    """Test helper: apply a rules-only plan through the guarded compare-and-
    write entrypoint (``apply_reviewed_plan``) and return the count of cells
    actually written -- the same number the old unguarded ``apply_backfill``
    returned, so the write-path tests below read unchanged. F3 routed the
    rules-only ``--apply`` path through this same guarded writer."""
    applied, _already, _skipped, _rejected = apply_reviewed_plan(repo, notebook_id, plan)
    return len(applied)


def _dry_run_then_apply(notebook_id, plan_path, extra_args=()):
    """Plan-handshake helper: EVERY ``--apply`` now REQUIRES a reviewed
    ``--plan`` (the rules-only plan-less exception was dropped). Run the
    dry-run (which always saves the plan) then apply THAT plan file -- the
    two-step flow the CLI now mandates for all applies. Returns the apply rc."""
    rc = main(["--notebook", notebook_id, *extra_args, "--save-plan", str(plan_path)])
    assert rc == 0, "dry-run (plan save) failed"
    return main(["--notebook", notebook_id, *extra_args, "--apply", "--plan", str(plan_path)])


def _bind_reformat(repo, client):
    repo._runtime.models.chat_clients = {
        **repo._runtime.models.chat_clients,
        "knowhow_reformat": client,
    }

# Excel-flavored raw content (tab-indented `•` bullets, `A.` section marker) --
# the exact shape rule_normalize/reformat_cell are built to clean up (same RAW
# string convention test_knowhow_reformat.py uses).
DIRTY = "A. 考量\n\t• 增大 R： 变慢\n\t• 增大 C： 变化"
CLEAN = "R 已过大"  # plain prose, nothing for rule_normalize to change
CLEAN2 = "都已修复完毕"  # a second, wholly-clean row's cells


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'khmd.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    return SQLiteRepository(Settings(model_services_config=""), model_provider=RecordingModelProvider())


@pytest.fixture
def nb_with_dirty_cells(repo) -> str:
    """One notebook, one table (anchor + procedure column), two rows: one
    whose procedure cell carries Excel-flavored markup (and an already-clean
    anchor cell), one that's entirely clean -- gives plan_backfill both a
    changed=True and a changed=False entry, and gives the apply path's
    "writes only what changed" contract a wholly-untouched row to prove
    against.

    Both rows are explicitly marked 'synced' right after creation (rather
    than relying on add_knowhow_row's own 'pending' schema default) so that
    a later observed flip back to 'pending' unambiguously means
    update_knowhow_cell actually ran on that row, not just a schema default
    carried over from creation.
    """
    nb_id = repo.create_notebook(
        NotebookCreate(name="t", purpose="p", primary_domain="d")
    ).id
    table_id = repo.create_knowhow_table(
        nb_id, "违例表", "",
        [{"name": "现象", "role": "anchor"}, {"name": "修复方法", "role": "procedure"}],
    )
    columns = {c["name"]: c["id"] for c in repo.get_knowhow_table(table_id)["columns"]}
    dirty_row_id = repo.add_knowhow_row(
        table_id, {columns["现象"]: CLEAN, columns["修复方法"]: DIRTY}
    )
    clean_row_id = repo.add_knowhow_row(
        table_id, {columns["现象"]: CLEAN2, columns["修复方法"]: CLEAN2}
    )
    repo.set_knowhow_row_projection(dirty_row_id, "synced")
    repo.set_knowhow_row_projection(clean_row_id, "synced")
    return nb_id


def _any_dirty_markers(repo, notebook_id: str) -> bool:
    """True if any cell in the notebook still carries a raw tab/bullet
    marker -- the DB-level proxy for "backfill has not actually run yet"."""
    for summary in repo.list_knowhow_tables(notebook_id):
        table = repo.get_knowhow_table(summary["id"])
        for row in table["rows"]:
            for content in row["cells"].values():
                if "\t" in (content or "") or "•" in (content or ""):
                    return True
    return False


def _find_row_id(repo, notebook_id: str, needle: str) -> str:
    """Find the row carrying a cell whose content is exactly `needle`."""
    for summary in repo.list_knowhow_tables(notebook_id):
        table = repo.get_knowhow_table(summary["id"])
        for row in table["rows"]:
            if needle in row["cells"].values():
                return row["id"]
    raise AssertionError(f"no row found with a cell == {needle!r}")


def _row_status(repo, notebook_id: str, row_id: str) -> str:
    for summary in repo.list_knowhow_tables(notebook_id):
        table = repo.get_knowhow_table(summary["id"])
        for row in table["rows"]:
            if row["id"] == row_id:
                return row["projection_status"]
    raise AssertionError(f"row {row_id} not found")


def _column_id(repo, notebook_id: str, role: str) -> str:
    """The id of the (only) column carrying ``role`` in the notebook's
    (only) table -- both fixtures below (``nb_with_dirty_cells``) create
    exactly one table with one anchor + one procedure column."""
    for summary in repo.list_knowhow_tables(notebook_id):
        table = repo.get_knowhow_table(summary["id"])
        for column in table["columns"]:
            if column["role"] == role:
                return column["id"]
    raise AssertionError(f"no column with role={role!r} found")


def _row_column_projected_text(repo, row_id: str, column_id: str) -> "str | None":
    """The TEXT actually written by KnowhowProjector.project_table for this
    (row, column) slot -- i.e. what reprojection derived from the cell,
    read straight out of ``source_elements`` (mirrors
    test_knowhow_projection.py's ``_row_element_ids``/``_row_chunk_texts``
    helpers' direct-SQL convention). ``None`` if the row/column was never
    projected at all (e.g. reprojection never ran)."""
    with repo._connect() as db:
        row = db.execute(
            "SELECT text FROM source_elements WHERE "
            "json_extract(metadata,'$.knowhow.row_id')=? AND "
            "json_extract(metadata,'$.knowhow.column_id')=?",
            (row_id, column_id),
        ).fetchone()
    return row["text"] if row else None


# ---------------------------------------------------------------------------
# plan_backfill / apply_reviewed_plan (function-level, no CLI)
# ---------------------------------------------------------------------------


def test_backfill_dry_run_does_not_write(repo, nb_with_dirty_cells):
    plan = plan_backfill(repo, nb_with_dirty_cells, use_llm=False)

    assert any(p["changed"] for p in plan)
    assert any(not p["changed"] for p in plan)  # the clean anchor cell too
    assert _any_dirty_markers(repo, nb_with_dirty_cells)  # 库未变


def test_backfill_plan_entries_have_required_shape(repo, nb_with_dirty_cells):
    plan = plan_backfill(repo, nb_with_dirty_cells, use_llm=False)
    assert plan
    for entry in plan:
        assert set(entry) == {
            "table_id", "row_id", "column_id", "before", "after", "source", "changed",
        }
    dirty_entry = next(p for p in plan if p["before"] == DIRTY)
    assert dirty_entry["changed"] is True
    assert dirty_entry["source"] == "rule"
    assert "•" not in dirty_entry["after"] and "\t" not in dirty_entry["after"]

    clean_entry = next(p for p in plan if p["before"] == CLEAN)
    assert clean_entry["changed"] is False
    assert clean_entry["after"] == CLEAN


def test_backfill_apply_then_idempotent(repo, nb_with_dirty_cells):
    dirty_row_id = _find_row_id(repo, nb_with_dirty_cells, DIRTY)

    plan = plan_backfill(repo, nb_with_dirty_cells, use_llm=False)
    written = _apply_rules_plan(repo, nb_with_dirty_cells, plan)

    assert written == sum(1 for p in plan if p["changed"])
    assert written > 0
    assert not _any_dirty_markers(repo, nb_with_dirty_cells)
    # update_knowhow_cell's existing contract: writing a cell marks its row
    # pending so reprojection recomputes KG/steps. The fixture explicitly set
    # this row to 'synced' up front, so seeing 'pending' here proves the
    # write actually happened rather than reflecting a leftover schema
    # default from row creation.
    assert _row_status(repo, nb_with_dirty_cells, dirty_row_id) == "pending"

    plan2 = plan_backfill(repo, nb_with_dirty_cells, use_llm=False)
    assert plan2  # cells are still there, just none of them changed=True now
    assert all(not p["changed"] for p in plan2)


def test_apply_backfill_skips_unchanged_entries(repo, nb_with_dirty_cells):
    clean_row_id = _find_row_id(repo, nb_with_dirty_cells, CLEAN2)

    plan = plan_backfill(repo, nb_with_dirty_cells, use_llm=False)
    clean_row_entries = [p for p in plan if p["row_id"] == clean_row_id]
    assert clean_row_entries and all(not p["changed"] for p in clean_row_entries)

    written = _apply_rules_plan(repo, nb_with_dirty_cells, plan)

    assert written > 0  # the dirty row's cell was written
    # the wholly-clean row was never sent through update_knowhow_cell at all
    # -- it stays 'synced' (the fixture's explicit baseline) rather than
    # flipping to 'pending' the way the dirty row's own write does.
    assert _row_status(repo, nb_with_dirty_cells, clean_row_id) == "synced"


class _BoomLLMClient:
    """A CONFIGURED client whose chat_json blows up if ever invoked --
    proves use_llm=False takes the rule_normalize path directly and never
    reaches the LLM at all, not merely that its result happens to match."""

    configured = True
    model = "boom-model"

    def chat_json(self, *args, **kwargs):
        raise AssertionError("use_llm=False must never call the LLM")


def test_use_llm_false_never_touches_the_llm_client(repo, nb_with_dirty_cells):
    _bind_reformat(repo, _BoomLLMClient())

    plan = plan_backfill(repo, nb_with_dirty_cells, use_llm=False)

    assert any(p["changed"] for p in plan)
    dirty_entry = next(p for p in plan if p["before"] == DIRTY)
    assert dirty_entry["source"] == "rule"


# ---------------------------------------------------------------------------
# C1 layer 2 (defense in depth): plan_backfill's use_llm=False (rule-only)
# path must ALSO gate its candidate through content_invariant before using
# it -- the guard already exists (md_normalize.content_invariant) but was
# never applied to the always-on rule path, so a rule_normalize bug (like
# C1's fence/table mangling) would previously have sailed straight into the
# database unchecked. After the C1 root fix, a real cell that trips this
# gate is hard to construct on purpose (that's the point of the fix) -- so
# this test forces the failure by monkeypatching rule_normalize itself to
# return corrupted (unrelated) text, proving the gate actually fires rather
# than merely existing in the source.
# ---------------------------------------------------------------------------


def test_backfill_gate_blocks_invariant_violating_candidate(
    repo, nb_with_dirty_cells, monkeypatch
):
    real_rule_normalize = bkmd.md_normalize.rule_normalize

    def _corrupt_only_dirty(raw):
        if raw == DIRTY:
            return "完全无关的文本"   # deliberately content-destroying candidate
        return real_rule_normalize(raw)

    monkeypatch.setattr(bkmd.md_normalize, "rule_normalize", _corrupt_only_dirty)

    plan = plan_backfill(repo, nb_with_dirty_cells, use_llm=False)

    dirty_entry = next(p for p in plan if p["before"] == DIRTY)
    # the corrupted candidate must NOT be used -- stored/reported text stays
    # byte-identical to the original, not the (invariant-violating) candidate.
    assert dirty_entry["after"] == DIRTY
    assert dirty_entry["changed"] is False
    # distinct source label (not plain "rule") so it's visible in the
    # by-source summary Counter as needing manual attention, separate from
    # cells that were simply already clean.
    assert dirty_entry["source"] == "rule/invariant-failed"

    # every OTHER cell is completely unaffected -- proves the gate is scoped
    # per-cell (keyed off content_invariant of THAT candidate), not some
    # blanket "rule_normalize misbehaved once, disable it for everything".
    # (CLEAN2 is in BOTH the anchor column -- now source="anchor", skipped --
    # and the procedure column; pick the procedure one, still a plain "rule".)
    clean_entry = next(p for p in plan if p["before"] == CLEAN2 and p["source"] == "rule")
    assert clean_entry["changed"] is False

    # applying must not write the corrupted candidate anywhere either (the
    # gated entry is changed=False, so the guarded writer never stages it).
    _apply_rules_plan(repo, nb_with_dirty_cells, plan)
    assert _find_row_id(repo, nb_with_dirty_cells, DIRTY)  # original text still verbatim in DB


class _FakeLLMClient:
    configured = True
    model = "fake-reformat-model"

    def __init__(self, reply: str):
        self.reply = reply
        self.calls = 0

    def chat_json(self, messages, schema_hint, **kwargs):
        self.calls += 1
        return json.dumps({"reformatted_md": self.reply}, ensure_ascii=False)


def test_use_llm_true_delegates_to_reformat_cell(repo, nb_with_dirty_cells):
    good = "**A. 考量**\n\n- 增大 R:变慢\n- 增大 C:变化"  # format-only rewrite, passes content_invariant
    client = _FakeLLMClient(good)
    _bind_reformat(repo, client)

    plan = plan_backfill(repo, nb_with_dirty_cells, use_llm=True)

    dirty_entry = next(p for p in plan if p["before"] == DIRTY)
    assert dirty_entry["source"] == "llm"
    assert dirty_entry["after"] == good
    # reformat_cell runs once per non-empty NON-ANCHOR cell. P1-c skips the
    # anchor column entirely (it's a grouping key, never normalized), so the
    # two anchor cells (现象: CLEAN, CLEAN2) never reach the LLM -- only the two
    # 修复方法 procedure cells (DIRTY, CLEAN2) do, one call each.
    assert client.calls == 2


# ---------------------------------------------------------------------------
# plan_backfill(use_llm=True) memoizes on (column_id, exact before text) --
# code-review fix for the same root cause the frontend batch modal has:
# anchor-grouped tables forward-fill one shared value across sibling rows, so
# the identical cell content appears in multiple (row, column) slots. Without
# memoization, plan_backfill would call reformat_cell once per slot even when
# several slots share byte-identical content -- wasted LLM calls, and (since
# the LLM runs at temperature=1.0 with no caching) each identical input could
# come back with a DIFFERENT, individually-valid rewrite, making previously-
# identical sibling cells gratuitously diverge after backfill.
# ---------------------------------------------------------------------------

# All distinct valid bullet glyphs recognized by md_normalize.classify_line
# (BULLET_GLYPHS + the ASCII "-*+" alternatives) -- content_signature/
# content_invariant strip the marker character entirely regardless of which
# of these is used, so cycling through them lets _CountingLLMClient produce a
# genuinely different (but still content-invariant-passing) rewrite on every
# call, with enough distinct values that no two calls in this test's
# worst-case (non-deduped) call count could coincidentally match.
_DISTINCT_BULLETS = "-*+•●◦▪‣·"


class _CountingLLMClient:
    """Configured client whose chat_json returns a DIFFERENT rewrite every
    call (cycling through _DISTINCT_BULLETS) -- mirrors the real
    temperature=1.0/no-caching root cause where reformatting the SAME input
    twice yields two DIFFERENT, individually valid outputs. If
    plan_backfill calls this once per duplicate cell instead of memoizing,
    the duplicates' `after` values will diverge; if it memoizes correctly,
    only the first occurrence of each distinct (column, content) pair ever
    reaches this client."""

    configured = True
    model = "counting-fake-model"

    def __init__(self):
        self.calls = 0

    def chat_json(self, messages, schema_hint, **kwargs):
        bullet = _DISTINCT_BULLETS[self.calls % len(_DISTINCT_BULLETS)]
        self.calls += 1
        reply = f"**A. 考量**\n\n{bullet} 增大 R:变慢\n{bullet} 增大 C:变化"
        return json.dumps({"reformatted_md": reply}, ensure_ascii=False)


@pytest.fixture
def nb_with_duplicate_cells(repo) -> str:
    """One table whose anchor column AND procedure column each carry
    byte-identical content forward-filled across three sibling rows (mirrors
    a real anchor group: the shared anchor value itself, plus a
    shared-column attribute, both repeated verbatim on every row of the
    group) plus a fourth row with distinct content in both columns --
    gives plan_backfill(use_llm=True) genuine duplicates in two columns to
    collapse into one LLM call each, and non-duplicates to prove the
    memoization keys on (column, content) rather than collapsing an entire
    column -- or the whole table -- to a single call regardless of input."""
    nb_id = repo.create_notebook(
        NotebookCreate(name="t2", purpose="p", primary_domain="d")
    ).id
    table_id = repo.create_knowhow_table(
        nb_id, "重复内容表", "",
        [{"name": "现象", "role": "anchor"}, {"name": "修复方法", "role": "procedure"}],
    )
    columns = {c["name"]: c["id"] for c in repo.get_knowhow_table(table_id)["columns"]}
    for _ in range(3):
        repo.add_knowhow_row(
            table_id, {columns["现象"]: "同一现象", columns["修复方法"]: DIRTY}
        )
    repo.add_knowhow_row(
        table_id, {columns["现象"]: "另一现象", columns["修复方法"]: CLEAN2}
    )
    return nb_id


def test_use_llm_true_dedupes_repeated_column_content(repo, nb_with_duplicate_cells):
    client = _CountingLLMClient()
    _bind_reformat(repo, client)

    plan = plan_backfill(repo, nb_with_duplicate_cells, use_llm=True)

    duplicate_entries = [p for p in plan if p["before"] == DIRTY]
    assert len(duplicate_entries) == 3
    assert all(p["source"] == "llm" for p in duplicate_entries)
    after_values = {p["after"] for p in duplicate_entries}
    assert len(after_values) == 1, (
        "duplicate cells with byte-identical (column, before) got DIFFERENT "
        f"rewrites: {after_values} -- each distinct (column, content) pair "
        "must be reformatted only once and the result reused for every "
        "sibling with the same content"
    )

    # The 4th row's distinct content is its own (column, content) pair --
    # confirms the memoization keys on content rather than coalescing every
    # cell in the column into one bucket regardless of what it says. (Its
    # `source` isn't asserted here: this fake client always replies with a
    # fixed "A. 考量/R/C" rewrite, which fails content_invariant against
    # CLEAN2's unrelated text and falls back to rule -- irrelevant to what
    # this test is verifying.)
    distinct_entry = next(p for p in plan if p["before"] == CLEAN2)
    assert distinct_entry["after"] not in after_values

    # P1-c skips the anchor column (现象) entirely, so only the 修复方法
    # procedure column reaches the LLM. Just 2 distinct (column_id, before)
    # pairs there: 修复方法×{DIRTY, CLEAN2}. (The 现象 anchor cells --
    # "同一现象"/"另一现象" -- are never reformatted.) NOT 8 (one call per
    # non-empty cell) and NOT 4 (anchor cells no longer counted).
    assert client.calls == 2


# ---------------------------------------------------------------------------
# main() — CLI-level argv/dry-run/--apply/--use-llm behavior
# ---------------------------------------------------------------------------


def test_main_requires_notebook_argument():
    with pytest.raises(SystemExit):
        main([])


def test_main_dry_run_is_the_default_and_writes_nothing(repo, nb_with_dirty_cells, capsys, tmp_path):
    rc = main(["--notebook", nb_with_dirty_cells, "--save-plan", str(tmp_path / "plan.json")])

    assert rc == 0
    assert _any_dirty_markers(repo, nb_with_dirty_cells)
    out = capsys.readouterr()
    assert "总格子数" in out.out or "总格子数" in out.err


def test_main_apply_flag_writes_changes(repo, nb_with_dirty_cells, tmp_path):
    rc = _dry_run_then_apply(nb_with_dirty_cells, tmp_path / "plan.json")

    assert rc == 0
    assert not _any_dirty_markers(repo, nb_with_dirty_cells)


def test_main_apply_records_backfill_origin_and_owner_actor(
    repo, nb_with_dirty_cells, tmp_path,
):
    """knowhow 表版本管理 Task 13 code review: actor/origin threading through
    the --apply path had ZERO test coverage anywhere (the only actor
    assertion in the whole suite was a store-level test calling
    update_knowhow_cell directly with a literal "user-1") — a mutation
    reverting apply_reviewed_plan's real threaded ``actor``/``origin="backfill"``
    back to their defaults left the entire 4817-test suite green. Assert the
    real end-to-end shape: every ``cell_update`` flow entry --apply produces
    carries ``origin="backfill"`` and ``actor`` == the notebook OWNER's
    resolved id (``resolve_notebook_owner_profile`` — the same helper
    ``--use-llm`` already relies on for its own per-user model resolution) —
    never empty, never the manual editor's ``"user"``."""
    table_id = repo.list_knowhow_tables(nb_with_dirty_cells)[0]["id"]
    hist = repo._runtime.knowhow_history_store
    seq_before = hist.head_seq(table_id)

    rc = _dry_run_then_apply(nb_with_dirty_cells, tmp_path / "plan.json")
    assert rc == 0

    owner = repo.maintenance.resolve_notebook_owner_profile(nb_with_dirty_cells)
    assert owner is not None, "test fixture assumption broken: notebook has no resolvable owner"

    new_changes = [c for c in hist.list_changes(table_id, limit=50) if c["seq"] > seq_before]
    cell_updates = [c for c in new_changes if c["kind"] == "cell_update"]
    assert cell_updates, "apply must have actually written at least one cell"
    for change in cell_updates:
        assert change["origin"] == "backfill"
        assert change["actor"] == owner.id
        assert change["actor"] != ""


def test_main_use_llm_unconfigured_prints_no_silent_degradation_warning(
    repo, nb_with_dirty_cells, capsys, tmp_path
):
    """Repo rule: --use-llm requested but the rewrite model isn't actually
    configured (test env has no LLM configured by default) must surface an
    explicit warning rather than silently proceeding as if nothing were
    missing."""
    rc = main(["--notebook", nb_with_dirty_cells, "--use-llm", "--save-plan", str(tmp_path / "plan.json")])

    assert rc == 0
    captured = capsys.readouterr()
    msg = captured.out + captured.err
    assert "WARNING" in msg
    # unconfigured => rule/no-llm wording, distinct from the configured-but-
    # degraded rule/llm-failed case (see the dedicated test below).
    assert "rule/no-llm" in msg


def test_main_use_llm_configured_prints_no_warning(
    repo, nb_with_dirty_cells, capsys, monkeypatch, tmp_path
):
    _bind_reformat(repo, _FakeLLMClient(
        "**A. 考量**\n\n- 增大 R:变慢\n- 增大 C:变化"
    ))
    # main() constructs its own SQLiteRepository(Settings()) internally (the
    # production entry point has no repo to inject) -- force it to reuse
    # THIS test's repo object (same process, fake client already installed)
    # instead of a fresh instance that would have no rewrite client at all.
    monkeypatch.setattr(bkmd, "SQLiteRepository", lambda *a, **k: repo)

    # The fixed reply format-rewrites DIRTY (-> source="llm", changed=True); for
    # the already-clean CLEAN2 procedure cell it fails content_invariant and
    # falls back to rules (source="rule/llm-failed", changed=False). A
    # rule/llm-failed with changed=False is NOT a material degradation (the cell
    # was fine either way), so the extended warning deliberately does not fire
    # on it -- the LLM worked for the cell that actually needed changing.
    rc = main(["--notebook", nb_with_dirty_cells, "--use-llm", "--save-plan", str(tmp_path / "plan.json")])

    assert rc == 0
    captured = capsys.readouterr()
    assert "WARNING" not in captured.out and "WARNING" not in captured.err


# ---------------------------------------------------------------------------
# P1-3 (code review, most important): the apply write only marks a written
# row's projection_status 'pending' (update_knowhow_cell's existing, UNCHANGED
# contract) -- unlike the HTTP cell-update route, nothing in this one-shot CLI
# process ever schedules or RUNS the projector afterwards. Left 'pending', a
# row is not just stale: the next SERVER START runs
# migrations.py::_recover_interrupted_jobs, which treats a still-
# 'pending'/'syncing' row as an abandoned crash artifact and flips it to
# 'failed' (this actually happened in production: 10 rows in a real notebook
# went to 'failed' after running this script). Recovery no longer fires on
# every SQLiteRepository construction (it moved to startup_warmup.run_startup,
# so this CLI itself is harmless now), but a row left 'pending' still dies at
# the next restart -- so the row must be settled here regardless. The fix adds
# a SEPARATE, explicit reprojection step (``reproject_changed_tables``) that main() runs
# synchronously, in-process, after the apply write returns and before main()
# itself returns -- never a background schedule (that would just move the
# same bug one layer down: a short-lived CLI process exits right past a
# debounced/backgrounded run before it ever starts).
# ---------------------------------------------------------------------------


def test_reproject_changed_tables_dedupes_and_skips_tables_without_changes(monkeypatch):
    """Pure grouping-logic test with a spy projector (no real DB/projection
    work) -- reproject_changed_tables must call project_table exactly ONCE
    per DISTINCT table_id that owns at least one changed=True entry: several
    changed entries in the SAME table collapse to a single call (not once
    per cell), and a table whose entries are all changed=False is skipped
    entirely (nothing in it needs reprojecting)."""
    calls: list[str] = []

    class _FakeProjector:
        def project_table(self, table_id):
            calls.append(table_id)

    monkeypatch.setattr(bkmd, "build_projector", lambda repo: _FakeProjector())

    plan = [
        {"table_id": "t1", "row_id": "r1", "column_id": "c1", "before": "x",
         "after": "y", "source": "rule", "changed": True},
        {"table_id": "t1", "row_id": "r2", "column_id": "c1", "before": "x",
         "after": "y", "source": "rule", "changed": True},
        {"table_id": "t2", "row_id": "r3", "column_id": "c1", "before": "x",
         "after": "x", "source": "rule", "changed": False},
        {"table_id": "t3", "row_id": "r4", "column_id": "c1", "before": "x",
         "after": "z", "source": "rule", "changed": True},
    ]

    reprojected = reproject_changed_tables(object(), plan)

    assert reprojected == ["t1", "t3"]  # t2 had no changed cell -- skipped
    assert calls == ["t1", "t3"]  # exactly one project_table call per distinct table


def test_apply_then_reproject_settles_rows_to_synced_with_new_projected_content(
    repo, nb_with_dirty_cells
):
    """The core P1-3 guarantee, at the plan_backfill/apply_reviewed_plan/
    reproject_changed_tables function level (no CLI argv involved): after
    BOTH steps run, the affected row is 'synced' -- never 'pending' (the
    original bug) or 'failed' (what _recover_interrupted_jobs does to a
    still-'pending' row at the next server start) -- and the
    PROJECTED artifact (source_elements, read directly via SQL, not just the
    store's own cell) reflects the NEW, cleaned content rather than stale
    pre-backfill text or nothing at all."""
    dirty_row_id = _find_row_id(repo, nb_with_dirty_cells, DIRTY)
    method_col_id = _column_id(repo, nb_with_dirty_cells, "procedure")

    plan = plan_backfill(repo, nb_with_dirty_cells, use_llm=False)
    written = _apply_rules_plan(repo, nb_with_dirty_cells, plan)
    assert written > 0
    # the guarded write alone (update_knowhow_cells_bulk_guarded's per-row
    # effect) still only reaches 'pending' -- reprojection is a genuinely
    # SEPARATE step, not folded into the write itself.
    assert _row_status(repo, nb_with_dirty_cells, dirty_row_id) == "pending"

    reprojected = reproject_changed_tables(repo, plan)
    assert len(reprojected) == 1

    assert _row_status(repo, nb_with_dirty_cells, dirty_row_id) == "synced"
    projected_text = _row_column_projected_text(repo, dirty_row_id, method_col_id)
    assert projected_text is not None
    assert "\t" not in projected_text and "•" not in projected_text


def test_main_apply_completes_reprojection_before_returning(repo, nb_with_dirty_cells, tmp_path):
    """CLI-level version of the same guarantee: by the time main() RETURNS —
    not just "eventually", the way the HTTP PATCH-cell route's debounced
    background scheduler would — every row touched by --apply must already
    be 'synced'."""
    dirty_row_id = _find_row_id(repo, nb_with_dirty_cells, DIRTY)

    rc = _dry_run_then_apply(nb_with_dirty_cells, tmp_path / "plan.json")

    assert rc == 0
    assert _row_status(repo, nb_with_dirty_cells, dirty_row_id) == "synced"


def test_main_apply_prints_what_was_reprojected(repo, nb_with_dirty_cells, capsys, tmp_path):
    rc = _dry_run_then_apply(nb_with_dirty_cells, tmp_path / "plan.json")

    assert rc == 0
    captured = capsys.readouterr()
    assert "重投影" in captured.out or "重投影" in captured.err


def test_main_apply_survives_a_later_server_startup_recovery_without_going_failed(
    repo, nb_with_dirty_cells, tmp_path
):
    """The exact production incident this fix addresses (task brief: "10
    rows in a real notebook went to failed"): a row left 'pending' after
    --apply survives only until the next SERVER START --
    migrations.py::_recover_interrupted_jobs flips any still-'pending'/
    'syncing' row to 'failed' (its crash-recovery heuristic: a background job
    that outlived its process). Reprojecting synchronously before main()
    returns must leave the row 'synced', so a later server startup against the
    SAME on-disk DB must NOT reclassify it as 'failed'.

    Recovery is driven EXPLICITLY here because that is now the only way it
    runs: it moved out of ``SQLiteRepository.__init__`` into
    ``startup_warmup.run_startup`` (so this CLI's own repository construction
    is no longer the trigger -- see test_startup_recovery_ownership.py). Left
    as a bare second construction, this test would pass vacuously."""
    dirty_row_id = _find_row_id(repo, nb_with_dirty_cells, DIRTY)

    rc = _dry_run_then_apply(nb_with_dirty_cells, tmp_path / "plan.json")
    assert rc == 0

    server_repo = SQLiteRepository(Settings())  # 服务端重启:构造 + 显式清算
    server_repo._recover_interrupted_jobs()
    assert _row_status(server_repo, nb_with_dirty_cells, dirty_row_id) == "synced"


# ---------------------------------------------------------------------------
# apply_reviewed_plan is the SAME guarded compare-and-write for both apply paths
# (rules-only --apply --plan and --use-llm --apply --plan). It re-reads and
# compares INSIDE the write transaction, so a stale expected_before is SKIPPED,
# not overwritten (the moved-target guarantee). This function-level test drives
# the guarded writer directly with an in-process plan (no CLI), so it is
# independent of the CLI's plan-handshake requirement.
# ---------------------------------------------------------------------------


def test_rules_only_apply_skips_cell_edited_after_in_process_plan(repo, nb_two_dirty):
    """Simulate the race at the store level (a live edit between the plan and the
    write): apply_reviewed_plan must SKIP the moved cell rather than clobber it,
    and still write the untouched one."""
    r1 = _find_row_id(repo, nb_two_dirty, DIRTY)
    r2 = _find_row_id(repo, nb_two_dirty, DIRTY_B)
    method_col = _column_id(repo, nb_two_dirty, "procedure")

    plan = plan_backfill(repo, nb_two_dirty, use_llm=False)
    # a live backend user edits r1 AFTER the plan captured its before
    repo.update_knowhow_cell(r1, method_col, "并发编辑，不应被覆盖")

    applied, _already, skipped, rejected = apply_reviewed_plan(repo, nb_two_dirty, plan)

    applied_cells = {(a["row_id"], a["column_id"]) for a in applied}
    assert (r1, method_col) not in applied_cells  # moved target NOT written
    assert any(s["row_id"] == r1 and s["reason"] == "moved" for s in skipped)
    assert rejected == []
    # r1 keeps the concurrent edit; r2 (unchanged since the plan) IS normalized.
    assert _cell_text(repo, nb_two_dirty, r1, method_col) == "并发编辑，不应被覆盖"
    r2_text = _cell_text(repo, nb_two_dirty, r2, method_col)
    assert "•" not in r2_text and "\t" not in r2_text


# ---------------------------------------------------------------------------
# F3 (this batch): rules-only ``--apply`` without ``--plan`` is now ALSO a hard
# error -- the rules-only plan-less exception was DROPPED. Re-planning at apply
# time reads the CURRENT db, so a cell edited after the reviewed dry-run would
# enter a fresh plan carrying its current content, pass the guarded write, and
# get written despite never being reviewed. So EVERY --apply must carry a
# reviewed --plan; the CLI-level "skip a concurrently-edited cell" coverage now
# lives on the --plan path (test_apply_with_plan_skips_cell_edited_after_review).
# ---------------------------------------------------------------------------


def test_rules_only_apply_without_plan_is_a_hard_error_and_writes_nothing(
    repo, nb_with_dirty_cells, capsys
):
    rc = main(["--notebook", nb_with_dirty_cells, "--apply"])

    assert rc != 0
    msg = "".join(capsys.readouterr())
    # points at the two-step plan handshake (same usage style as --use-llm).
    assert "--plan" in msg
    assert _any_dirty_markers(repo, nb_with_dirty_cells)  # wrote NOTHING


# ---------------------------------------------------------------------------
# P1-c: the anchor column is a grouping KEY, not prose. plan_backfill must
# NEVER normalize it (both the rule and the LLM path) -- an Excel-idiom-shaped
# anchor cell must yield a byte-stable, changed=False entry (source="anchor"),
# never a normalized rewrite that would split it off from its existing group.
# ---------------------------------------------------------------------------


def test_backfill_never_normalizes_anchor_column(repo):
    nb_id = repo.create_notebook(
        NotebookCreate(name="anchor-nb", purpose="p", primary_domain="d")
    ).id
    table_id = repo.create_knowhow_table(
        nb_id, "违例表", "",
        [{"name": "概念", "role": "anchor"}, {"name": "方法", "role": "procedure"}],
    )
    columns = {c["name"]: c["id"] for c in repo.get_knowhow_table(table_id)["columns"]}
    # anchor cell is Excel-idiom-shaped (`A. 概念`) -- exactly what rule_normalize
    # WOULD turn into `**A. 概念**` if it weren't the grouping key.
    repo.add_knowhow_row(table_id, {columns["概念"]: "A. 概念", columns["方法"]: DIRTY})

    for use_llm in (False, True):
        plan = plan_backfill(repo, nb_id, use_llm=use_llm)
        anchor_entries = [p for p in plan if p["before"] == "A. 概念"]
        # NO normalized entry: either absent, or present-but-byte-stable.
        assert all(
            not p["changed"] and p["after"] == "A. 概念" for p in anchor_entries
        ), (use_llm, anchor_entries)
        # the non-anchor procedure cell IS still planned + normalized.
        proc_entry = next(p for p in plan if p["before"] == DIRTY)
        assert proc_entry["changed"] is True
        assert "•" not in proc_entry["after"] and "\t" not in proc_entry["after"]


# ---------------------------------------------------------------------------
# Fold-in: the --use-llm no-silent-degradation warning also counts the
# configured-but-degraded rule/llm-failed case (LLM ran, its output failed the
# content-invariant check, rules fallback then actually changed the cell),
# with wording that distinguishes it from the unconfigured rule/no-llm case.
# ---------------------------------------------------------------------------


class _AlwaysBadReformatClient:
    """Configured client whose rewrite ALWAYS fails content_invariant (returns
    unrelated text), forcing reformat_cell's rule fallback -> rule/llm-failed."""

    configured = True
    model = "always-bad-model"

    def chat_json(self, messages, schema_hint, **kwargs):
        return json.dumps(
            {"reformatted_md": "完全无关且更长的替代文本，用于触发内容不变式校验失败"},
            ensure_ascii=False,
        )


def test_main_use_llm_llm_failed_prints_degradation_warning(
    repo, nb_with_dirty_cells, capsys, monkeypatch, tmp_path
):
    _bind_reformat(repo, _AlwaysBadReformatClient())
    monkeypatch.setattr(bkmd, "SQLiteRepository", lambda *a, **k: repo)

    rc = main(["--notebook", nb_with_dirty_cells, "--use-llm", "--save-plan", str(tmp_path / "plan.json")])

    assert rc == 0
    msg = "".join(capsys.readouterr())
    assert "WARNING" in msg
    # configured-but-degraded => rule/llm-failed wording, NOT the unconfigured
    # rule/no-llm wording (the model IS configured here, it just misbehaved).
    assert "rule/llm-failed" in msg
    assert "rule/no-llm" not in msg


# ---------------------------------------------------------------------------
# P1-a: the dry-run -> review -> apply plan-file handshake. What gets applied
# must be exactly what was reviewed. Dry-run ALWAYS writes a plan file;
# --apply --plan re-applies THAT file verbatim; --use-llm --apply WITHOUT
# --plan is a hard error (stochastic model => never-reviewed writes).
# ---------------------------------------------------------------------------

def _cell_text(repo, notebook_id: str, row_id: str, column_id: str) -> "str | None":
    """The CURRENT stored content of one (row, column) cell (not the projected
    element -- see ``_row_column_projected_text`` for that)."""
    for summary in repo.list_knowhow_tables(notebook_id):
        table = repo.get_knowhow_table(summary["id"])
        for row in table["rows"]:
            if row["id"] == row_id:
                return row["cells"].get(column_id)
    return None


DIRTY_B = "B. 其它\n\t• 项 X\n\t• 项 Y"  # a second, distinct Excel-idiom cell


@pytest.fixture
def nb_two_dirty(repo) -> str:
    """One table (anchor + procedure), two rows each with a DISTINCT dirty
    procedure cell -- gives the apply-with-plan tests two changed entries so
    one can be mutated/skipped while the other still applies."""
    nb_id = repo.create_notebook(
        NotebookCreate(name="two-dirty", purpose="p", primary_domain="d")
    ).id
    table_id = repo.create_knowhow_table(
        nb_id, "t", "",
        [{"name": "现象", "role": "anchor"}, {"name": "方法", "role": "procedure"}],
    )
    columns = {c["name"]: c["id"] for c in repo.get_knowhow_table(table_id)["columns"]}
    r1 = repo.add_knowhow_row(table_id, {columns["现象"]: "甲现象", columns["方法"]: DIRTY})
    r2 = repo.add_knowhow_row(table_id, {columns["现象"]: "乙现象", columns["方法"]: DIRTY_B})
    repo.set_knowhow_row_projection(r1, "synced")
    repo.set_knowhow_row_projection(r2, "synced")
    return nb_id


class _TwoAnswerReformatClient:
    """Configured client returning DIFFERENT (but both content-invariant-
    passing) rewrites on successive calls -- mirrors the real temperature=1.0/
    no-caching root cause. ANSWERS[0] is the reviewed candidate a dry-run plans;
    ANSWERS[1] is what a (wrongly) re-planning apply would produce instead. They
    differ only in bullet glyph, so both pass content_invariant against DIRTY --
    the byte difference is the discriminator proving apply used the FILE, not a
    re-plan."""

    configured = True
    model = "two-answer-model"
    ANSWERS = [
        "**A. 考量**\n\n- 增大 R：变慢\n- 增大 C：变化",   # [0] reviewed
        "**A. 考量**\n\n* 增大 R：变慢\n* 增大 C：变化",   # [1] would-be re-plan
    ]

    def __init__(self):
        self.calls = 0

    def chat_json(self, messages, schema_hint, **kwargs):
        idx = min(self.calls, len(self.ANSWERS) - 1)
        self.calls += 1
        return json.dumps({"reformatted_md": self.ANSWERS[idx]}, ensure_ascii=False)


def test_dry_run_writes_a_valid_plan_file(repo, nb_with_dirty_cells, tmp_path):
    plan_path = tmp_path / "myplan.json"
    rc = main(["--notebook", nb_with_dirty_cells, "--save-plan", str(plan_path)])

    assert rc == 0
    assert plan_path.exists()
    document = json.loads(plan_path.read_text(encoding="utf-8"))
    assert document["notebook_id"] == nb_with_dirty_cells
    assert document["use_llm"] is False
    assert isinstance(document.get("created_at"), str) and document["created_at"]
    assert isinstance(document["entries"], list) and document["entries"]
    for entry in document["entries"]:
        assert set(entry) == {
            "table_id", "row_id", "column_id", "before", "after", "source", "changed",
        }
    # dry-run wrote NOTHING to the DB
    assert _any_dirty_markers(repo, nb_with_dirty_cells)


def test_dry_run_default_plan_path_is_under_local_backfill_plans(
    repo, nb_with_dirty_cells, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    rc = main(["--notebook", nb_with_dirty_cells])

    assert rc == 0
    plans = list(
        (tmp_path / ".local" / "backfill_plans").glob(
            f"knowhow_md_{nb_with_dirty_cells}_*.json"
        )
    )
    assert len(plans) == 1


def test_use_llm_apply_without_plan_is_a_hard_error_and_writes_nothing(
    repo, nb_with_dirty_cells, capsys
):
    rc = main(["--notebook", nb_with_dirty_cells, "--use-llm", "--apply"])

    assert rc != 0
    msg = "".join(capsys.readouterr())
    # explains WHY (stochastic model) and shows the correct two-step usage.
    assert "--plan" in msg
    assert _any_dirty_markers(repo, nb_with_dirty_cells)  # wrote NOTHING


def test_apply_with_plan_writes_reviewed_after_verbatim(
    repo, nb_with_dirty_cells, tmp_path, monkeypatch
):
    client = _TwoAnswerReformatClient()
    _bind_reformat(repo, client)
    monkeypatch.setattr(bkmd, "SQLiteRepository", lambda *a, **k: repo)
    plan_path = tmp_path / "plan.json"
    dirty_row_id = _find_row_id(repo, nb_with_dirty_cells, DIRTY)
    method_col_id = _column_id(repo, nb_with_dirty_cells, "procedure")

    # dry-run --use-llm reviews the DIRTY cell as ANSWERS[0] and saves the plan.
    rc = main(["--notebook", nb_with_dirty_cells, "--use-llm", "--save-plan", str(plan_path)])
    assert rc == 0
    calls_after_plan = client.calls
    assert calls_after_plan >= 1
    document = json.loads(plan_path.read_text(encoding="utf-8"))
    dirty_after = next(
        e["after"] for e in document["entries"] if e["before"] == DIRTY and e["changed"]
    )
    assert dirty_after == _TwoAnswerReformatClient.ANSWERS[0]

    # apply the reviewed plan: it must write ANSWERS[0] verbatim, NOT re-plan
    # (which would call the stochastic client again and produce ANSWERS[1]).
    rc = main(["--notebook", nb_with_dirty_cells, "--use-llm", "--apply", "--plan", str(plan_path)])
    assert rc == 0
    assert client.calls == calls_after_plan  # apply made ZERO new LLM calls

    stored = _cell_text(repo, nb_with_dirty_cells, dirty_row_id, method_col_id)
    assert stored == _TwoAnswerReformatClient.ANSWERS[0]     # reviewed candidate landed
    assert stored != _TwoAnswerReformatClient.ANSWERS[1]     # not a fresh re-plan


def test_apply_with_plan_skips_cell_edited_after_review(repo, nb_two_dirty, tmp_path, capsys):
    plan_path = tmp_path / "plan.json"
    r1 = _find_row_id(repo, nb_two_dirty, DIRTY)
    r2 = _find_row_id(repo, nb_two_dirty, DIRTY_B)
    method_col = _column_id(repo, nb_two_dirty, "procedure")

    rc = main(["--notebook", nb_two_dirty, "--save-plan", str(plan_path)])
    assert rc == 0

    # a human edits r1's cell AFTER reviewing the plan (its stored content no
    # longer matches the plan's recorded `before`).
    repo.update_knowhow_cell(r1, method_col, "评审后手工改成别的内容")
    capsys.readouterr()  # clear buffered dry-run output

    rc = main(["--notebook", nb_two_dirty, "--apply", "--plan", str(plan_path)])
    assert rc == 0
    msg = "".join(capsys.readouterr())
    assert "内容在评审后已变化，跳过" in msg

    # r1 (moved target) was NOT overwritten by the plan's after.
    assert _cell_text(repo, nb_two_dirty, r1, method_col) == "评审后手工改成别的内容"
    # r2 (unchanged since review) WAS applied -- normalized, no raw idioms.
    r2_text = _cell_text(repo, nb_two_dirty, r2, method_col)
    assert r2_text is not None
    assert "•" not in r2_text and "\t" not in r2_text
    assert "**B. 其它**" in r2_text


def test_apply_rejects_plan_for_wrong_notebook(repo, nb_with_dirty_cells, tmp_path, capsys):
    plan_path = tmp_path / "plan.json"
    rc = main(["--notebook", nb_with_dirty_cells, "--save-plan", str(plan_path)])
    assert rc == 0

    rc = main(["--notebook", "nb-some-other-notebook", "--apply", "--plan", str(plan_path)])
    assert rc != 0
    msg = "".join(capsys.readouterr())
    assert "不一致" in msg
    # the real notebook's cells are untouched.
    assert _any_dirty_markers(repo, nb_with_dirty_cells)


# ---------------------------------------------------------------------------
# P1 (TOCTOU): the plan-apply moved-target check must be ATOMIC with the write.
# The old apply_reviewed_plan read the current cells OUTSIDE the transaction,
# compared to each entry's `before`, THEN called the plain bulk write -- a cell a
# live backend user edited BETWEEN that read and the write passed the stale
# comparison and got OVERWRITTEN, violating the "post-review edits are skipped"
# guarantee. The store now exposes update_knowhow_cells_bulk_guarded, which
# re-reads and compares INSIDE the one write transaction.
# ---------------------------------------------------------------------------


def test_bulk_guarded_writes_only_matching_cells_atomically(repo, nb_two_dirty):
    """Store-level compare-and-write: with a STALE expected_before for one cell
    (as if edited after the plan was reviewed), update_knowhow_cells_bulk_guarded
    must skip that cell and still write the other entry in the SAME call --
    re-reading the current content under the write lock, not trusting a caller
    pre-read."""
    r1 = _find_row_id(repo, nb_two_dirty, DIRTY)
    r2 = _find_row_id(repo, nb_two_dirty, DIRTY_B)
    method_col = _column_id(repo, nb_two_dirty, "procedure")
    t_id = repo.list_knowhow_tables(nb_two_dirty)[0]["id"]

    # A live backend user edits r1 AFTER its plan `before` (DIRTY) was captured,
    # so the guarded write's in-transaction re-read no longer matches the stale
    # expected_before and must refuse it.
    repo.update_knowhow_cell(r1, method_col, "评审后手工改成别的内容")
    seq_before = repo.list_knowhow_tables(nb_two_dirty)[0]["mutation_seq"]

    result = repo.update_knowhow_cells_bulk_guarded(nb_two_dirty, [
        (t_id, r1, method_col, DIRTY, "规整后的甲"),     # stale expected_before -> skip
        (t_id, r2, method_col, DIRTY_B, "规整后的乙"),   # current == expected -> write
    ])

    assert result["written"] == [(r2, method_col)]
    assert result["skipped"] == [(r1, method_col)]
    assert result.get("rejected", []) == []  # both belong to the notebook
    # r1 (moved target) NOT clobbered; r2 written.
    assert _cell_text(repo, nb_two_dirty, r1, method_col) == "评审后手工改成别的内容"
    assert _cell_text(repo, nb_two_dirty, r2, method_col) == "规整后的乙"
    # exactly one write -> exactly one mutation_seq bump (the written table only).
    assert repo.list_knowhow_tables(nb_two_dirty)[0]["mutation_seq"] == seq_before + 1


def test_bulk_guarded_all_skipped_neither_writes_nor_bumps(repo, nb_two_dirty):
    """A batch whose every entry's expected_before no longer matches writes
    nothing and does NOT bump the table's mutation_seq (a skip never counts as a
    change) -- and never fails the call."""
    r1 = _find_row_id(repo, nb_two_dirty, DIRTY)
    method_col = _column_id(repo, nb_two_dirty, "procedure")
    t_id = repo.list_knowhow_tables(nb_two_dirty)[0]["id"]
    seq_before = repo.list_knowhow_tables(nb_two_dirty)[0]["mutation_seq"]

    result = repo.update_knowhow_cells_bulk_guarded(
        nb_two_dirty, [(t_id, r1, method_col, "从未存在过的旧内容", "新内容")]
    )

    assert result == {"written": [], "skipped": [(r1, method_col)], "already_applied": [], "rejected": []}
    assert _cell_text(repo, nb_two_dirty, r1, method_col) == DIRTY  # untouched
    assert repo.list_knowhow_tables(nb_two_dirty)[0]["mutation_seq"] == seq_before


# ---------------------------------------------------------------------------
# F2 (code review): the guarded bulk writer must validate entry MEMBERSHIP
# inside the transaction. It previously trusted the caller's (row_id,
# column_id) pairs -- an entry whose ids belong to ANOTHER notebook/table
# passed the ``before`` compare and silently overwrote a foreign cell while
# bumping the caller's OWN table. The method now takes the expected
# notebook_id and, per entry, joins row/column to their owning table and
# verifies row.table == column.table == claimed table_id AND that table
# belongs to notebook_id; a mismatch is a THIRD outcome ("rejected"), never
# written.
# ---------------------------------------------------------------------------


@pytest.fixture
def nb_foreign(repo) -> str:
    """A SECOND, unrelated notebook (its own table + dirty row) -- the source
    of "foreign" row/column ids for the membership tests below."""
    nb_id = repo.create_notebook(
        NotebookCreate(name="foreign", purpose="p", primary_domain="d")
    ).id
    table_id = repo.create_knowhow_table(
        nb_id, "外部表", "",
        [{"name": "现象", "role": "anchor"}, {"name": "方法", "role": "procedure"}],
    )
    columns = {c["name"]: c["id"] for c in repo.get_knowhow_table(table_id)["columns"]}
    repo.add_knowhow_row(table_id, {columns["现象"]: "外部现象", columns["方法"]: DIRTY})
    return nb_id


def test_bulk_guarded_rejects_foreign_notebook_cell_and_still_writes_local(
    repo, nb_two_dirty, nb_foreign
):
    """An entry whose (row, column) live in ANOTHER notebook -- but whose
    ``expected_before`` matches that foreign cell's current content -- must be
    REJECTED (not written), the foreign cell left untouched, while a legitimate
    entry in the SAME call is still written. The caller claims its OWN table_id
    for the foreign cell (the exact "overwrite a foreign cell while bumping my
    table" attack)."""
    local_row = _find_row_id(repo, nb_two_dirty, DIRTY)
    local_col = _column_id(repo, nb_two_dirty, "procedure")
    local_table = repo.list_knowhow_tables(nb_two_dirty)[0]["id"]

    foreign_row = _find_row_id(repo, nb_foreign, DIRTY)
    foreign_col = _column_id(repo, nb_foreign, "procedure")

    result = repo.update_knowhow_cells_bulk_guarded(nb_two_dirty, [
        # foreign cell, but caller claims its own local_table id for it
        (local_table, foreign_row, foreign_col, DIRTY, "恶意覆盖外部格子"),
        (local_table, local_row, local_col, DIRTY, "合法的本地规整"),
    ])

    assert result["written"] == [(local_row, local_col)]
    assert result["rejected"] == [(foreign_row, foreign_col)]
    assert result["skipped"] == []
    # foreign cell untouched despite the matching expected_before
    assert _cell_text(repo, nb_foreign, foreign_row, foreign_col) == DIRTY
    # local cell written
    assert _cell_text(repo, nb_two_dirty, local_row, local_col) == "合法的本地规整"


def test_bulk_guarded_rejects_entry_claiming_foreign_table(repo, nb_two_dirty, nb_foreign):
    """An entry naming the FOREIGN table_id (whose notebook != the claimed
    notebook_id) is rejected even though row/column are internally consistent
    with that foreign table."""
    foreign_row = _find_row_id(repo, nb_foreign, DIRTY)
    foreign_col = _column_id(repo, nb_foreign, "procedure")
    foreign_table = repo.list_knowhow_tables(nb_foreign)[0]["id"]

    result = repo.update_knowhow_cells_bulk_guarded(
        nb_two_dirty,  # claim nb_two_dirty, but the table belongs to nb_foreign
        [(foreign_table, foreign_row, foreign_col, DIRTY, "越界写入")],
    )

    assert result["written"] == []
    assert result["rejected"] == [(foreign_row, foreign_col)]
    assert _cell_text(repo, nb_foreign, foreign_row, foreign_col) == DIRTY  # untouched


def test_bulk_guarded_rejects_row_column_table_mismatch(repo):
    """Row and column from DIFFERENT tables in the SAME notebook -> rejected
    (row.table != column.table)."""
    nb_id = repo.create_notebook(
        NotebookCreate(name="two-tables", purpose="p", primary_domain="d")
    ).id
    t1 = repo.create_knowhow_table(
        nb_id, "表一", "",
        [{"name": "现象", "role": "anchor"}, {"name": "方法", "role": "procedure"}],
    )
    t2 = repo.create_knowhow_table(
        nb_id, "表二", "",
        [{"name": "现象", "role": "anchor"}, {"name": "方法", "role": "procedure"}],
    )
    cols1 = {c["name"]: c["id"] for c in repo.get_knowhow_table(t1)["columns"]}
    cols2 = {c["name"]: c["id"] for c in repo.get_knowhow_table(t2)["columns"]}
    row1 = repo.add_knowhow_row(t1, {cols1["现象"]: "甲", cols1["方法"]: DIRTY})
    # row1 (table t1) paired with a column from t2 -> mismatch
    result = repo.update_knowhow_cells_bulk_guarded(
        nb_id, [(t1, row1, cols2["方法"], None, "不该写入")]
    )
    assert result["written"] == []
    assert result["rejected"] == [(row1, cols2["方法"])]


def test_apply_from_plan_skip_report_comes_from_transaction_return_value(
    repo, nb_two_dirty, tmp_path, monkeypatch, capsys
):
    """The CLI's skip report must come from update_knowhow_cells_bulk_guarded's
    RETURN VALUE (the atomic in-transaction compare), NOT from a CLI-level
    pre-read. r1's stored cell is UNCHANGED (still equals the plan's before), so a
    pre-read would NOT skip it; stub the guarded store method to report it skipped
    anyway and assert the CLI still reports it -- proving the CLI trusts the
    transaction result."""
    plan_path = tmp_path / "plan.json"
    r1 = _find_row_id(repo, nb_two_dirty, DIRTY)
    method_col = _column_id(repo, nb_two_dirty, "procedure")

    rc = main(["--notebook", nb_two_dirty, "--save-plan", str(plan_path)])
    assert rc == 0
    capsys.readouterr()  # clear buffered dry-run output

    def _fake_guarded(notebook_id, updates, **kwargs):
        skipped = [(row, col) for (_t, row, col, _b, _a) in updates if row == r1]
        written = [(row, col) for (_t, row, col, _b, _a) in updates if row != r1]
        return {"written": written, "skipped": skipped, "rejected": []}

    monkeypatch.setattr(repo, "update_knowhow_cells_bulk_guarded", _fake_guarded)
    monkeypatch.setattr(bkmd, "SQLiteRepository", lambda *a, **k: repo)

    rc = main(["--notebook", nb_two_dirty, "--apply", "--plan", str(plan_path)])
    assert rc == 0
    msg = "".join(capsys.readouterr())
    assert "内容在评审后已变化，跳过" in msg
    assert r1 in msg  # the skipped row (from the return value) is named in the report


# ---------------------------------------------------------------------------
# P2: the DEFAULT (rules-only) dry-run must be FULLY READ-ONLY -- it opens the
# DB mode=ro and NEVER constructs the write-capable SQLiteRepository (whose
# __init__ runs migrations + seed, both of which WRITE; crash recovery is no
# longer among them -- it moved to startup_warmup.run_startup). --use-llm /
# --apply still construct the repository and print a one-line DB-open notice.
# ---------------------------------------------------------------------------


def test_plan_backfill_readonly_matches_repo_plan(repo, nb_with_dirty_cells):
    """The read-only planner (mode=ro connection, no repository) produces a plan
    byte-for-byte identical to the live-repo rules-only plan -- they share
    _build_plan, parameterized only on the data source."""
    expected = plan_backfill(repo, nb_with_dirty_cells, use_llm=False)
    ro = bkmd.plan_backfill_readonly(Settings().sqlite_path, nb_with_dirty_cells)
    assert ro == expected


def test_default_dry_run_is_read_only_and_never_constructs_the_write_repo(
    repo, nb_with_dirty_cells, tmp_path, monkeypatch
):
    """Monkeypatch SQLiteRepository to blow up if instantiated: the default
    rules-only dry-run must complete WITHOUT constructing it, produce the same
    plan as the repo path, and write nothing."""
    expected = plan_backfill(repo, nb_with_dirty_cells, use_llm=False)

    class _Boom:
        def __init__(self, *a, **k):
            raise AssertionError("default dry-run must not construct SQLiteRepository")

    monkeypatch.setattr(bkmd, "SQLiteRepository", _Boom)

    plan_path = tmp_path / "plan.json"
    rc = main(["--notebook", nb_with_dirty_cells, "--save-plan", str(plan_path)])

    assert rc == 0
    document = json.loads(plan_path.read_text(encoding="utf-8"))
    assert document["entries"] == expected
    assert _any_dirty_markers(repo, nb_with_dirty_cells)  # wrote nothing


def test_default_dry_run_works_against_os_read_only_db(
    repo, nb_with_dirty_cells, tmp_path
):
    """OS-level proof: with the DB file itself chmod'd 0o444 (so a write-capable
    open / migration could not run), the default rules-only dry-run still
    completes and produces the same plan. Checkpoint+truncate the WAL first so a
    mode=ro open needs no writable sidecar."""
    expected = plan_backfill(repo, nb_with_dirty_cells, use_llm=False)
    db_path = pathlib.Path(Settings().sqlite_path)

    checkpoint = sqlite3.connect(str(db_path))
    try:
        checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        checkpoint.close()

    original_mode = db_path.stat().st_mode
    db_path.chmod(0o444)
    try:
        plan_path = tmp_path / "plan.json"
        rc = main(["--notebook", nb_with_dirty_cells, "--save-plan", str(plan_path)])
        assert rc == 0
        document = json.loads(plan_path.read_text(encoding="utf-8"))
        assert document["entries"] == expected
    finally:
        db_path.chmod(original_mode)


def test_main_use_llm_dry_run_uses_repo_and_prints_db_open_notice(
    repo, nb_with_dirty_cells, capsys, monkeypatch, tmp_path
):
    """--use-llm needs the system workload provider, so it DOES construct the
    repository -- and must print the one-line notice that opening the DB
    read-write may run pending migrations/recovery."""
    repo._runtime.models.chat_clients = {
        "knowhow_reformat": _FakeLLMClient(
            "**A. 考量**\n\n- 增大 R:变慢\n- 增大 C:变化"
        )
    }
    monkeypatch.setattr(bkmd, "SQLiteRepository", lambda *a, **k: repo)

    rc = main(
        ["--notebook", nb_with_dirty_cells, "--use-llm", "--save-plan", str(tmp_path / "plan.json")]
    )

    assert rc == 0
    msg = "".join(capsys.readouterr())
    assert "建议后端空闲时执行" in msg


def test_main_apply_prints_db_open_notice(repo, nb_with_dirty_cells, capsys, tmp_path):
    """--apply writes, so it constructs the repository and must print the same
    DB-open notice."""
    rc = _dry_run_then_apply(nb_with_dirty_cells, tmp_path / "plan.json")

    assert rc == 0
    msg = "".join(capsys.readouterr())
    assert "建议后端空闲时执行" in msg


# ---------------------------------------------------------------------------
# F4 (code review): the read-only dry-run URI must percent-encode the
# filesystem path. ``f"file:{path}?mode=ro"`` breaks for a path containing a
# literal ``?`` (SQLite parses everything after it as the URI query, so the
# real filename is truncated and ``mode=ro`` is silently dropped -> a
# WRITE-CAPABLE open of a DIFFERENT file, violating the dry-run read-only
# guarantee) or ``%`` (mis-decoded as a percent-escape). ``urllib.parse.quote``
# (safe="/" so path separators survive) fixes both.
# ---------------------------------------------------------------------------


def test_plan_backfill_readonly_handles_path_with_question_and_percent(
    repo, nb_with_dirty_cells, tmp_path
):
    """A DB file whose name contains ``?`` and ``%`` must still open correctly
    read-only through the dry-run planner and produce the same plan as a
    plain-named DB. Without percent-encoding, the ``?`` truncates the URI path
    (opening the wrong file / dropping mode=ro) and the call fails or plans the
    wrong database."""
    expected = plan_backfill(repo, nb_with_dirty_cells, use_llm=False)
    src = pathlib.Path(Settings().sqlite_path)

    # Checkpoint+truncate the WAL so a single-file copy carries all committed
    # rows (a mode=ro open of the copy needs no writable sidecar).
    checkpoint = sqlite3.connect(str(src))
    try:
        checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        checkpoint.close()

    weird = tmp_path / "db with ? and %2F and % literal.db"
    weird.write_bytes(src.read_bytes())

    ro = bkmd.plan_backfill_readonly(str(weird), nb_with_dirty_cells)
    assert ro == expected


def test_plan_backfill_readonly_uri_is_still_read_only_for_weird_path(
    repo, nb_with_dirty_cells, tmp_path
):
    """The percent-encoded URI must keep the connection READ-ONLY: a write
    attempted through it raises (proving ``mode=ro`` survived encoding rather
    than being dropped when the path contained ``?``)."""
    src = pathlib.Path(Settings().sqlite_path)
    checkpoint = sqlite3.connect(str(src))
    try:
        checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        checkpoint.close()

    weird = tmp_path / "weird ? name %.db"
    weird.write_bytes(src.read_bytes())

    from urllib.parse import quote

    conn = sqlite3.connect(f"file:{quote(str(weird))}?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE _should_fail (x)")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# System model service routing: --use-llm always requests the exact
# knowhow_reformat workload and never resolves a notebook-owner endpoint.
# ---------------------------------------------------------------------------


def test_use_llm_uses_system_workload_even_when_owner_unresolvable(
    repo, nb_with_dirty_cells, tmp_path, monkeypatch, capsys
):
    conn = sqlite3.connect(Settings().sqlite_path)
    try:
        conn.execute(
            "UPDATE notebooks SET created_by = ? WHERE id = ?",
            ("user-does-not-exist", nb_with_dirty_cells),
        )
        conn.commit()
    finally:
        conn.close()

    client = _FakeLLMClient("A. 考量\n\n- 增大 R：变慢\n- 增大 C：变化")
    repo._runtime.models.chat_clients = {"knowhow_reformat": client}
    monkeypatch.setattr(bkmd, "SQLiteRepository", lambda *a, **k: repo)
    plan_path = tmp_path / "p.json"

    rc = main([
        "--notebook", nb_with_dirty_cells,
        "--use-llm", "--save-plan", str(plan_path),
    ])

    assert rc == 0
    assert plan_path.exists()
    assert ("chat", "knowhow_reformat") in repo._runtime.models.calls
    assert "系统 knowhow_reformat workload" in "".join(capsys.readouterr())


# ---------------------------------------------------------------------------
# F4 (review) — a backfill RERUN after a partial apply must REPROJECT the rows
# that a prior run already WROTE but never reprojected. The apply writes commit
# in their own transaction; reprojection is a SEPARATE step afterward. If it
# throws (or the process exits) after the writes committed, the rows are left
# 'pending' -- and the NEXT SQLiteRepository construction's crash-recovery flips
# any lingering 'pending' row to 'failed' FOREVER. On rerun of the SAME reviewed
# plan every such cell now holds its AFTER value, so the guard's `before` compare
# no longer matches -- the OLD code lumped it into the moved-target `skipped`
# bucket, left `applied` empty, and reprojected NOTHING, so the stuck rows never
# recovered. Fix: the guard classifies a cell whose current == AFTER as a
# DISTINCT `already_applied` outcome (vs a genuine moved target, current != before
# AND != after), and the CLI reprojects tables with applied OR already_applied
# entries (reprojection is idempotent).
# ---------------------------------------------------------------------------


class _ThrowingProjector:
    """Stand-in KnowhowProjector whose project_table raises -- simulates a
    reprojection that fails AFTER the guarded writes already committed."""

    def project_table(self, table_id):
        raise RuntimeError("simulated reprojection failure after writes committed")


def test_bulk_guarded_already_applied_is_its_own_bucket(repo, nb_two_dirty):
    """Store-level: a cell whose CURRENT content already equals the update's AFTER
    (a prior apply wrote it, its reprojection never completed) is classified as a
    DISTINCT ``already_applied`` outcome -- NOT lumped with genuine moved-target
    ``skipped`` (current != before AND != after) -- writes nothing and bumps no
    mutation_seq. RED before the fix: no ``already_applied`` key; the cell landed
    in ``skipped``."""
    r1 = _find_row_id(repo, nb_two_dirty, DIRTY)
    method_col = _column_id(repo, nb_two_dirty, "procedure")
    t_id = repo.list_knowhow_tables(nb_two_dirty)[0]["id"]
    # the cell already holds the AFTER value (as a prior committed apply left it)
    repo.update_knowhow_cell(r1, method_col, "规整后的甲")
    seq_before = repo.list_knowhow_tables(nb_two_dirty)[0]["mutation_seq"]

    result = repo.update_knowhow_cells_bulk_guarded(nb_two_dirty, [
        (t_id, r1, method_col, DIRTY, "规整后的甲"),   # current == after -> already_applied
    ])

    assert result["written"] == []
    assert result["skipped"] == []                      # NOT a genuine moved target
    assert result["already_applied"] == [(r1, method_col)]
    assert result.get("rejected", []) == []
    assert _cell_text(repo, nb_two_dirty, r1, method_col) == "规整后的甲"  # untouched
    assert repo.list_knowhow_tables(nb_two_dirty)[0]["mutation_seq"] == seq_before  # no bump


def test_apply_reviewed_plan_reports_already_applied_separately(repo, nb_two_dirty):
    """apply_reviewed_plan returns a 4-tuple ``(applied, already_applied, skipped,
    rejected)``. An already-applied cell (current == after) is in ``already_applied``,
    NOT ``applied`` (no write) and NOT ``skipped`` (not a moved target). RED before
    the fix: apply_reviewed_plan returned a 3-tuple."""
    r1 = _find_row_id(repo, nb_two_dirty, DIRTY)
    method_col = _column_id(repo, nb_two_dirty, "procedure")
    plan = plan_backfill(repo, nb_two_dirty, use_llm=False)
    r1_entry = next(e for e in plan if e["row_id"] == r1 and e["changed"])
    # pre-apply exactly r1's own reviewed AFTER, as a prior committed run would
    repo.update_knowhow_cell(r1, method_col, r1_entry["after"])

    applied, already_applied, skipped, rejected = apply_reviewed_plan(repo, nb_two_dirty, plan)

    applied_cells = {(a["row_id"], a["column_id"]) for a in applied}
    already_cells = {(a["row_id"], a["column_id"]) for a in already_applied}
    assert (r1, method_col) in already_cells
    assert (r1, method_col) not in applied_cells
    assert all(s["row_id"] != r1 for s in skipped)
    assert rejected == []


def test_rerun_after_reproject_failure_reprojects_already_applied_rows(
    repo, nb_with_dirty_cells, monkeypatch
):
    """THE F4 incident, end-to-end at the function level: apply commits its writes
    (rows -> 'pending'); reprojection then THROWS (a crash/rerun after the writes
    committed). On RERUN of the SAME plan every written cell is now at its AFTER
    value -> classified ``already_applied``, so ``applied`` is EMPTY. The old code
    reprojected only ``applied`` -> the table was NEVER reprojected -> its rows
    stayed 'pending' (and a later SQLiteRepository construction flips them to
    'failed'). The fix reprojects tables with applied OR already_applied entries,
    settling the rows to 'synced' with no new write."""
    dirty_row_id = _find_row_id(repo, nb_with_dirty_cells, DIRTY)
    plan = plan_backfill(repo, nb_with_dirty_cells, use_llm=False)

    # First apply: writes commit (rows -> 'pending') ...
    applied, already, skipped, rejected = apply_reviewed_plan(repo, nb_with_dirty_cells, plan)
    assert applied and not already
    assert _row_status(repo, nb_with_dirty_cells, dirty_row_id) == "pending"
    # ... then reprojection THROWS after the writes already committed.
    monkeypatch.setattr(bkmd, "build_projector", lambda _repo: _ThrowingProjector())
    with pytest.raises(RuntimeError):
        reproject_changed_tables(repo, applied)
    monkeypatch.undo()
    assert _row_status(repo, nb_with_dirty_cells, dirty_row_id) == "pending"  # left stuck

    # Rerun the SAME reviewed plan against the now-partially-applied DB.
    applied2, already2, skipped2, rejected2 = apply_reviewed_plan(repo, nb_with_dirty_cells, plan)
    assert applied2 == []           # zero new writes -- everything is already at AFTER
    assert skipped2 == []           # NOT moved targets
    assert already2                 # the already-applied cell(s)

    # The fix: reproject tables from applied OR already_applied (idempotent).
    reprojected = reproject_changed_tables(repo, applied2 + already2)
    assert len(reprojected) == 1
    assert _row_status(repo, nb_with_dirty_cells, dirty_row_id) == "synced"


def test_genuine_moved_target_is_not_reprojected(repo, nb_two_dirty):
    """A genuine moved target (current != before AND != after -- a live edit after
    review) stays ``skipped``, never ``already_applied``, and contributes NO table
    to the reprojection set when it is the only non-write entry. Guards against the
    fix over-reprojecting moved cells."""
    r1 = _find_row_id(repo, nb_two_dirty, DIRTY)
    method_col = _column_id(repo, nb_two_dirty, "procedure")
    plan = plan_backfill(repo, nb_two_dirty, use_llm=False)
    # keep ONLY r1's entry, then move it (current != before, != after)
    r1_plan = [e for e in plan if e["row_id"] == r1]
    repo.update_knowhow_cell(r1, method_col, "评审后又改成第三种内容")

    applied, already_applied, skipped, rejected = apply_reviewed_plan(repo, nb_two_dirty, r1_plan)

    assert applied == []
    assert already_applied == []
    assert any(s["row_id"] == r1 and s["reason"] == "moved" for s in skipped)
    # reprojection set (applied + already_applied) is empty -> nothing reprojected
    assert reproject_changed_tables(repo, applied + already_applied) == []


def test_main_apply_rerun_after_reproject_crash_settles_rows_synced(
    repo, nb_with_dirty_cells, tmp_path, monkeypatch
):
    """CLI-level F4: a first --apply whose reprojection CRASHES after the writes
    commit leaves rows 'pending'; a SECOND --apply of the same plan (reprojection
    now healthy) must settle the rows to 'synced' even though it writes nothing (all
    cells already at AFTER)."""
    dirty_row_id = _find_row_id(repo, nb_with_dirty_cells, DIRTY)
    plan_path = tmp_path / "plan.json"
    monkeypatch.setattr(bkmd, "SQLiteRepository", lambda *a, **k: repo)

    rc = main(["--notebook", nb_with_dirty_cells, "--save-plan", str(plan_path)])
    assert rc == 0

    # first apply: make reprojection crash AFTER the guarded writes commit
    monkeypatch.setattr(bkmd, "build_projector", lambda _repo: _ThrowingProjector())
    with pytest.raises(RuntimeError):
        main(["--notebook", nb_with_dirty_cells, "--apply", "--plan", str(plan_path)])
    monkeypatch.undo()
    monkeypatch.setattr(bkmd, "SQLiteRepository", lambda *a, **k: repo)
    assert _row_status(repo, nb_with_dirty_cells, dirty_row_id) == "pending"  # stuck

    # second apply (healthy reprojection): zero writes, rows settle 'synced'
    rc = main(["--notebook", nb_with_dirty_cells, "--apply", "--plan", str(plan_path)])
    assert rc == 0
    assert _row_status(repo, nb_with_dirty_cells, dirty_row_id) == "synced"


# ---------------------------------------------------------------------------
# F3 (this review) — the DEFAULT plan path must be collision-free. The old
# `%Y%m%dT%H%M%SZ`-only filename made two dry-runs within the SAME wall-clock
# second resolve to the IDENTICAL default path, and `write_text` silently
# clobbered the first (reviewed) plan. Fix: the default filename gains
# microseconds + pid, and `save_plan` opens with O_EXCL (`x` mode) and retries
# with a bounded `-1`/`-2`... suffix on FileExistsError -- never overwriting.
# (Appended at EOF so the additions don't shift line-pinned call sites that the
# repository surface-manifest guards freeze earlier in this file.)
# ---------------------------------------------------------------------------


def test_default_plan_path_distinct_within_same_second():
    from datetime import datetime, timezone
    nb = "nb-collide"
    t0 = datetime(2026, 7, 19, 12, 0, 0, 0, tzinfo=timezone.utc)
    t1 = t0.replace(microsecond=1)                 # same second, next microsecond
    # microseconds are in the filename, so two same-second dry-runs differ.
    assert bkmd._default_plan_path(nb, t0) != bkmd._default_plan_path(nb, t1)


def test_save_plan_o_excl_disambiguates_identical_default_path(tmp_path, monkeypatch):
    # even if two plans map to the IDENTICAL default path (same notebook, same
    # created_at down to the microsecond, same pid), save_plan's O_EXCL + `-N`
    # suffix gives DISTINCT files and never overwrites the reviewed first plan.
    monkeypatch.chdir(tmp_path)
    from datetime import datetime, timezone
    created = datetime(2026, 7, 19, 12, 0, 0, 123456, tzinfo=timezone.utc)
    plan = [{"table_id": "t", "row_id": "r", "column_id": "c",
             "before": "x", "after": "y", "source": "rule", "changed": True}]
    p1 = bkmd.save_plan(plan, "nb-x", False, created)
    p2 = bkmd.save_plan(plan, "nb-x", False, created)   # same default-path target
    assert p1 != p2
    assert p1.exists() and p2.exists()
    # the first (reviewed) plan file is intact, not clobbered.
    assert bkmd.load_plan(str(p1))["notebook_id"] == "nb-x"
