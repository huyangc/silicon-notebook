"""Z4 (P0, 热路径修复批 0) structural guard.

``ask_stream``'s synchronous DB prep (``repo.get_notebook`` +
``_require_ask_available`` -- which on a large notebook re-reads
``all_visible_source_ids``/``hidden_source_ids`` over tens of thousands of
rows -- plus ``_intent_history``) and ``_stream_ask_events``'s first
synchronous segment (``repo.start_ask_stream``) used to run directly on the
event-loop thread. On a large notebook that stalls every other coroutine in
the process for seconds, including ``/api/ready``. The fix mirrors the
wrapping shape already used by ``preview_ask_intent`` /
``preview_ask_intent_stream``: bundle the synchronous work into one nested
function and hand it to ``asyncio.to_thread``.

This is an AST/source guard rather than a runtime timing test on purpose:
a mocked repository in a unit test answers so fast that "ran on the event
loop" and "ran in a thread" are behaviourally indistinguishable without
inspecting the code shape itself.
"""
from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ASK_ROUTES = ROOT / "backend" / "app" / "api" / "ask_routes.py"


def _tree() -> ast.Module:
    return ast.parse(ASK_ROUTES.read_text(encoding="utf-8"))


def _find_function(tree: ast.AST, name: str) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {ASK_ROUTES}")


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _calls(node: ast.AST):
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            yield child


def _nested_function_defs(node: ast.AST) -> list[ast.FunctionDef]:
    return [
        child for child in ast.walk(node)
        if isinstance(child, ast.FunctionDef) and child is not node
    ]


def test_ask_stream_db_prep_runs_inside_asyncio_to_thread() -> None:
    ask_stream = _find_function(_tree(), "ask_stream")

    to_thread_calls = [
        call for call in _calls(ask_stream)
        if _dotted(call.func) == "asyncio.to_thread"
    ]
    assert to_thread_calls, (
        "ask_stream must hand its synchronous DB prep to asyncio.to_thread, "
        "mirroring preview_ask_intent's prepare_preview wrapping"
    )
    threaded_names = {
        arg.id for call in to_thread_calls for arg in call.args[:1]
        if isinstance(arg, ast.Name)
    }

    nested_defs = _nested_function_defs(ask_stream)
    prep_fn = next((fn for fn in nested_defs if fn.name in threaded_names), None)
    assert prep_fn is not None, (
        "asyncio.to_thread's argument must be a nested function defined "
        "inside ask_stream (the prep closure), not called bare"
    )

    prep_calls = {_dotted(call.func) for call in _calls(prep_fn)}
    for required in ("repo.get_notebook", "_require_ask_available", "_intent_history"):
        assert required in prep_calls, (
            f"{required} must be called from inside the asyncio.to_thread-"
            "wrapped prep closure, not directly on ask_stream's event-loop "
            "code path"
        )

    # These two must NOT also appear directly in ask_stream's own body
    # (outside every nested def) -- that would mean only part of the prep
    # got moved off the event loop.
    nested_call_ids = {id(call) for fn in nested_defs for call in _calls(fn)}
    top_level_calls = {
        _dotted(call.func) for call in _calls(ask_stream)
        if id(call) not in nested_call_ids
    }
    assert "repo.get_notebook" not in top_level_calls
    assert "_require_ask_available" not in top_level_calls


def test_stream_ask_events_start_ask_stream_runs_inside_asyncio_to_thread() -> None:
    """``repo.start_ask_stream`` must run off the event loop, but as an
    actual nested-closure CALL (not a bare function reference handed to
    ``asyncio.to_thread``) -- ``tests/test_repository_protocol_coverage.py``
    scans production source for the literal ``repo.start_ask_stream(...)``
    call form to prove ``AskStreamPort``'s declared surface is exercised, and
    a bare-reference hand-off would make that member invisible to it."""
    stream_fn = _find_function(_tree(), "_stream_ask_events")

    to_thread_calls = [
        call for call in _calls(stream_fn)
        if _dotted(call.func) == "asyncio.to_thread"
    ]
    assert to_thread_calls, (
        "_stream_ask_events must run repo.start_ask_stream via "
        "asyncio.to_thread, not directly on the event loop"
    )
    threaded_names = {
        arg.id for call in to_thread_calls for arg in call.args[:1]
        if isinstance(arg, ast.Name)
    }
    nested_defs = _nested_function_defs(stream_fn)
    start_fn = next((fn for fn in nested_defs if fn.name in threaded_names), None)
    assert start_fn is not None, (
        "asyncio.to_thread's argument must be a nested closure defined "
        "inside _stream_ask_events that itself calls repo.start_ask_stream, "
        "not repo.start_ask_stream passed bare by reference"
    )
    inner_calls = {_dotted(call.func) for call in _calls(start_fn)}
    assert "repo.start_ask_stream" in inner_calls, (
        "the nested closure passed to asyncio.to_thread must actually call "
        "repo.start_ask_stream(...)"
    )

    awaited = any(
        isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and _dotted(node.value.func) == "asyncio.to_thread"
        for node in ast.walk(stream_fn)
    )
    assert awaited, (
        "the asyncio.to_thread(...) call must be awaited, or the scheduled "
        "thread work is silently dropped"
    )

    # repo.start_ask_stream must not ALSO be called directly on the event
    # loop somewhere outside the threaded closure.
    nested_call_ids = {id(call) for fn in nested_defs for call in _calls(fn)}
    top_level_calls = {
        _dotted(call.func) for call in _calls(stream_fn)
        if id(call) not in nested_call_ids
    }
    assert "repo.start_ask_stream" not in top_level_calls
