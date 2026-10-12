"""``_ReasoningRunState`` is the only authority for run-level state in the loop.

``ReasoningRetriever.run`` used to unpack the state into ~60 local names once,
after which the locals were the authority and the state's scalars went stale.
Now every read and write goes through ``state.``. The failure this guards
against is silent: a forgotten prefix on a WRITE creates a local the next round
never reads, and a forgotten prefix on a READ of ``max_steps`` reads the
uncapped ``run`` parameter instead of the tier-capped ``state.max_steps`` —
neither raises, and pyflakes sees a defined, used name.

Two rules, checked on the source of every guarded method (``run`` today; the
methods the next split step extracts from it join ``GUARDED``):

1. **No bare state field.** A bare name that is also a ``_ReasoningRunState``
   field is a violation, except a load of one of ``run``'s own frozen-input
   parameters (they equal the state's copy) and the ``max_steps`` argument
   handed to ``_new_run_state``.
2. **No stale-prone alias.** ``x = state.<field>`` is allowed only for a
   mutable container or the trace recorder that the loop never rebinds
   (an alias of a rebound container keeps the old object; an alias of a
   scalar is a copy).
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
#: methods holding the reflect loop and its close
GUARDED = ("run",)
#: ``run`` parameters that are frozen inputs (state keeps an identical copy)
FROZEN_PARAMETERS = frozenset({
    "notebook_id", "question", "history", "on_step", "top_n",
    "intent_queries", "limits", "intent_detail",
})
#: methods allowed to (re)build state containers: they finish before the loop
FIRST_ROUND_PREFIXES = ("_first_round_", "_run_first_round", "_new_run_state")
_CONTAINER_HEADS = ("List", "Dict", "list", "dict", "set", "Set")
_ALIASABLE_OBJECTS = frozenset({"_TraceRecorder"})

Violation = Tuple[str, str, str]


def _class(tree: ast.Module, name: str) -> ast.ClassDef:
    return next(node for node in tree.body
                if isinstance(node, ast.ClassDef) and node.name == name)


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


def violations(source: str) -> List[Violation]:
    """``(method, name, rule)`` for every breach of the two rules."""
    tree = ast.parse(source)
    fields = _state_fields(tree)
    owner = _class(tree, OWNER_CLASS)
    aliasable = _aliasable(fields, _rebound_in_loop(owner))
    found: List[Violation] = []
    for method in owner.body:
        if not isinstance(method, ast.FunctionDef) or method.name not in GUARDED:
            continue
        handed_over = _new_run_state_arguments(method)
        for node in ast.walk(method):
            if isinstance(node, ast.Name) and node.id in fields:
                frozen_read = (node.id in FROZEN_PARAMETERS
                               and isinstance(node.ctx, ast.Load))
                if not frozen_read and id(node) not in handed_over:
                    found.append((method.name, node.id, "bare-state-field"))
            if (isinstance(node, (ast.Assign, ast.AnnAssign))
                    and _is_state_attribute(node.value)
                    and node.value.attr not in aliasable
                    and any(isinstance(target, ast.Name)
                            for raw in (node.targets if isinstance(node, ast.Assign)
                                        else (node.target,))
                            for target in _direct_targets(raw))):
                found.append((method.name, node.value.attr, "stale-prone-alias"))
    return found


@pytest.fixture(scope="module")
def source() -> str:
    return SOURCE.read_text(encoding="utf-8")


def test_the_loop_reads_and_writes_run_state_only_through_state(source):
    assert violations(source) == []


def test_every_guarded_method_exists(source):
    owner = _class(ast.parse(source), OWNER_CLASS)
    names = {node.name for node in owner.body if isinstance(node, ast.FunctionDef)}
    assert set(GUARDED) <= names


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        # a write that lost its prefix: the next round never sees it
        ("state.consult_block_text = rendered.rendered_text",
         "consult_block_text = rendered.rendered_text",
         ("run", "consult_block_text", "bare-state-field")),
        # a reset that lost its prefix
        ("            state.consult_delivered_this_turn = False\n",
         "            consult_delivered_this_turn = False\n",
         ("run", "consult_delivered_this_turn", "bare-state-field")),
        # the loop bound read from the uncapped parameter
        ("while state.steps < state.max_steps:",
         "while state.steps < max_steps:",
         ("run", "max_steps", "bare-state-field")),
        # the closure's overflow write that lost its prefix
        ("            state.outline_overflow = overflow\n",
         "            outline_overflow = overflow\n",
         ("run", "outline_overflow", "bare-state-field")),
    ],
    ids=["write", "reset", "max-steps-parameter", "closure-write"],
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


def test_container_and_recorder_aliases_stay_allowed(source):
    anchor = "        self._run_first_round(state)\n"
    mutated = source.replace(
        anchor, anchor + "        record = state.record\n"
        "        collected = state.collected\n")
    # an alias of a never-rebound container or of the recorder is the same
    # object as state's, so only rule 1 (the bare uses) could object to it
    assert [v for v in violations(mutated) if v[2] == "stale-prone-alias"] == []
