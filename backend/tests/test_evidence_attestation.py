"""PR-D D1 -- the producer-side attestation helpers.

``attest_read`` / ``attest_pointers`` publish ONLY through the installed run
plan's ``on_evidence`` (no second mechanism), cost nothing without a plan, and
fail closed: a pointer read that fails is STATED unreadable (``None``) rather
than left absent. The consumer's first-real-snapshot-wins merge is exercised
through them here as well, because that is the pairing the rule exists for.
"""
from __future__ import annotations

from contextvars import copy_context
import threading

import pytest

from app.domain.evidence_fingerprint import element_text_sha
from app.repositories.read_budget import current_read_budget
from app.services.cancellation import AskCancelled
from app.services.evidence_attestation import (
    DEAD, LIVE, UNKNOWN, attest_pointers, attest_read, evidence_attestation_seat,
)
from app.services.federated_run import FederatedRunPlan, federated_run_plan
from app.services.global_ask import _RunState


class _Reader:
    """``GlobalAskEvidenceReaderPort`` over a dict, recording every read."""

    def __init__(self, rows=None, error=None):
        self.rows = dict(rows or {})
        self.error = error
        self.reads: list = []
        self.budgets: list = []

    def evidence_fingerprints(self, element_ids):
        self.reads.append(tuple(element_ids))
        self.budgets.append(current_read_budget())
        if self.error is not None:
            raise self.error
        return {key: self.rows[key] for key in element_ids if key in self.rows}


def _plan(published, *, cancel=None, timeout=7.0):
    return FederatedRunPlan(
        phase_timeout_seconds=30.0, notebook_timeout_seconds=timeout,
        executor=None, window=lambda: 1, cancel=cancel,
        on_library=lambda *_: None, on_evidence=published.append,
    )


def test_without_a_plan_nothing_is_read_or_published():
    reader = _Reader({"e-1": ("s", "fp")})
    with evidence_attestation_seat(reader):
        attest_read("document_overview", {"e-1": ("s", "text")})
        assert attest_pointers("kg_objects", ["e-1"]) == {}
    assert reader.reads == []


def test_attest_read_hashes_in_process_and_publishes_once():
    published: list = []
    events: list = []
    reader = _Reader()
    with federated_run_plan(_plan(published)), evidence_attestation_seat(
        reader, emit=events.append,
    ):
        attest_read("collection_enumeration", {
            "e-1": ("s-1", "全文 🔋\r\n"), "e-2": ("s-1", None),
            "": ("s-1", "no id"), "e-3": ("", "no source"),
        })
    assert reader.reads == []
    assert published == [{
        "e-1": ("s-1", element_text_sha("全文 🔋\r\n")),
        "e-2": ("s-1", element_text_sha("")),
    }]
    assert events == [{
        "kind": "producer_evidence_attested", "producer": "collection_enumeration",
        "method": "read", "elements": 2,
    }]


def test_attest_pointers_reads_once_publishes_live_and_reports_dead():
    published: list = []
    events: list = []
    reader = _Reader({"e-live": ("s-1", "fp-live")})
    with federated_run_plan(_plan(published, timeout=7.0)), evidence_attestation_seat(
        reader, emit=events.append,
    ):
        states = attest_pointers("kg_objects", ["e-live", "e-dead", "e-live", ""])
    assert states == {"e-live": LIVE, "e-dead": DEAD}
    assert reader.reads == [("e-live", "e-dead")]
    # Bounded by the run's per-library read budget, polled on its cancel token.
    [budget] = reader.budgets
    assert budget is not None and budget.cancel_event is None
    # A dead pointer publishes NOTHING: there is no text whose change could be
    # detected, and a pretend snapshot would call it deleted during the answer.
    assert published == [{"e-live": ("s-1", "fp-live")}]
    assert events == [{
        "kind": "producer_evidence_attested", "producer": "kg_objects",
        "method": "pointers", "elements": 2, "live": 1, "dead": 1,
    }]


def test_attest_pointers_is_memoised_for_the_run():
    published: list = []
    reader = _Reader({"e-1": ("s", "a"), "e-2": ("s", "b")})
    with federated_run_plan(_plan(published)), evidence_attestation_seat(reader):
        attest_pointers("follow_chain", ["e-1", "e-gone"])
        states = attest_pointers("follow_chain", ["e-1", "e-gone", "e-2"])
    assert states == {"e-1": LIVE, "e-gone": DEAD, "e-2": LIVE}
    assert reader.reads == [("e-1", "e-gone"), ("e-2",)]


def test_a_failed_pointer_read_is_stated_unreadable_and_retried():
    published: list = []
    events: list = []
    reader = _Reader({"e-1": ("s", "a")}, error=RuntimeError("secret sql text"))
    with federated_run_plan(_plan(published)), evidence_attestation_seat(
        reader, emit=events.append,
    ):
        assert attest_pointers("table_analysis", ["e-1"]) == {"e-1": UNKNOWN}
        reader.error = None
        assert attest_pointers("table_analysis", ["e-1"]) == {"e-1": LIVE}
    assert published == [{"e-1": None}, {"e-1": ("s", "a")}]
    assert events[0] == {
        "kind": "producer_evidence_unavailable", "producer": "table_analysis",
        "reason": "read_failed", "elements": 1, "error_type": "RuntimeError",
    }
    assert "secret" not in repr(events)


