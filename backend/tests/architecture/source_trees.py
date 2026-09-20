"""Share read-only syntax trees between co-located repository guards.

Callers must not mutate the returned AST. Every lookup reads the current file;
the exact content is part of the cache key, so edits are visible even if a
mutation test restores the file's size and timestamp. Directory discovery and
synthetic string parsing stay with each guard and are never cached here.
"""
from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1024)
def _parse_source(source: str, filename: str) -> ast.Module:
    return ast.parse(source, filename=filename)


def read_source_tree(path: Path, *, errors: str = "strict") -> ast.Module:
    return _parse_source(
        path.read_text(encoding="utf-8", errors=errors), str(path.resolve()),
    )
