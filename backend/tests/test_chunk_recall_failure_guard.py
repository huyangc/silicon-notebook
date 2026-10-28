"""Every fail-open ``except`` on the chunk recall's call tree tells the
run's recall memo (``chunk_recall_memo``) that the recall is degraded.

The memo keeps a recall for the raw-element arm only when nothing on the way
failed open: a recall whose full-text half timed out, whose ANN handle did
not open or whose contributor failed is not what a healthy repeat gives, so
an element search must recall again rather than inherit it.  The memo can
only know through ``note_recall_failure`` (directly, or through
``_note_model_error``, which calls it), so every handler on the tree must:

* re-raise (``raise`` anywhere in its body), or
* call ``note_recall_failure`` / ``_note_model_error``, or a hook the lane
  installs that does (``NOTING_CALLS``: the generated-question call's
  failure hooks, the contribution host's event emitter, whose sink
  ``chunk_recall_memo.noting_contribution_events`` wraps), or
* be registered in ``EXEMPT`` with the number of such handlers in that
  function and why none of them leaves a degraded recall behind.

The tree is the static closure from ``ROOTS``: inside a module, a ``self.X``
reference (call or not -- a bound method handed on as a callback runs on the
tree too) reaches method ``X`` of the root's class family, and a bare name
reaches the module's function ``X``; nested functions and lambdas are part of
the function that holds them.  Other modules join only as explicit roots.
Like the hit-origin guard this is a registry: it pins that every handler was
looked at, not what each one does at runtime (the behaviour is pinned by
``test_chunk_recall_memo``'s per-failure cases).
"""
from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
RC = "services/retrieval_candidates.py"
GQ = "services/generated_question_contribution.py"
EXT = "extensions/retrieval.py"
CL = "services/chunk_lane.py"

#: (module, class family, root qualnames)
ROOTS = (
    (RC, ("CandidateRetrievalService", "_RetrievalState"),
     ("CandidateRetrievalService._retrieve_chunks",
      "CandidateRetrievalService._retrieve_chunks_once")),
    (GQ, ("GeneratedQuestionContributionCall", "_AdmissionSource"),
     ("GeneratedQuestionContributionCall.propose",
      "GeneratedQuestionContributionCall.read",
      "GeneratedQuestionContributionCall.visible_result",
      "GeneratedQuestionContributionCall.fail_open_result",
      "_AdmissionSource.read")),
    (EXT, ("RetrievalContributorHost",), ("RetrievalContributorHost.run",)),
    (CL, (), ("_lexical_gate_drift_probe",)),
)

NOTING_CALLS = {
    RC: {"note_recall_failure", "_note_model_error"},
    GQ: {"note_recall_failure", "_emit_failure", "_failure_event",
         "_matrix_failure_event"},
    EXT: {"emit", "_emit"},
    CL: {"note_recall_failure"},
}

#: (module, qualname) -> (handlers exempt in that function, why)
EXEMPT = {
    (RC, "CandidateRetrievalService._chunk_source_ceiling"): (
        2, "the report authority probe fails CLOSED to "
           "_REPORT_CHUNK_AUTHORITY_FAILED (memoised per run; a recall under it "
           "takes no memo slot), and the inner one only guards its telemetry"),
    (RC, "CandidateRetrievalService._emit_generated_question_query_event"): (
        1, "telemetry only; the hooks that call it noted first (noting)"),
    (GQ, "GeneratedQuestionContributionCall._emit_failure"): (
        1, "the failure hook raising; the hook noted before it ran (noting)"),
    (GQ, "GeneratedQuestionContributionCall._valid_evaluation"): (
        1, "a predicate: False makes propose raise, whose handler calls "
           "_emit_failure"),
    (GQ, "GeneratedQuestionContributionCall.visible_result"): (
        1, "the matrix-failure hook raising; the hook noted before it ran"),
    (EXT, "RetrievalContributorHost.run"): (
        1, "a malformed cancellation token is the core's own object: no "
           "contributor ran, the same on every call"),
    (EXT, "RetrievalContributorHost._run"): (
        2, "a failed connection probe is followed by the invalid_context emit "
           "the sink notes; behind the admission read the only I/O is "
           "_AdmissionSource.read's fallback, which notes its own failure "
           "(the rest is deterministic conversion of the same answer)"),
    (EXT, "RetrievalContributorHost._emit"): (
        1, "the event sink raising; the noting sink noted before forwarding"),
    (EXT, "RetrievalContributorHost._valid_result"): (
        1, "a validation predicate over the contributor's result"),
    (EXT, "RetrievalContributorHost._candidate_structurally_valid"): (
        1, "a validation predicate over one candidate"),
    (EXT, "RetrievalContributorHost._valid_cancellation_token"): (
        1, "a validation predicate over the core's token"),
    (CL, "_lexical_gate_drift_probe"): (
        1, "the diagnostic emit of the probe failure it has just noted"),
}