def test_cancellation_propagates_out_of_a_pointer_read():
    published: list = []
    reader = _Reader(error=AskCancelled())
    with federated_run_plan(_plan(published)), evidence_attestation_seat(reader):
        with pytest.raises(AskCancelled):
            attest_pointers("kg_objects", ["e-1"])
    assert published == []


def test_a_plan_without_a_seat_refuses_by_name():
    published: list = []
    with federated_run_plan(_plan(published)):
        assert attest_pointers("kg_objects", ["e-1"]) == {"e-1": UNKNOWN}
    assert published == [{"e-1": None}]


def test_producer_codes_never_carry_prose_into_telemetry():
    published: list = []
    events: list = []
    with federated_run_plan(_plan(published)), evidence_attestation_seat(
        _Reader(), emit=events.append,
    ):
        attest_read("用户的问题 text", {"e-1": ("s", "t")})
    assert events[0]["producer"] == "unknown"


def test_the_seat_reaches_a_worker_thread_through_copy_context():
    published: list = []
    reader = _Reader({"e-1": ("s", "a")})
    seen: list = []
    with federated_run_plan(_plan(published)), evidence_attestation_seat(reader):
        context = copy_context()
        worker = threading.Thread(target=lambda: seen.append(
            context.run(attest_pointers, "kg_objects", ["e-1"])
        ))
        worker.start()
        worker.join(5)
    assert seen == [{"e-1": LIVE}]


def test_nesting_a_seat_is_refused():
    with evidence_attestation_seat(_Reader()):
        with pytest.raises(ValueError):
            with evidence_attestation_seat(_Reader()):
                pass


def test_a_later_pointer_read_cannot_overwrite_what_a_reader_attested():
    """First real snapshot wins in the consumer: a producer that READ the text
    registers it; a later pointer read of the same id -- taken after an edit --
    must not replace it, or the terminal check compares new text with itself."""
    state = _RunState(("nb",))
    plan = FederatedRunPlan(
        phase_timeout_seconds=30.0, notebook_timeout_seconds=5.0, executor=None,
        window=lambda: 1, cancel=None, on_library=lambda *_: None,
        on_evidence=state.record_evidence,
    )
    reader = _Reader({"e-1": ("s", element_text_sha("edited text"))})
    with federated_run_plan(plan), evidence_attestation_seat(reader):
        attest_read("document_overview", {"e-1": ("s", "original text")})
        attest_pointers("kg_objects", ["e-1"])
    assert state.evidence == {"e-1": ("s", element_text_sha("original text"))}


def test_a_real_snapshot_replaces_an_earlier_unreadable_one():
    state = _RunState(("nb",))
    state.record_evidence({"e-1": None})
    state.record_evidence({"e-1": ("s", "fp")})
    state.record_evidence({"e-1": None})
    state.record_evidence({"e-1": ("s", "later")})
    assert state.evidence == {"e-1": ("s", "fp")}


# ---------------------------------------------------------------------------
# A blind pointer read never launders a declared failure (fix round item 1)
# ---------------------------------------------------------------------------

def _state_plan():
    state = _RunState(("nb",))
    plan = FederatedRunPlan(
        phase_timeout_seconds=30.0, notebook_timeout_seconds=5.0, executor=None,
        window=lambda: 1, cancel=None, on_library=lambda *_: None,
        on_evidence=state.record_evidence,
    )
    return state, plan


def test_a_pointer_snapshot_is_published_as_a_pointer_snapshot():
    from app.services.federated_run import PointerSnapshot

    published: list = []
    with federated_run_plan(_plan(published)), evidence_attestation_seat(
        _Reader({"e-1": ("s", "fp")}),
    ):
        attest_pointers("kg_objects", ["e-1"])
    assert isinstance(published[0]["e-1"], PointerSnapshot)
    assert published[0]["e-1"] == ("s", "fp")


def test_a_pointer_read_does_not_replace_a_declared_none():
    """Federated channel: passage text changed under the run -> ``None``. A
    later blind pointer read sees the NEW text; it must not replace the None,
    or the terminal check compares new with new and passes."""
    state, plan = _state_plan()
    state.record_evidence({"e-1": None})
    with federated_run_plan(plan), evidence_attestation_seat(
        _Reader({"e-1": ("s", element_text_sha("new text"))}),
    ):
        assert attest_pointers("kg_objects", ["e-1"]) == {"e-1": LIVE}
    assert state.evidence == {"e-1": None}


def test_pointer_first_then_a_read_based_none_keeps_the_pointer_snapshot():
    state, plan = _state_plan()
    with federated_run_plan(plan), evidence_attestation_seat(
        _Reader({"e-1": ("s", "fp-pointer")}),
    ):
        attest_pointers("kg_objects", ["e-1"])
    state.record_evidence({"e-1": None})
    assert state.evidence == {"e-1": ("s", "fp-pointer")}


