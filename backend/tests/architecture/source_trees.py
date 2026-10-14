"""Share read-only syntax trees between co-located repository guards.

Callers must not mutate the returned AST. Every lookup reads the current file;
the exact content is part of the cache key, so edits are visible even if a
mutation test restores the file's size and timestamp. Directory discovery and
synthetic string parsing stay with each guard and are never cached here.

``app_source_trees()`` is the shared way to read every module under
``backend/app``; guards that go through it (and ``read_source_tree()`` for a
single file) let a pytest worker parse each file once no matter how many of them
run there. New whole-tree guards should use it too. The file list and the file
text are re-read on every call; only the parse is cached, keyed by content alone
(an AST carries no file name), so a guard never sees a stale tree.
"""
from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

_APP_ROOT = Path(__file__).resolve().parents[2] / "app"


@lru_cache(maxsize=4096)
def _parse_content(source: str) -> ast.Module:
    return ast.parse(source)


def _parse_source(source: str, filename: str) -> ast.Module:
    """Parse by content alone: an AST node carries no file name, so two files with
    identical text share one tree (and a copy of the app tree costs no re-parse).
    The file name only labels a ``SyntaxError``; failures are never cached."""
    try:
        return _parse_content(source)
    except SyntaxError as exc:
        exc.filename = filename
        raise


def read_source_tree(path: Path, *, errors: str = "strict") -> ast.Module:
    return _parse_source(
        path.read_text(encoding="utf-8", errors=errors), str(path.resolve()),
    )


class AppSource(NamedTuple):
    #: POSIX path relative to the scanned root (``backend/app`` by default).
    relative: str
    source: str
    tree: ast.Module


def app_source_trees(root: Path | None = None) -> tuple[AppSource, ...]:
    """``(relative, source, tree)`` for every ``*.py`` under ``backend/app``.

    ``root`` lets a mutation test point a guard at a fake app directory; the trees
    for it are cached by content like any other. Files come back in sorted path
    order. Callers must not mutate the returned trees.
    """
    base = root if root is not None else _APP_ROOT
    found: list[AppSource] = []
    for path in sorted(base.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        found.append(AppSource(
            path.relative_to(base).as_posix(), source,
            _parse_source(source, str(path.resolve())),
        ))
    return tuple(found)