def _parse(relative: str) -> ast.Module:
    return ast.parse((APP / relative).read_text(encoding="utf-8"))


def _functions(tree: ast.Module) -> dict:
    """qualname -> node, for module functions and class methods (one level)."""
    table: dict = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            table[node.name] = node
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    table[f"{node.name}.{item.name}"] = item
    return table


def _reachable(relative: str, family: tuple, roots: tuple) -> dict:
    table = _functions(_parse(relative))
    module_level = {name for name in table if "." not in name}
    seen: dict = {}
    stack = list(roots)
    while stack:
        qualname = stack.pop()
        if qualname in seen or qualname not in table:
            continue
        node = seen[qualname] = table[qualname]
        for child in ast.walk(node):
            if (isinstance(child, ast.Attribute)
                    and isinstance(child.value, ast.Name)
                    and child.value.id in {"self", "cls"}):
                stack.extend(f"{owner}.{child.attr}" for owner in family
                             if f"{owner}.{child.attr}" in table)
            elif isinstance(child, ast.Name) and child.id in module_level:
                stack.append(child.id)
    return seen


def _called_names(node: ast.AST) -> set:
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Attribute):
                names.add(func.attr)
            elif isinstance(func, ast.Name):
                names.add(func.id)
    return names


def _handler_ok(relative: str, handler: ast.ExceptHandler) -> bool:
    if any(isinstance(child, ast.Raise) for child in ast.walk(handler)):
        return True
    return bool(_called_names(handler) & NOTING_CALLS[relative])


def unnoted_handlers() -> dict:
    """(module, qualname) -> number of handlers that neither re-raise nor note."""
    found: dict = {}
    for relative, family, roots in ROOTS:
        for qualname, node in _reachable(relative, family, roots).items():
            count = sum(
                1 for child in ast.walk(node)
                if isinstance(child, ast.ExceptHandler)
                and not _handler_ok(relative, child)
            )
            if count:
                found[(relative, qualname)] = count
    return found


def test_every_handler_on_the_recall_tree_notes_or_is_registered():
    found = unnoted_handlers()
    expected = {site: entry[0] for site, entry in EXEMPT.items() if entry[0]}
    assert found == expected, (
        "a fail-open except on the chunk recall's tree must re-raise, call "
        "note_recall_failure/_note_model_error, or be registered in EXEMPT "
        f"with its reason: found {found}, registered {expected}"
    )


def test_every_registered_site_is_still_on_the_tree():
    reachable = {
        (relative, qualname)
        for relative, family, roots in ROOTS
        for qualname in _reachable(relative, family, roots)
    }
    assert set(EXEMPT) <= reachable, sorted(set(EXEMPT) - reachable)
    assert all(reason for _count, reason in EXEMPT.values())


def test_the_roots_exist():
    for relative, _family, roots in ROOTS:
        table = _functions(_parse(relative))
        assert set(roots) <= set(table), (relative, sorted(set(roots) - set(table)))


def test_the_guard_sees_a_handler_that_only_logs():
    source = (
        "class CandidateRetrievalService:\n"
        "    def _retrieve_chunks_once(self):\n"
        "        try:\n"
        "            pass\n"
        "        except Exception:\n"
        "            self.event_log.emit({})\n"
        "        try:\n"
        "            pass\n"
        "        except Exception:\n"
        "            chunk_recall_memo.note_recall_failure('x')\n"
    )
    node = ast.parse(source).body[0].body[0]
    handlers = [child for child in ast.walk(node)
                if isinstance(child, ast.ExceptHandler)]
    assert [_handler_ok(RC, handler) for handler in handlers] == [False, True]
