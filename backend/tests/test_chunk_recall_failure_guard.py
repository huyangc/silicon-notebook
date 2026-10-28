"""Every fail-open ``except`` on the chunk recall's call tree tells the
run's recall memo (``chunk_recall_memo``) that the recall is degraded.

The memo keeps a recall for the raw-element arm only when nothing on the way
failed open: a recall whose full-text half timed out, whose ANN handle did
not open or whose contributor failed is not what a healthy repeat gives, so
an element search must recall again rather than inherit it.  The memo can
only know through ``note_recall_failure`` (directly, or through
``_note_model_error``, which calls it), so every handler on the tree must,
as a TOP-LEVEL statement of its body -- a raise or a note under an ``if``
(``if isinstance(e, X): raise`` and swallow the rest) does not count:

* re-raise, or
* call ``note_recall_failure`` / ``_note_model_error``, or a hook the lane
  installs that does (``NOTING_CALLS``: the generated-question call's
  failure hooks; the contribution host's ``_note_degraded``, and its
  ``emit``/``_emit`` only with a literal ``outcome`` among the host's
  ``DEGRADED_CONTRIBUTION_OUTCOMES`` and not ``stable``), or
* be registered in ``EXEMPT``, one entry per handler -- keyed by module,
  function, the handler's exception type as written and its ordinal among
  that function's handlers of the same type -- with why it leaves no
  degraded recall behind.

The tree is the static closure from ``ROOTS``: inside a module, a ``self.X``
reference (call or not -- a bound method handed on as a callback runs on the
tree too) reaches method ``X`` of the root's class family, and a bare name
reaches the module's function ``X``; nested functions and lambdas are part of
the function that holds them.  Other modules join only as explicit roots.

What it does NOT cover: a degradation that is not an ``except`` -- a
component that answers ``None`` or an empty result for "could not" (the scale
catalog's failed load, an ANN handle that did not open) is noted where that
answer is read, and those notes are pinned by the per-failure cases
(``test_chunk_recall_memo_failures``), not here.  Like the hit-origin guard
this is a registry: it pins that every handler was looked at, not what each
one does at runtime.
"""
from __future__ import annotations

import ast
from pathlib import Path

from app.extensions.retrieval import DEGRADED_CONTRIBUTION_OUTCOMES

APP = Path(__file__).resolve().parents[1] / "app"
RC = "services/retrieval_candidates.py"
GQ = "services/generated_question_contribution.py"
EXT = "extensions/retrieval.py"
CL = "services/chunk_lane.py"
SAC = "services/scale_artifact_catalog.py"

#: (module, class family, root qualnames)
ROOTS = (
    (RC, ("CandidateRetrievalService", "_RetrievalState"),
     ("CandidateRetrievalService._retrieve_chunks",
      "CandidateRetrievalService._retrieve_chunks_once",
      "CandidateRetrievalService._element_chunk_recall")),
    (GQ, ("GeneratedQuestionContributionCall", "_AdmissionSource"),
     ("GeneratedQuestionContributionCall.propose",
      "GeneratedQuestionContributionCall.read",
      "GeneratedQuestionContributionCall.visible_result",
      "GeneratedQuestionContributionCall.fail_open_result",
      "_AdmissionSource.read")),
    (EXT, ("RetrievalContributorHost",), ("RetrievalContributorHost.run",)),
    (CL, (), ("_lexical_gate_drift_probe",)),
    (SAC, ("ScaleArtifactCatalog",),
     ("ScaleArtifactCatalog.load", "ScaleArtifactCatalog.open_ann",
      "ScaleArtifactCatalog.peek_warm_chunk_index")),
)

NOTING_CALLS = {
    RC: {"note_recall_failure", "_note_model_error"},
    GQ: {"note_recall_failure", "_emit_failure", "_failure_event",
         "_emit_matrix_failure", "_matrix_failure_event"},
    EXT: {"_note_degraded"},
    CL: {"note_recall_failure"},
    SAC: {"note_recall_failure"},
}
#: Calls that note only with a degraded literal ``outcome`` and not ``stable``.
OUTCOME_CALLS = {EXT: {"emit", "_emit"}}

_TELEMETRY = "only guards its own telemetry/event emit; retrieval is unchanged"

