"""``kg rewrite-descriptions`` on SQLite (PostgreSQL twin:
``tests/postgres/test_concept_description_rewrite_pg.py``; the scenarios are
shared in ``tests/concept_description_rewrite_cases.py``).

Mutation anchors (each turns the named test red):

* plan reads ignore the notebook (drop ``cc.notebook_id`` from
  ``concept_description_member_page``) -> ``test_other_notebooks_are_untouched``;
* the plan ignores what is staged (every input pending again) ->
  ``test_resume_skips_what_was_already_rewritten`` and the interrupt case;
* a failed call aborts the run (re-raise in ``_generate``) ->
  ``test_failed_inputs_are_fail_soft``.
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests import concept_description_rewrite_cases as cases
from tests.model_testkit import RecordingModelProvider, bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repository = SQLiteRepository(Settings(), model_provider=RecordingModelProvider())
    bind_all_embedding_clients(repository, FakeEmbedder(dim=16))
    try:
        yield repository
    finally:
        # By the class: the CLI cases below replace the instance's ``close``
        # so the command's own close leaves the fixture's repository open.
        SQLiteRepository.close(repository)


def test_dry_run_counts_without_calling_or_writing(repo):
    cases.assert_dry_run_counts_without_calling_or_writing(repo)


def test_rewrites_every_description_through_the_write_contract(repo):
    cases.assert_rewrites_every_description_through_the_write_contract(repo)


def test_resume_skips_what_was_already_rewritten(repo):
    cases.assert_resume_skips_what_was_already_rewritten(repo)


def test_interrupt_keeps_finished_results(repo):
    cases.assert_interrupt_keeps_finished_results(repo)


def test_failed_inputs_are_fail_soft(repo, caplog):
    cases.assert_failed_inputs_are_fail_soft(repo, caplog)


def test_other_notebooks_are_untouched(repo):
    cases.assert_other_notebooks_are_untouched(repo)


def test_a_rebuild_in_flight_defers_the_publish(repo):
    cases.assert_a_rebuild_in_flight_defers_the_publish(repo)


def test_an_append_between_plan_and_publish_is_not_overwritten(repo):
    cases.assert_an_append_between_plan_and_publish_is_not_overwritten(repo)


def test_a_rebuild_published_mid_run_is_replanned(repo):
    cases.assert_a_rebuild_published_mid_run_is_replanned(repo)


def test_unavailable_without_a_bound_model(repo):
    cases.assert_unavailable_without_a_bound_model(repo)


# ------------------------------------------------------------------ the CLI
def _cli(repo, monkeypatch, *argv):
    """Drive the command against the fixture's repository (its own fake
    models) through the real schema-guest opener."""
    from app.scripts import rewrite_concept_descriptions
    from app.services import maintenance_cli

    monkeypatch.setattr(repo, "close", lambda: None)
    monkeypatch.setattr(maintenance_cli, "create_repository", lambda _settings: repo)
    return rewrite_concept_descriptions.main(list(argv))


def test_cli_rewrites_resumes_and_reports_exit_status(repo, monkeypatch, capsys):
    nb = cases.build_notebook(repo)
    new = cases.DescModel("model-new")
    cases.bind_chat_client(repo, cases.WORKLOAD, new)

    assert _cli(repo, monkeypatch, nb, "--dry-run") == 0
    assert '"model_calls": 2' in capsys.readouterr().out and new.calls == 0
    assert _cli(repo, monkeypatch, nb, "--limit", "1") == 1  # one input left
    assert _cli(repo, monkeypatch, "--all") == 0
    out = capsys.readouterr().out
    assert "rewrite-descriptions done" in out and '"incomplete_notebooks": []' in out
    assert new.calls == 2
    assert cases._texts(repo, nb)["cascode"] == "model-new::cascode"


def test_cli_interrupt_exits_130_at_once_and_the_rerun_continues(repo, monkeypatch, capsys):
    """Ctrl-C leaves through ``_hard_exit`` (``os._exit``): neither the
    repository close nor interpreter shutdown, both of which wait for model
    calls still in flight, runs first."""
    from app.scripts import rewrite_concept_descriptions

    exits = []

    def hard_exit(code):
        exits.append((code, closed[:]))
        raise SystemExit(code)

    closed = []
    monkeypatch.setattr(rewrite_concept_descriptions, "_hard_exit", hard_exit)
    nb = cases.build_notebook(repo)
    cases.bind_chat_client(repo, cases.WORKLOAD, cases.DescModel("model-new", interrupt_after=1))
    from app.services import maintenance_cli

    monkeypatch.setattr(repo, "close", lambda: closed.append(True))
    monkeypatch.setattr(maintenance_cli, "create_repository", lambda _settings: repo)
    with pytest.raises(SystemExit):
        rewrite_concept_descriptions.main([nb])
    assert exits == [(130, [])]  # before the repository close
    assert "run the same command again" in capsys.readouterr().err
    resumed = cases.DescModel("model-new")
    cases.bind_chat_client(repo, cases.WORKLOAD, resumed)
    assert _cli(repo, monkeypatch, nb) == 0
    assert resumed.calls == 1


@pytest.mark.parametrize("argv", [(), ("nb-x", "--all"), ("nb-x", "--limit", "0")])
def test_cli_usage_errors_exit_2(argv, capsys):
    from app.scripts import rewrite_concept_descriptions

    with pytest.raises(SystemExit) as caught:
        rewrite_concept_descriptions.main(list(argv))
    assert caught.value.code == 2


def test_cli_unknown_notebook_and_unbound_model_exit_2(repo, monkeypatch, capsys):
    from tests.model_testkit import UNCONFIGURED_CHAT_CLIENT

    assert _cli(repo, monkeypatch, "nb-missing", "--dry-run") == 2
    assert "unknown notebook" in capsys.readouterr().err
    nb = cases.build_notebook(repo)
    cases.bind_chat_client(repo, cases.WORKLOAD, UNCONFIGURED_CHAT_CLIENT)
    assert _cli(repo, monkeypatch, nb, "--dry-run") == 2
    assert "kg_concept_description" in capsys.readouterr().err


def test_rewrite_computes_the_rebuild_signature_on_messy_quotes(repo):
    """The command's input normalization (app.domain.concept_description_rewrite)
    against the rebuild description stage's own inline loop: padded,
    duplicated, empty and more than eight quotes across members. The rewrite
    keeps every row's stored signature and the next rebuild reuses its text."""
    from app.models.schemas import NotebookCreate

    def concept(local_id, name, spans):
        return {
            "local_id": local_id, "object_type": "concept",
            "payload": {"name": name, "section_path": "1"},
            "evidence": [{"source_id": "s", "source_title": "D", "element_id": f"e{i}",
                          "element_type": "p", "location_label": "1",
                          "quoted_span": span, "confidence": 1.0}
                         for i, span in enumerate(spans)],
        }

    nb = repo.create_notebook(NotebookCreate(name="messy")).id
    repo.store_kg(nb, "s1", [concept("a", "Gate oxide", [
        "  z last  ", "", "b two", "b two", "a one", "   "])], [])
    repo.store_kg(nb, "s2", [concept("b", "gate oxide", [
        "y", "x", "w", "v", "u", "a one ", "\tc three"])], [])
    old = cases.DescModel("model-old")
    cases.bind_chat_client(repo, cases.WORKLOAD, old)
    repo._runtime.knowledge_lifecycle.rebuild_unified_kg(nb)
    assert old.calls == 1
    before = cases.descriptions(repo, nb)
    (name,) = [n for n, pairs in before.items() if any(d for d, _s in pairs)]
    sig = {s for _d, s in before[name]}

    new = cases.DescModel("model-new")
    cases.bind_chat_client(repo, cases.WORKLOAD, new)
    report = cases.rewrite(repo, nb)
    assert (report.status, report.rewritten, report.moved) == ("done", 1, 0)
    assert {s for _d, s in cases.descriptions(repo, nb)[name]} == sig
    repo._runtime.knowledge_lifecycle.rebuild_unified_kg(nb, force=True)
    assert new.calls == 1
    assert cases._texts(repo, nb)[name] == f"model-new::{name}"


