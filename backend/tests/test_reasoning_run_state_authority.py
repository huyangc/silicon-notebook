"""``_ReasoningRunState`` is the only authority for run-level state in the loop.

``ReasoningRetriever.run`` used to unpack the state into ~60 local names once,
after which the locals were the authority and the state's scalars went stale.
Now every read and write goes through ``state.``, and the loop and close that
lived in ``run`` are methods of their own (``_run_reflect_loop`` and what it
calls, ``_disclose_unresolved_after_loop``, ``_finish_run``). The failure this guards
against is silent: a forgotten prefix on a WRITE creates a local the next round
never reads, and a forgotten prefix on a READ of ``max_steps`` reads the
uncapped ``run`` parameter instead of the tier-capped ``state.max_steps`` —
neither raises, and pyflakes sees a defined, used name.

Two rules, checked on the source of every guarded method (``run`` and the
loop/close methods split out of it, listed in ``GUARDED``):

1. **No bare state field.** A bare name that is also a ``_ReasoningRunState``
   field is a violation. Exceptions: in ``run`` only, a load of one of its
   frozen-input parameters (they equal the state's copy) and the ``max_steps``
   argument handed to ``_new_run_state`` — an extracted method reads
   ``state.limits`` / ``state.question``, never a parameter of the same name
   (``limits`` and ``state.enum_limits`` are different values); and a
   same-name alias of an aliasable field (``record = state.record``), its
   later loads included.
2. **No stale-prone alias.** Binding a name to ``state.<field>`` — plain
   (``x = state.f``), tuple (``a, b = state.f, state.g``) or walrus
   (``(x := state.f)``) — is allowed only for a mutable container or the
   trace recorder that the loop never rebinds (an alias of a rebound
   container keeps the old object; an alias of a scalar is a copy). A
   conditional or a call around the attribute is a deliberate snapshot and
   stays allowed.

A completeness check keeps rule coverage from silently shrinking: every
``ReasoningRetriever`` method that takes the run state (a ``state`` parameter
or one annotated ``_ReasoningRunState``) is either guarded, a first-round
stage, or on the explicit ``EXEMPT_STATE_METHODS`` list of pre-existing
action/helper methods — a new loop method has to be placed deliberately.

``PINNED_READS`` pins where the guarded methods read ``state.limits`` versus
``state.enum_limits`` and ``state.enumeration_active`` versus
``self.enumeration_active()``: pairs that look interchangeable, and that the
split had to keep apart verbatim.

Capability implementations and their explicit live views are checked alongside
the coordinator, so moving an action cannot bypass authority checks.

Known blind spots (not checked; none occurs today): ``state.f = ...``
rebinding done by an unrelated module-level helper or another class, through
``setattr``, or under a parameter not named ``state`` is not seen by the
rebinding analysis; an alias taken between ``_new_run_state`` and the first
round of a field the first round later rebinds would go stale unreported.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterable, List, Tuple

import pytest

SOURCE = (
    Path(__file__).resolve().parents[1]
    / "app" / "services" / "reasoning_retrieval.py"
)
STATE_CLASS = "_ReasoningRunState"
OWNER_CLASS = "ReasoningRetriever"
CAPABILITY_OWNERS = {
    "reasoning_paper_actions.py": "ReasoningPaperActions",
    "reasoning_section_navigation.py": "ReasoningSectionNavigation",
}
CAPABILITY_METHODS = {
    "ReasoningPaperActions": frozenset({
        "_paper_tools_spent", "_paper_budget_left", "_paper_allowed",
        "_paper_has_results", "_paper_skip", "_paper_tool_failed",
        "_paper_tools_ledger", "_resolved_paper_sources", "_action_find_sources",
        "_action_get_paper_cards", "_action_extract_from_sources",
    }),
    "ReasoningSectionNavigation": frozenset({
        "_section_evidence_sources", "_section_source_rows", "_resolve_section_source",
        "_section_outline_for", "_section_similarities", "_section_similarities_parallel",
        "_take_section_hint_detail", "_section_hint_block", "_section_hints_text",
        "_section_skip", "_section_actions_offered", "_section_spent",
        "_section_read_left", "_section_listing_note", "_section_failed",
        "_action_browse_sections", "_replay_section_listing", "_deliver_section_listing",
        "_resolve_section_address", "_ambiguous_section_skip", "_action_read_section",
    }),
}
#: methods holding the reflect loop and its close
GUARDED = (
    "run", "_run_reflect_loop", "_reflect_round_ledger_notes",
    "_reflect_round_context_blocks", "_reflect_round_kwargs",
    "_dispatch_reflect_action",
    "_action_expand_graph", "_action_add_subquery", "_action_search_elements",
    "_action_consult_memory", "_action_exact_lookup", "_action_follow_chain",
    "_feed_exact_lookup_skip", "_collect_kg_gap", "_apply_outline_update",
    "_disclose_unresolved_after_loop", "_finish_run",
    # in-document navigation: the two actions, the hint block and their helpers
    "_not_a_name_note", "_section_source_rows", "_resolve_section_source",
    "_section_outline_for", "_section_hint_block", "_section_hints_text",
    "_section_skip", "_action_browse_sections", "_deliver_section_listing",
    "_resolve_section_address", "_action_read_section",
    "_section_spent", "_section_read_left", "_section_listing_note",
    "_section_failed", "_replay_section_listing", "_take_section_hint_detail",
    "_ambiguous_section_skip", "_section_evidence_sources",
    # paper tools: the per-round helpers the loop, the ledger and the kwargs call
    "_note_reflect_decision", "_paper_tools_spent", "_paper_tools_ledger",
    "_action_find_sources", "_action_get_paper_cards", "_action_extract_from_sources",
    "_paper_allowed", "_paper_has_results", "_paper_skip", "_paper_tool_failed",
    "_resolved_paper_sources", "_paper_budget_left",
)
#: ``run`` parameters that are frozen inputs (state keeps an identical copy);
#: exempt in ``run`` only
FROZEN_PARAMETERS = frozenset({
    "notebook_id", "question", "history", "intent_queries", "limits",
    "intent_detail", "intent_keywords",
})
#: pre-existing methods that take the run state but are not loop bodies:
#: action executors and their helpers (they alias containers by convention)
EXEMPT_STATE_METHODS = frozenset({
    "_search_passages_for_direction", "_action_ppr_retrieve",
    "_action_search_chunks", "_action_read_document", "_external_evidence_room",
    "_plugin_action_kwargs", "_add_plugin_reflect_context", "_action_plugin",
    "_admit_external_evidence", "_action_expand_community", "_run_enumeration",
})
#: methods allowed to (re)build state containers: they finish before the loop
FIRST_ROUND_PREFIXES = ("_first_round_", "_run_first_round", "_new_run_state")
_CONTAINER_HEADS = ("List", "Dict", "list", "dict", "set", "Set")
_ALIASABLE_OBJECTS = frozenset({"_TraceRecorder"})

Violation = Tuple[str, str, str]


def _class(tree: ast.Module, name: str) -> ast.ClassDef:
    return next(node for node in tree.body
                if isinstance(node, ast.ClassDef) and node.name == name)


def _state_owner(tree: ast.Module) -> ast.ClassDef:
    """Analyze coordinator and actual capability implementations as one authority.

    Legacy delegates remain covered, but cannot stand in for the moved bodies.
    """
    owner = _class(tree, OWNER_CLASS)
    for name in CAPABILITY_OWNERS.values():
        owner.body.extend(_class(tree, name).body)
    return owner


def _state_fields(tree: ast.Module) -> dict:
    """field name → annotation source."""
    return {
        node.target.id: ast.unparse(node.annotation)
        for node in _class(tree, STATE_CLASS).body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }


def _is_state_attribute(node: ast.AST) -> bool:
    return (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "state")


def _direct_targets(target: ast.AST) -> Iterable[ast.AST]:
    """The names an assignment target rebinds (``state.x[k] = v`` mutates
    ``state.x``; it does not rebind it)."""
    if isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            yield from _direct_targets(element)
    elif isinstance(target, ast.Starred):
        yield from _direct_targets(target.value)
    else:
        yield target


def _rebound_in_loop(owner: ast.ClassDef) -> set:
    """Fields rebound (``state.<field> = ...``) outside the first round."""
    rebound = set()
    for method in owner.body:
        if not isinstance(method, ast.FunctionDef):
            continue
        if method.name.startswith(FIRST_ROUND_PREFIXES):
            continue
        for node in ast.walk(method):
            targets: Iterable[ast.AST] = ()
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = (node.target,)
            for target in targets:
                for part in _direct_targets(target):
                    if _is_state_attribute(part):
                        rebound.add(part.attr)
    return rebound


def _aliasable(fields: dict, rebound: set) -> set:
    return {
        name for name, annotation in fields.items()
        if name not in rebound and (
            annotation.startswith(_CONTAINER_HEADS)
            or annotation in _ALIASABLE_OBJECTS)
    }


def _new_run_state_arguments(method: ast.FunctionDef) -> set:
    """The ``max_steps=max_steps`` value node(s) passed to ``_new_run_state``."""
    allowed = set()
    for node in ast.walk(method):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_new_run_state"):
            for keyword in node.keywords:
                if keyword.arg == "max_steps" and isinstance(keyword.value, ast.Name):
                    allowed.add(id(keyword.value))
    return allowed


def _alias_pairs(node: ast.AST) -> Iterable[Tuple[ast.Name, ast.Attribute]]:
    """``(name, state.<field>)`` pairs a statement or expression binds."""
    if isinstance(node, ast.NamedExpr):
        if _is_state_attribute(node.value):
            yield node.target, node.value
        return
    if isinstance(node, ast.Assign):
        targets, value = node.targets, node.value
    elif isinstance(node, ast.AnnAssign) and node.value is not None:
        targets, value = (node.target,), node.value
    else:
        return
    for target in targets:
        if isinstance(target, ast.Name) and _is_state_attribute(value):
            yield target, value
        elif (isinstance(target, (ast.Tuple, ast.List))
              and isinstance(value, (ast.Tuple, ast.List))
              and len(target.elts) == len(value.elts)):
            for element, item in zip(target.elts, value.elts):
                if isinstance(element, ast.Name) and _is_state_attribute(item):
                    yield element, item


def _takes_run_state(method: ast.FunctionDef) -> bool:
    arguments = method.args.posonlyargs + method.args.args + method.args.kwonlyargs
    return any(
        argument.arg == "state"
        or (argument.annotation is not None
            and STATE_CLASS in ast.unparse(argument.annotation))
        for argument in arguments
    )


def unplaced_state_methods(source: str, guarded=GUARDED) -> List[str]:
    """State-taking methods that are neither guarded, first-round nor exempt."""
    owner = _state_owner(ast.parse(source))
    return sorted(
        method.name for method in owner.body
        if isinstance(method, ast.FunctionDef) and _takes_run_state(method)
        and method.name not in guarded
        and not method.name.startswith(FIRST_ROUND_PREFIXES)
        and method.name not in EXEMPT_STATE_METHODS
    )


def violations(source: str, guarded=GUARDED) -> List[Violation]:
    """``(method, name, rule)`` for every breach of the two rules."""
    tree = ast.parse(source)
    fields = _state_fields(tree)
    owner = _state_owner(tree)
    aliasable = _aliasable(fields, _rebound_in_loop(owner))
    found: List[Violation] = []
    for method in owner.body:
        if not isinstance(method, ast.FunctionDef) or method.name not in guarded:
            continue
        handed_over = _new_run_state_arguments(method)
        frozen = FROZEN_PARAMETERS if method.name == "run" else frozenset()
        same_name_aliases = {
            name.id for node in ast.walk(method)
            for name, attribute in _alias_pairs(node)
            if name.id == attribute.attr and attribute.attr in aliasable
        }
        for node in ast.walk(method):
            for name, attribute in _alias_pairs(node):
                if attribute.attr not in aliasable:
                    found.append((method.name, attribute.attr, "stale-prone-alias"))
            if isinstance(node, ast.Name) and node.id in fields:
                is_load = isinstance(node.ctx, ast.Load)
                excused = (
                    (node.id in frozen and is_load)
                    or id(node) in handed_over
                    or node.id in same_name_aliases and (
                        is_load or _binds_own_field(method, node))
                )
                if not excused:
                    found.append((method.name, node.id, "bare-state-field"))
    return found


def _binds_own_field(method: ast.FunctionDef, name: ast.Name) -> bool:
    """Whether this Store is the ``x = state.x`` alias statement itself."""
    return any(
        bound is name and attribute.attr == name.id
        for node in ast.walk(method)
        for bound, attribute in _alias_pairs(node)
    )


@pytest.fixture(scope="module")
def source() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in (SOURCE, *(SOURCE.with_name(name) for name in CAPABILITY_OWNERS))
    )


def test_the_loop_reads_and_writes_run_state_only_through_state(source):
    assert violations(source) == []


def test_every_guarded_method_exists(source):
    owner = _state_owner(ast.parse(source))
    names = {node.name for node in owner.body if isinstance(node, ast.FunctionDef)}
    assert set(GUARDED) <= names


def missing_capability_methods(source: str) -> set:
    tree = ast.parse(source)
    return {
        (owner, name)
        for owner, expected in CAPABILITY_METHODS.items()
        for name in expected - {
            node.name for node in _class(tree, owner).body
            if isinstance(node, ast.FunctionDef)
        }
    }


def test_capability_implementations_are_guarded_at_their_actual_owner(source):
    assert missing_capability_methods(source) == set()


@pytest.mark.parametrize("relocate", [False, True], ids=["delete", "relocate"])
@pytest.mark.parametrize(
    ("owner", "method"),
    [("ReasoningPaperActions", "_action_get_paper_cards"),
     ("ReasoningSectionNavigation", "_action_read_section")],
)
def test_a_legacy_delegate_cannot_hide_deleted_or_relocated_implementation(
    source, owner, method, relocate,
):
    tree = ast.parse(source)
    implementation = _class(tree, owner)
    moved = next(node for node in implementation.body
                 if isinstance(node, ast.FunctionDef) and node.name == method)
    implementation.body.remove(moved)
    if relocate:
        _class(tree, OWNER_CLASS).body.append(moved)
    assert (owner, method) in missing_capability_methods(ast.unparse(tree))


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [("state.find_sources_calls += 1", "find_sources_calls += 1",
      ("_action_find_sources", "find_sources_calls", "bare-state-field")),
     ("state.section_listing_turn = state.steps", "section_listing_turn = state.steps",
      ("_deliver_section_listing", "section_listing_turn", "bare-state-field")),
     ("taken = dict(state.section_hint_detail)",
      "taken = state.section_hint_detail",
      ("_take_section_hint_detail", "section_hint_detail", "stale-prone-alias"))],
)
def test_moved_implementations_keep_scalar_and_rebound_container_guards(
    source, before, after, expected,
):
    assert source.count(before) == 1
    assert expected in violations(source.replace(before, after))


def test_capability_views_read_and_write_the_unique_live_run_authority():
    from types import SimpleNamespace

    from app.services.reasoning_retrieval import _PaperRunView, _SectionRunView

    state = SimpleNamespace(
        paper_card_reads=0, paper_cards={}, section_reads=0,
        section_hint_detail={}, chunks=[], steps=1, max_steps=3,
    )
    paper, section = _PaperRunView(state), _SectionRunView(state)
    paper.paper_card_reads += 1
    section.section_reads += 1
    assert (state.paper_card_reads, state.section_reads) == (1, 1)
    state.paper_card_reads, state.section_reads = 5, 7
    state.steps, state.max_steps = 2, 4
    assert (paper.paper_card_reads, section.section_reads) == (5, 7)
    assert (section.steps, section.max_steps) == (2, 4)
    assert paper.paper_cards is state.paper_cards
    assert section.chunks is state.chunks
    old_detail = state.section_hint_detail
    section.section_hint_detail = {"entries": 2}
    assert section.section_hint_detail is state.section_hint_detail
    assert state.section_hint_detail is not old_detail
    for view in (paper, section):
        with pytest.raises(AttributeError):
            _ = view.max_outline_updates


def view_authority_violations(source: str) -> set:
    """A view can forward named fields, but cannot cache or redirect authority."""
    tree = ast.parse(source)
    found = set()
    for view_name in ("PaperRunView", "SectionRunView"):
        protocol = _class(tree, view_name)
        expected = {node.target.id for node in protocol.body
                    if isinstance(node, ast.AnnAssign)}
        adapter = _class(tree, "_" + view_name)
        getters = {}
        for method in adapter.body:
            if not isinstance(method, ast.FunctionDef):
                continue
            if method.name == "__init__":
                body = method.body
                valid = len(body) == 1 and ast.unparse(body[0]) == "self._state = state"
            elif any(isinstance(d, ast.Name) and d.id == "property"
                     for d in method.decorator_list):
                getters[method.name] = method
                valid = (len(method.body) == 1 and isinstance(method.body[0], ast.Return)
                         and ast.unparse(method.body[0].value) == "self._state." + method.name)
            else:
                valid = (len(method.body) == 1
                         and ast.unparse(method.body[0]) == "self._state." + method.name + " = value")
            if not valid:
                found.add((view_name, method.name))
        found.update((view_name, field) for field in expected ^ getters.keys())
    return found


def test_every_capability_view_field_forwards_the_same_authority(source):
    assert view_authority_violations(source) == set()


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [("return self._state.paper_card_reads", "return 0", ("PaperRunView", "paper_card_reads")),
     ("self._state.section_reads = value", "self._state.section_browses = value",
      ("SectionRunView", "section_reads"))],
)
def test_a_view_cannot_snapshot_or_redirect_a_budget(source, before, after, expected):
    assert source.count(before) == 1
    assert expected in view_authority_violations(source.replace(before, after))


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        # a write that lost its prefix: the next round never sees it
        ("state.consult_block_text = rendered.rendered_text",
         "consult_block_text = rendered.rendered_text",
         ("_action_consult_memory", "consult_block_text", "bare-state-field")),
        # a reset that lost its prefix
        ("            state.consult_delivered_this_turn = False\n",
         "            consult_delivered_this_turn = False\n",
         ("_run_reflect_loop", "consult_delivered_this_turn", "bare-state-field")),
        # the loop bound read from the uncapped parameter
        ("while state.steps < state.max_steps:",
         "while state.steps < max_steps:",
         ("_run_reflect_loop", "max_steps", "bare-state-field")),
        # the outline method's overflow write that lost its prefix
        ("        state.outline_overflow = overflow\n",
         "        outline_overflow = overflow\n",
         ("_apply_outline_update", "outline_overflow", "bare-state-field")),
    ],
    ids=["write", "reset", "max-steps-parameter", "outline-method-write"],
)
def test_a_dropped_prefix_is_reported(source, before, after, expected):
    assert source.count(before) == 1, before
    assert expected in violations(source.replace(before, after))


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        ("steps = state.steps", ("run", "steps", "stale-prone-alias")),
        ("outline = state.outline", ("run", "outline", "stale-prone-alias")),
    ],
    ids=["scalar", "rebound-container"],
)
def test_a_stale_prone_alias_is_reported(source, alias, expected):
    anchor = "        self._run_first_round(state)\n"
    assert source.count(anchor) == 1
    mutated = source.replace(anchor, anchor + f"        {alias}\n")
    assert expected in violations(mutated)


_ANCHOR = "        self._run_first_round(state)\n"


def test_same_name_container_and_recorder_aliases_stay_allowed(source):
    # an alias of a never-rebound container or of the recorder is the same
    # object as state's: neither rule objects, its later loads included
    mutated = source.replace(
        _ANCHOR, _ANCHOR + "        record = state.record\n"
        "        collected = state.collected\n"
        "        record(collected)\n")
    assert violations(mutated) == []


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        ("a, b = state.steps, state.collected",
         ("run", "steps", "stale-prone-alias")),
        ("print((n := state.stale))", ("run", "stale", "stale-prone-alias")),
    ],
    ids=["tuple", "walrus"],
)
def test_tuple_and_walrus_aliases_are_reported(source, alias, expected):
    assert expected in violations(source.replace(_ANCHOR, _ANCHOR + f"        {alias}\n"))


@pytest.mark.parametrize(
    "snapshot",
    ["n = state.steps if state.stale else 0", "n = int(state.steps)"],
    ids=["conditional", "call"],
)
def test_conditional_and_call_snapshots_stay_allowed(source, snapshot):
    assert violations(source.replace(_ANCHOR, _ANCHOR + f"        {snapshot}\n")) == []


def _with_method(source: str, body: str) -> str:
    """The production source plus one extra ``ReasoningRetriever`` method."""
    anchor = "    def run(self, notebook_id, question,"
    assert source.count(anchor) == 1
    return source.replace(anchor, body + anchor)


def test_an_extracted_method_may_not_read_a_frozen_parameter_bare(source):
    # ``limits`` here may be ``state.enum_limits`` (tier default) while the run
    # had ``limits=None``: the extracted method must read ``state.limits``
    mutated = _with_method(source, (
        "    def _turn(self, state, limits):\n"
        "        return limits.effort\n\n"))
    assert ("_turn", "limits", "bare-state-field") in violations(
        mutated, guarded=GUARDED + ("_turn",))


def test_every_state_taking_method_is_placed(source):
    assert unplaced_state_methods(source) == []


@pytest.mark.parametrize(
    "signature",
    ["(self, state)", "(self, run_state: \"_ReasoningRunState\")"],
    ids=["named-state", "annotated"],
)
def test_a_new_unplaced_loop_method_is_reported(source, signature):
    mutated = _with_method(
        source, f"    def _reflect_turn{signature}:\n        return None\n\n")
    assert unplaced_state_methods(mutated) == ["_reflect_turn"]


#: Reads that look interchangeable but are not, pinned where the split left
#: them. ``state.limits`` is the caller's raw value (None for knowhow and
#: plain runs) and ``state.enum_limits`` its tier fallback: swapping one for the
#: other changes the element view, the experience situation and the evidence
#: budget of a limits-less run. ``state.enumeration_active`` is the run's
#: snapshot (it shapes the prompt) and ``self.enumeration_active()`` the live
#: gate the dispatch re-checks as defence in depth.
PINNED_READS = {
    ("_reflect_round_ledger_notes", "state.limits"): 3,
    ("_reflect_round_ledger_notes", "state.enumeration_active"): 1,
    ("_reflect_round_context_blocks", "state.enum_limits"): 1,
    ("_reflect_round_kwargs", "state.enum_limits"): 1,
    ("_dispatch_reflect_action", "self.enumeration_active()"): 1,
    ("_action_consult_memory", "state.limits"): 2,
    ("_finish_run", "state.limits"): 1,
    # The in-document navigation gate reads the TIER FALLBACK, exactly like
    # ``document_read_active``: ``reflect()`` gets ``limits=state.enum_limits``
    # from ``_reflect_round_kwargs``, so prompt and executor judge one tier.
    ("_not_a_name_note", "state.enum_limits"): 1,
    ("_section_read_left", "state.enum_limits"): 1,
    ("_action_browse_sections", "state.enum_limits"): 1,
    ("_action_read_section", "state.enum_limits"): 1,
    # The paper tools read the tier fallback too: ``reflect()`` judges the same
    # tier through ``limits=state.enum_limits``.
    ("_paper_tools_spent", "state.enum_limits"): 1,
    ("_paper_tools_ledger", "state.enum_limits"): 1,
    ("_paper_budget_left", "state.enum_limits"): 4,
    ("_action_find_sources", "state.enum_limits"): 1,
    ("_action_get_paper_cards", "state.enum_limits"): 3,
    ("_action_extract_from_sources", "state.enum_limits"): 2,
}


def pinned_reads(source: str) -> dict:
    """``(method, read) -> count`` over the guarded methods."""
    owner = _state_owner(ast.parse(source))
    found: dict = {}
    for method in owner.body:
        if not isinstance(method, ast.FunctionDef) or method.name not in GUARDED:
            continue
        for node in ast.walk(method):
            read = None
            if _is_state_attribute(node) and node.attr in (
                    "limits", "enum_limits", "enumeration_active"):
                read = f"state.{node.attr}"
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and isinstance(node.func.value, ast.Name)
                  and node.func.value.id == "self"
                  and node.func.attr == "enumeration_active"):
                read = "self.enumeration_active()"
            if read:
                found[(method.name, read)] = found.get((method.name, read), 0) + 1
    return found


def test_raw_limits_and_both_enumeration_gates_stay_where_they_were(source):
    assert pinned_reads(source) == PINNED_READS


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("and not self.enumeration_active()\n", "and not state.enumeration_active\n"),
        ("len(state.used_queries), limits=state.limits\n",
         "len(state.used_queries), limits=state.enum_limits\n"),
    ],
    ids=["live-gate-to-snapshot", "raw-limits-to-fallback"],
)
def test_a_normalised_read_is_reported(source, before, after):
    assert source.count(before) == 1, before
    assert pinned_reads(source.replace(before, after)) != PINNED_READS