#: (module, qualname, exception type as written, ordinal) -> why
EXEMPT = {
    (RC, "CandidateRetrievalService._chunk_source_ceiling", "Exception", 0):
        "the report authority probe fails CLOSED to "
        "_REPORT_CHUNK_AUTHORITY_FAILED, memoised per run; a recall under it "
        "takes no memo slot",
    (RC, "CandidateRetrievalService._chunk_source_ceiling", "Exception", 1):
        _TELEMETRY,
    (RC, "CandidateRetrievalService._emit_generated_question_query_event",
     "Exception", 0): _TELEMETRY + " (the hooks that call it noted first)",
    (RC, "CandidateRetrievalService._recall_readable_by_element_arm",
     "Exception", 0):
        "decides only whether a recall that already succeeded is kept; "
        "failing answers 'not kept', the safe side",
    (GQ, "GeneratedQuestionContributionCall._emit_failure", "Exception", 0):
        "the failure hook raising; the hook noted before it ran (noting)",
    (GQ, "GeneratedQuestionContributionCall._valid_evaluation", "Exception", 0):
        "a predicate: False makes propose raise, whose handler calls "
        "_emit_failure",
    (GQ, "GeneratedQuestionContributionCall._emit_matrix_failure", "Exception", 0):
        "the matrix-failure hook raising; the hook noted before it ran",
    (EXT, "RetrievalContributorHost.run", "_MalformedCancellationToken", 0):
        "a malformed cancellation token is the core's own object: no "
        "contributor ran, the same on every call",
    (EXT, "RetrievalContributorHost._run", "Exception", 0):
        "the context factory failing is followed, in a loop over the "
        "registrations, by invalid_context emits that note",
    (EXT, "RetrievalContributorHost._run", "Exception", 1):
        "a failed connection probe leaves connection_held=None, followed by "
        "invalid_context emits (in a loop) that note",
    (EXT, "RetrievalContributorHost._run", "Exception", 2):
        "a failed baseline identity is followed, in a loop over the "
        "registrations, by invalid_context emits that note",
    (EXT, "RetrievalContributorHost._emit", "Exception", 0): _TELEMETRY,
    (EXT, "RetrievalContributorHost._note_degraded", "Exception", 0):
        "the caller's on_degraded hook raising; the caller owns that hook",
    (EXT, "RetrievalContributorHost._valid_result", "Exception", 0):
        "a validation predicate over the contributor's result",
    (EXT, "RetrievalContributorHost._candidate_structurally_valid",
     "Exception", 0): "a validation predicate over one candidate",
    (EXT, "RetrievalContributorHost._valid_cancellation_token", "Exception", 0):
        "a validation predicate over the core's token",
    (SAC, "ScaleArtifactCatalog.open_ann", "Exception", 0):
        "a failed open answers None to every caller (its note_model_error is "
        "the catalog's own); the chunk lane notes 'chunk_ann_open' where it "
        "reads that None",
    (CL, "_lexical_gate_drift_probe", "Exception", 1):
        _TELEMETRY + " (the probe failure was noted just before)",
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


def _callee(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _notes(relative: str, call: ast.Call) -> bool:
    name = _callee(call)
    if name in NOTING_CALLS.get(relative, set()):
        return True
    if name not in OUTCOME_CALLS.get(relative, set()):
        return False
    keywords = {keyword.arg: keyword.value for keyword in call.keywords}
    outcome = keywords.get("outcome")
    stable = keywords.get("stable")
    return (
        isinstance(outcome, ast.Constant)
        and outcome.value in DEGRADED_CONTRIBUTION_OUTCOMES
        and not (isinstance(stable, ast.Constant) and stable.value is True)
        and not (stable is not None and not isinstance(stable, ast.Constant))
    )


def _handler_ok(relative: str, handler: ast.ExceptHandler) -> bool:
    """A top-level ``raise``, or a top-level call that notes."""
    for statement in handler.body:
        if isinstance(statement, ast.Raise):
            return True
        if (isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Call)
                and _notes(relative, statement.value)):
            return True
    return False


def _handler_sites(relative: str, qualname: str, node: ast.AST) -> list:
    """``(key, handler)`` for every handler in ``node``, in source order (a
    depth-first walk of the fields, which is source order; no positions)."""
    handlers: list = []

    def visit(current: ast.AST) -> None:
        if isinstance(current, ast.ExceptHandler):
            handlers.append(current)
        for child in ast.iter_child_nodes(current):
            visit(child)

    visit(node)
    seen: dict = {}
    sites = []
    for handler in handlers:
        kind = ast.unparse(handler.type) if handler.type is not None else ""
        ordinal = seen.get(kind, 0)
        seen[kind] = ordinal + 1
        sites.append(((relative, qualname, kind, ordinal), handler))
    return sites


def unnoted_handlers() -> set:
    """The keys of every handler on the tree that neither re-raises nor notes."""
    found: set = set()
    for relative, family, roots in ROOTS:
        for qualname, node in _reachable(relative, family, roots).items():
            found.update(
                key for key, handler in _handler_sites(relative, qualname, node)
                if not _handler_ok(relative, handler)
            )
    return found


def test_every_handler_on_the_recall_tree_notes_or_is_registered():
    found = unnoted_handlers()
    assert found == set(EXEMPT), (
        "a fail-open except on the chunk recall's tree must re-raise or note "
        "at its top level, or be registered in EXEMPT with its reason: "
        f"unregistered {sorted(found - set(EXEMPT))}, "
        f"registered but no longer unnoted {sorted(set(EXEMPT) - found)}"
    )
    assert all(EXEMPT.values())


def test_the_roots_exist():
    for relative, _family, roots in ROOTS:
        table = _functions(_parse(relative))
        assert set(roots) <= set(table), (relative, sorted(set(roots) - set(table)))


# ---------------------------------------------------------------------------
# The guard's own judgement: every shape it must refuse
# ---------------------------------------------------------------------------

def _handlers(body: str, relative: str = RC) -> list:
    source = "def f(self):\n" + "".join(f"    {line}\n" for line in body.splitlines())
    node = ast.parse(source).body[0]
    return [_handler_ok(relative, handler)
            for _key, handler in _handler_sites(relative, "f", node)]


def test_a_handler_that_only_logs_is_refused():
    assert _handlers(
        "try:\n    pass\nexcept Exception:\n    self.event_log.emit({})"
    ) == [False]


def test_a_top_level_note_or_raise_is_accepted():
    assert _handlers(
        "try:\n    pass\nexcept Exception:\n    chunk_recall_memo.note_recall_failure('x')\n"
        "try:\n    pass\nexcept Exception:\n    log()\n    raise"
    ) == [True, True]


def test_a_conditional_raise_is_refused():
    assert _handlers(
        "try:\n    pass\nexcept Exception as exc:\n"
        "    if isinstance(exc, KeyError):\n        raise\n    return None"
    ) == [False]


def test_a_note_under_a_condition_is_refused():
    assert _handlers(
        "try:\n    pass\nexcept Exception:\n"
        "    if flag:\n        note_recall_failure('x')\n    return None"
    ) == [False]


def test_an_emit_is_judged_by_its_outcome():
    verdicts = _handlers(
        "try:\n    pass\nexcept Exception:\n    emit(cid, outcome='failed')\n"
        "try:\n    pass\nexcept Exception:\n    emit(cid, outcome='rejected')\n"
        "try:\n    pass\nexcept Exception:\n    emit(cid, outcome=value)\n"
        "try:\n    pass\nexcept Exception:\n    self._emit(cid, outcome='unavailable', stable=True)\n"
        "try:\n    pass\nexcept Exception:\n    self._emit(cid, outcome='timeout', stable=flag)",
        relative=EXT,
    )
    assert verdicts == [True, False, False, False, False]


def test_exemptions_are_per_handler():
    """A second handler of the same type in a function with one exemption is
    a new site of its own, not covered by the first."""
    source = (
        "def f(self):\n"
        "    try:\n        pass\n    except Exception:\n        pass\n"
        "    try:\n        pass\n    except Exception:\n        pass\n"
        "    try:\n        pass\n    except OSError:\n        pass\n"
    )
    node = ast.parse(source).body[0]
    assert [key for key, _handler in _handler_sites(RC, "f", node)] == [
        (RC, "f", "Exception", 0), (RC, "f", "Exception", 1), (RC, "f", "OSError", 0),
    ]