# ------------------------------------------------- review follow-ups (2026-10-28)
def test_pages_are_bounded_by_rows_and_drop_the_rebuild_checkpoint_once(repo, monkeypatch):
    cases.assert_pages_are_bounded_by_rows_and_drop_the_rebuild_checkpoint_once(
        repo, monkeypatch)


def test_hub_canonicals_are_written_in_batches(repo, monkeypatch):
    cases.assert_hub_canonicals_are_written_in_batches(repo, monkeypatch)


def test_a_hub_stops_when_the_state_moves_after_its_verification(repo, monkeypatch):
    cases.assert_a_hub_stops_when_the_state_moves_after_its_verification(repo, monkeypatch)


def test_kg_delete_drops_the_staged_texts_only(repo):
    cases.assert_kg_delete_drops_the_staged_texts_only(repo)


def test_a_completed_notebook_is_answered_from_its_record(repo):
    cases.assert_a_completed_notebook_is_answered_from_its_record(repo)


def test_an_interrupt_does_not_wait_for_calls_in_flight(repo):
    cases.assert_an_interrupt_does_not_wait_for_calls_in_flight(repo)


def test_cli_a_database_error_ends_one_notebook_and_the_run_goes_on(repo, monkeypatch, capsys):
    """A lock / statement timeout or a deadlock inside one notebook: that
    notebook reports ``error`` with the class name, the next one runs, the
    summary line still prints, and the exit status is the incomplete one."""
    first = cases.build_notebook(repo, "first", prefix="a-")
    second = cases.build_notebook(repo, "second", prefix="b-")
    cases.bind_chat_client(repo, cases.WORKLOAD, cases.DescModel("model-new"))
    lifecycle = repo._runtime.knowledge_lifecycle
    real = lifecycle.rewrite_concept_descriptions

    class LockNotAvailable(Exception):
        pass

    def flaky(notebook_id, **kwargs):
        if notebook_id == first:
            raise LockNotAvailable("canceling statement due to lock timeout on K-mosfet")
        return real(notebook_id, **kwargs)

    monkeypatch.setattr(lifecycle, "rewrite_concept_descriptions", flaky)
    assert _cli(repo, monkeypatch, "--all") == 1
    out = capsys.readouterr().out
    assert f'"notebook_id": "{first}", "status": "error", "error": "LockNotAvailable"' in out
    assert "lock timeout" not in out and "mosfet" not in out.lower()
    assert "rewrite-descriptions done" in out and f'"incomplete_notebooks": ["{first}"]' in out
    assert cases._texts(repo, second)["MOSFET"] == "model-new::MOSFET"