def test_pointer_first_then_a_read_based_snapshot_keeps_the_first():
    """First real snapshot wins whichever kind it is: a later read-based
    snapshot with a different digest means the text changed during the run,
    which the terminal check must still see."""
    state, plan = _state_plan()
    with federated_run_plan(plan), evidence_attestation_seat(
        _Reader({"e-1": ("s", "fp-pointer")}),
    ):
        attest_pointers("kg_objects", ["e-1"])
    state.record_evidence({"e-1": ("s", "fp-read")})
    assert state.evidence == {"e-1": ("s", "fp-pointer")}


def test_a_read_based_snapshot_still_replaces_a_declared_none():
    state, plan = _state_plan()
    state.record_evidence({"e-1": None})
    with federated_run_plan(plan), evidence_attestation_seat(_Reader()):
        attest_read("document_overview", {"e-1": ("s", "text")})
    assert state.evidence == {"e-1": ("s", element_text_sha("text"))}


def test_a_second_read_based_snapshot_does_not_replace_the_first():
    state, _plan_ = _state_plan()
    state.record_evidence({"e-1": ("s", "listing")})
    state.record_evidence({"e-1": ("s", "minting")})
    assert state.evidence == {"e-1": ("s", "listing")}


def test_a_cancelled_run_propagates_out_of_the_pointer_read():
    """The read budget polls the run's cancel token and surfaces as its own
    timeout; that is the run stopping, not a failed read, so it raises."""
    from app.repositories.read_budget import ReadBudgetExceeded

    cancel = threading.Event()
    published: list = []

    class _Cancelling(_Reader):
        def evidence_fingerprints(self, element_ids):
            cancel.set()
            raise ReadBudgetExceeded("read budget exhausted")

    with federated_run_plan(_plan(published, cancel=cancel)), evidence_attestation_seat(
        _Cancelling(),
    ):
        with pytest.raises(AskCancelled):
            attest_pointers("table_analysis", ["e-1"])
    assert published == []


# ---------------------------------------------------------------------------
# The table-analysis lane lets control flow through (fix round item 12)
# ---------------------------------------------------------------------------

def _table_lane_service(analyze):
    from types import SimpleNamespace

    from app.services.ask_service import AskService

    warnings: list = []
    service = object.__new__(AskService)
    service.spreadsheet_analysis = SimpleNamespace(analyze=analyze)
    service.ask_engine_hidden_sources = lambda notebook_id, user_id: []
    service.ask_engine_participant_notebooks = lambda notebook_id: []
    service.ask_engine_visible_sources = lambda notebook_id: ["src-table"]
    service._tier_map_for = lambda ids: {}
    service.model_clients = SimpleNamespace(chat=lambda workload: None)
    service.event_log = SimpleNamespace(logger=SimpleNamespace(
        warning=lambda *args: warnings.append(args),
    ))
    prepared = SimpleNamespace(notebook_id="nb", user_id="u", research_question="q")
    runtime = SimpleNamespace(scope=None, cancellation=None, trace_sink=None)
    return service, prepared, runtime, warnings


def test_a_cancel_during_the_table_registration_read_cancels_the_run():
    """Not an empty lane: the pointer read the table producer spends inside a
    global run is stopped by the run's cancel token, and that cancellation
    must leave the lane."""
    from app.models.ask import Citation
    from app.repositories.read_budget import ReadBudgetExceeded
    from app.services.spreadsheet_analysis import _attested_row_citation

    cancel = threading.Event()

    class _Cancelling(_Reader):
        def evidence_fingerprints(self, element_ids):
            cancel.set()
            raise ReadBudgetExceeded("read budget exhausted")

    def analyze(**kwargs):
        _attested_row_citation(Citation(
            label="表 · Sheet1!A1:B2", source_id="src-table", element_id="e-row",
            location_label="Sheet1!A1:B2", quoted_span="q",
        ))
        return [], None

    service, prepared, runtime, warnings = _table_lane_service(analyze)
    with federated_run_plan(_plan([], cancel=cancel)), evidence_attestation_seat(_Cancelling()):
        with pytest.raises(AskCancelled):
            service._spreadsheet_reasoning_results(prepared, runtime, [])
    assert warnings == []


def test_a_retrieval_control_error_leaves_the_table_lane():
    from app.domain.retrieval_control import RetrievalControlError

    def analyze(**kwargs):
        raise RetrievalControlError("participant attestation failed")

    service, prepared, runtime, _warnings = _table_lane_service(analyze)
    with pytest.raises(RetrievalControlError):
        service._spreadsheet_reasoning_results(prepared, runtime, [])


def test_an_ordinary_table_lane_failure_stays_fail_soft():
    def analyze(**kwargs):
        raise RuntimeError("store hiccup")

    service, prepared, runtime, warnings = _table_lane_service(analyze)
    assert service._spreadsheet_reasoning_results(prepared, runtime, []) == []
    assert warnings and warnings[0][1] == "RuntimeError"