def test_a_hub_whose_input_changed_is_not_overwritten(repo, monkeypatch):
    cases.assert_a_hub_whose_input_changed_is_not_overwritten(repo, monkeypatch)


def test_no_completion_record_after_a_write_during_the_run(repo):
    cases.assert_no_completion_record_after_a_write_during_the_run(repo)


# ---------------------------------------------- review follow-ups, round 2
def test_a_reparse_without_a_bump_voids_the_record(repo):
    cases.assert_a_reparse_without_a_bump_voids_the_record(repo)


def test_another_model_never_answers_from_the_record(repo):
    cases.assert_another_model_never_answers_from_the_record(repo)


def test_a_graph_delete_during_generation_leaves_no_staged_texts(repo, monkeypatch):
    cases.assert_a_graph_delete_during_generation_leaves_no_staged_texts(repo, monkeypatch)


def test_rows_that_raced_a_graph_delete_are_pruned_at_the_end(repo, monkeypatch):
    cases.assert_rows_that_raced_a_graph_delete_are_pruned_at_the_end(repo, monkeypatch)


def test_an_interrupt_stages_results_already_queued(repo):
    cases.assert_an_interrupt_stages_results_already_queued(repo)


def test_the_record_is_read_by_key(repo, monkeypatch):
    cases.assert_the_record_is_read_by_key(repo, monkeypatch)


def test_a_page_is_atomic_against_another_process_on_sqlite(repo, monkeypatch, tmp_path):
    """Another process (its own connection to the file) resets the claim and
    moves the reset epoch -- what a knowledge-graph delete does -- right after
    a publish page read the published generation. Either that write has to
    wait for the page (the page's IMMEDIATE transaction holds the write lock),
    or, if it got in, the page must not write a single row over it."""
    import sqlite3

    nb = cases.build_notebook(repo)
    store = repo._runtime.knowledge_lifecycle.unified_kg
    real_lock = store.lock_published_concept_clusters
    outside = []

    def lock_then_outside_write(db, notebook_id):
        result = real_lock(db, notebook_id)
        if not outside:
            other = sqlite3.connect(str(tmp_path / "t.db"), timeout=0.2)
            try:
                other.execute(
                    "UPDATE unified_kg_state SET derived_building_generation=0, "
                    "derived_building_claimed_at=NULL, kg_reset_epoch=kg_reset_epoch+1 "
                    "WHERE notebook_id=?", (notebook_id,))
                other.commit()
                outside.append("committed")
            except sqlite3.OperationalError:
                outside.append("refused")
            finally:
                other.close()
        return result

    monkeypatch.setattr(store, "lock_published_concept_clusters", lock_then_outside_write)
    cases.bind_chat_client(repo, cases.WORKLOAD, cases.DescModel("model-new"))
    cases.rewrite(repo, nb)
    texts = cases._texts(repo, nb)
    if outside == ["committed"]:
        assert texts["MOSFET"] == "model-old::MOSFET" and texts["cascode"] == "model-old::cascode"
    else:
        assert outside == ["refused"]
        assert texts["MOSFET"] == "model-new::MOSFET"


def test_cli_calls_of_a_failed_notebook_are_charged_to_the_limit(repo, monkeypatch, capsys):
    """codex #1030 P2. Mutation anchor: drop the charge in the CLI's error
    path and the second notebook gets the whole budget again (red)."""
    cases.assert_calls_of_a_failed_notebook_are_charged_to_the_limit(repo, monkeypatch, capsys)
