"""Auto user-memory suggestions: every multi-row write to
``memory_suggest_jobs`` goes through the ordered-lock helper (review P2-2).

``ordered_job_delete_sql`` (``repositories/memory_suggest_rows.py``) locks the
rows a multi-row DELETE will remove in ``(notebook_id, owner_id)`` order before
deleting them, so two multi-row deletes over overlapping rows -- one per
notebook from a sharing change, one per owner from turning the switch off --
cannot deadlock. That only holds while NO other statement deletes or updates
job rows in bulk in its own scan order. This guard reads every non-SQLite
``backend/app`` module (and ``scripts/``) and classifies each SQL string
literal that carries a ``DELETE FROM memory_suggest_jobs`` or ``UPDATE
memory_suggest_jobs`` statement:

* the body of ``ordered_job_delete_sql`` itself -- allowed;
* a DELETE whose WHERE is exactly the full primary key (one row) -- allowed;
* an UPDATE whose WHERE is plain equalities that include the full primary key
  (claim / settle: one row) -- allowed;
* anything else must be registered in ``EXEMPTIONS`` with a verified reason;
  an unregistered statement fails naming the function and the literal, and an
  exemption whose function no longer holds such a statement is stale and fails.

String pieces are joined the way the compiler joins them (implicit
concatenation, f-string constant parts, ``+`` of literals); an interpolated
value reads as ``{}``. Docstrings (bare string statements) are not SQL.
``INDIRECT_WRITERS`` registers the writers that build their SQL at run time
(table name in a variable) and so cannot be seen by a literal scan: each is
checked to still exist, so a removed or renamed one cannot rot silently.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import NamedTuple

import pytest

from tests.architecture.source_trees import app_source_trees, read_source_tree

BACKEND = Path(__file__).resolve().parents[1]
SCRIPTS = BACKEND.parent / "scripts"

HELPER = "ordered_job_delete_sql"
HELPER_QUALNAME = "ordered_job_delete_sql"
HELPER_FILE = "repositories/memory_suggest_rows.py"
STORE_FILE = "repositories/postgres/memory_suggest_store.py"

#: The four statements that delete job rows in bulk, and the module each lives
#: in. Every one must call the helper (so the guard cannot pass vacuously).
HELPER_CALL_SITES: dict[str, str] = {
    "MemorySuggestStore.set_memory_mode": STORE_FILE,
    "MemorySuggestStore.clear_job_rows": STORE_FILE,
    "AuthStore.merge_sso_account": "repositories/auth_store.py",
    "_clear_suggest_jobs_without_read_on": "repositories/postgres/group_store.py",
}

#: Statements that are multi-row (not a full-primary-key write) yet may stay
#: outside the helper, by qualname -> the verified reason.
EXEMPTIONS: dict[str, str] = {
    "MemorySuggestStore.sweep_stale_on_start": (
        "A multi-row UPDATE (not a delete) of the rows left 'running' by a "
        "previous process. It runs once at startup in the single job-owning "
        "process before that process takes any job, so no concurrent "
        "multi-row writer of this process exists (plan appendix B4/B11); the "
        "only other writers of those rows are the single-row claim/settle of "
        "the same, not yet started, job runner."
    ),
}

#: Writers whose SQL is assembled from a variable table name, invisible to a
#: literal scan. ``(file, kind, reason)``; ``kind`` picks the existence check.
INDIRECT_WRITERS: dict[str, tuple[str, str, str]] = {
    "notebook delete, phase 3 form two": (
        "services/notebook_delete_tables.py",
        "direct_table",
        "PostgreSQL deletes the notebook's rows in ctid batches "
        "(``DELETE ... WHERE ctid = ANY(ARRAY(SELECT ctid ... WHERE "
        "notebook_id = %s LIMIT n))``), an unordered multi-row delete scoped "
        "to ONE notebook. It runs only in phase 3 of the delete job, after "
        "phase 1 tombstoned the notebook ('deleting': every read-access "
        "predicate and every entry point treats it as gone) and phase 2 "
        "waited the in-flight writers out; the module design (batch 3 W1 "
        "T-3) assumes no concurrent writer of that notebook's rows remains, "
        "so no ordered per-notebook delete (a sharing change) can overlap in "
        "more than the rows the tombstone already made unreachable. A "
        "per-owner ordered delete overlaps this one in at most one row "
        "(notebook X, owner O) and a single shared row cannot form a lock "
        "cycle. This is a design assumption, not enforced by this guard.",
    ),
    "account merge, scaffolding rows": (
        "repositories/user_references.py",
        "user_reference",
        "``AuthStore.merge_sso_account`` deletes every row of the removed "
        "account in each scaffolding table with ``DELETE FROM {table} WHERE "
        "{column}=?`` -- except ``memory_suggest_jobs`` on PostgreSQL, which "
        "it deletes through ``ordered_job_delete_sql`` (pinned as a helper "
        "call site above), so a per-notebook ordered cleanup over several "
        "notebooks cannot lock the same rows in the opposite order.",
    ),
}

_PLACEHOLDER = r"(?:%s|\?|\{\})"
_DELETE = re.compile(r"\bDELETE\s+FROM\s+(?:ONLY\s+)?memory_suggest_jobs\b", re.I)
_UPDATE = re.compile(r"\bUPDATE\s+(?:ONLY\s+)?memory_suggest_jobs\b", re.I)
_RETURNING = re.compile(r"\s+RETURNING\b.*$", re.I)
_EQUALITY = re.compile(
    rf"^(?:memory_suggest_jobs\.)?(\w+)\s*=\s*{_PLACEHOLDER}$", re.I
)
_PRIMARY_KEY = {"notebook_id", "owner_id"}


class Statement(NamedTuple):
    relative: str
    qualname: str
    text: str


def _render(node: ast.AST) -> str | None:
    """The text a string expression evaluates to, ``{}`` for each value that
    is not a literal; ``None`` when ``node`` is not a string expression."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for piece in node.values:
            rendered = _render(piece)
            parts.append("{}" if rendered is None else rendered)
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _render(node.left), _render(node.right)
        if left is None and right is None:
            return None
        return ("{}" if left is None else left) + ("{}" if right is None else right)
    return None


class _Collector(ast.NodeVisitor):
    def __init__(self, relative: str) -> None:
        self.relative = relative
        self.stack: list[str] = []
        self.found: list[Statement] = []

    @property
    def qualname(self) -> str:
        return ".".join(self.stack) or "<module>"

    def _scope(self, node: ast.AST) -> None:
        self.stack.append(node.name)  # type: ignore[attr-defined]
        self.generic_visit(node)
        self.stack.pop()

    visit_ClassDef = visit_FunctionDef = visit_AsyncFunctionDef = _scope

    def visit_Expr(self, node: ast.Expr) -> None:
        if _render(node.value) is not None:
            return  # a docstring or a bare string: prose, not SQL
        self.generic_visit(node)

    def _string(self, node: ast.AST) -> None:
        text = _render(node)
        if text is None:
            self.generic_visit(node)
            return
        if _DELETE.search(text) or _UPDATE.search(text):
            self.found.append(Statement(self.relative, self.qualname, text))

    visit_Constant = visit_JoinedStr = _string

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, ast.Add) and _render(node) is not None:
            self._string(node)
        else:
            self.generic_visit(node)


def collect_statements(tree: ast.AST, relative: str) -> list[Statement]:
    collector = _Collector(relative)
    collector.visit(tree)
    return collector.found


def _where_equalities(clause: str) -> list[str] | None:
    """The columns of ``col = placeholder AND ...``; ``None`` when any
    conjunct is something else (a range, an OR, a subquery, ...)."""
    columns: list[str] = []
    for conjunct in re.split(r"\s+AND\s+", clause.strip(), flags=re.I):
        match = _EQUALITY.match(conjunct.strip())
        if match is None:
            return None
        columns.append(match.group(1).lower())
    return columns


def _is_single_row(text: str) -> bool:
    """Whether ``text`` is a DELETE keyed by exactly the full primary key or
    an UPDATE whose WHERE is plain equalities including the full primary key."""
    normal = " ".join(text.split())
    if _DELETE.search(normal) and not _UPDATE.search(normal):
        tail = normal[_DELETE.search(normal).end():].strip()
        match = re.match(r"WHERE\s+(.*)$", _RETURNING.sub("", tail), re.I)
        if match is None:
            return False
        columns = _where_equalities(match.group(1))
        return sorted(columns or []) == sorted(_PRIMARY_KEY)
    if _UPDATE.search(normal) and not _DELETE.search(normal):
        tail = _RETURNING.sub("", normal[_UPDATE.search(normal).end():])
        wheres = list(re.finditer(r"\bWHERE\b", tail, re.I))
        if len(wheres) != 1:
            return False
        columns = _where_equalities(tail[wheres[0].end():])
        return columns is not None and _PRIMARY_KEY <= set(columns)
    return False


def violations(statements: list[Statement]) -> list[Statement]:
    """The statements that are neither the helper's body, a single-row write,
    nor registered in ``EXEMPTIONS``."""
    return [
        s for s in statements
        if s.qualname != HELPER_QUALNAME
        and s.qualname not in EXEMPTIONS
        and not _is_single_row(s.text)
    ]


def _scanned() -> list[Statement]:
    statements: list[Statement] = []
    for source in app_source_trees():
        if source.relative.startswith("repositories/sqlite/"):
            continue  # one writer at a time: no lock order to keep
        statements.extend(collect_statements(source.tree, source.relative))
    for path in sorted(SCRIPTS.rglob("*.py")):
        statements.extend(collect_statements(
            read_source_tree(path), f"../scripts/{path.relative_to(SCRIPTS).as_posix()}"
        ))
    return statements


# ---------------------------------------------------------------- the guard
def test_every_multi_row_job_write_goes_through_the_ordered_helper():
    bad = violations(_scanned())
    assert not bad, "\n".join(
        f"{s.relative}: {s.qualname} writes memory_suggest_jobs in bulk outside "
        f"{HELPER}() and is not registered in EXEMPTIONS: {s.text!r}"
        for s in bad
    )


def test_exemptions_are_not_stale():
    needing = {
        s.qualname for s in _scanned()
        if s.qualname != HELPER_QUALNAME and not _is_single_row(s.text)
    }
    stale = sorted(set(EXEMPTIONS) - needing)
    assert not stale, (
        f"EXEMPTIONS name functions that no longer hold a multi-row "
        f"memory_suggest_jobs statement: {stale}"
    )
    assert all(reason.strip() for reason in EXEMPTIONS.values())


def _functions(tree: ast.AST) -> dict[str, ast.AST]:
    found: dict[str, ast.AST] = {}

    def walk(node: ast.AST, prefix: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + (child.name,)
                found[".".join(name)] = child
                walk(child, name)
            else:
                walk(child, prefix)

    walk(tree, ())
    return found


def _calls_helper(function: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == HELPER
        for node in ast.walk(function)
    )


@pytest.mark.parametrize("qualname,relative", sorted(HELPER_CALL_SITES.items()))
def test_each_known_call_site_uses_the_helper(qualname, relative):
    functions = _functions(read_source_tree(BACKEND / "app" / relative))
    assert qualname in functions, f"{relative} no longer defines {qualname}"
    assert _calls_helper(functions[qualname]), (
        f"{qualname} must delete job rows through {HELPER}()"
    )


def test_the_helper_still_locks_in_primary_key_order():
    tree = read_source_tree(BACKEND / "app" / HELPER_FILE)
    helper = _functions(tree)[HELPER_QUALNAME]
    (statement,) = collect_statements(helper, HELPER_FILE)
    text = " ".join(statement.text.split())
    assert "AS MATERIALIZED" in text, text
    assert "ORDER BY notebook_id, owner_id FOR UPDATE" in text, text
    assert text.index("FOR UPDATE") < text.index("DELETE FROM memory_suggest_jobs"), text


def _has_call(tree: ast.AST, name: str, first_arg: str) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == name
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == first_arg
        for node in ast.walk(tree)
    )


@pytest.mark.parametrize("name", sorted(INDIRECT_WRITERS))
def test_indirect_writers_are_still_registered_where_the_reason_says(name):
    relative, kind, reason = INDIRECT_WRITERS[name]
    assert reason.strip()
    tree = read_source_tree(BACKEND / "app" / relative)
    if kind == "direct_table":
        assert _has_call(tree, "DirectTable", "memory_suggest_jobs"), (
            f"{relative} no longer lists memory_suggest_jobs as a direct-delete "
            f"table; revisit INDIRECT_WRITERS[{name!r}]"
        )
    else:
        assert _has_call(tree, "UserReference", "memory_suggest_jobs"), (
            f"{relative} no longer lists memory_suggest_jobs; revisit "
            f"INDIRECT_WRITERS[{name!r}]"
        )


# ------------------------------------------------- the classifier classifies
def _statements(code: str, relative: str = "x.py") -> list[Statement]:
    return collect_statements(ast.parse(code), relative)


def test_classifier_flags_a_bare_per_owner_delete():
    code = (
        "class Store:\n"
        "    def clear(self, db, owner):\n"
        '        db.execute("DELETE FROM memory_suggest_jobs WHERE owner_id = %s", (owner,))\n'
    )
    (bad,) = violations(_statements(code))
    assert bad.qualname == "Store.clear"


@pytest.mark.parametrize("sql", [
    "DELETE FROM memory_suggest_jobs WHERE notebook_id = %s",
    "DELETE FROM memory_suggest_jobs WHERE notebook_id = %s AND owner_id IN (SELECT 1)",
    "DELETE FROM memory_suggest_jobs WHERE notebook_id = %s OR owner_id = %s",
    "DELETE FROM memory_suggest_jobs WHERE notebook_id = %s AND owner_id = %s AND status = %s",
    "DELETE FROM memory_suggest_jobs",
    "UPDATE memory_suggest_jobs SET status = %s WHERE status = %s",
    "UPDATE memory_suggest_jobs SET status = %s WHERE notebook_id = %s",
    "UPDATE memory_suggest_jobs SET status = %s",
])
def test_classifier_flags_every_other_shape(sql):
    code = f"def f(db):\n    db.execute({sql!r})\n"
    assert violations(_statements(code)), sql


@pytest.mark.parametrize("sql", [
    "DELETE FROM memory_suggest_jobs WHERE notebook_id=%s AND owner_id=%s",
    "DELETE FROM memory_suggest_jobs\n  WHERE owner_id = %s  AND  notebook_id = %s",
    "UPDATE memory_suggest_jobs SET status = %s WHERE notebook_id = %s AND owner_id = %s",
    "UPDATE memory_suggest_jobs SET a = %s WHERE notebook_id = %s AND owner_id = %s "
    "AND status = %s AND claim_token = %s RETURNING a",
])
def test_classifier_accepts_single_row_writes(sql):
    code = f"def f(db):\n    db.execute({sql!r})\n"
    assert not violations(_statements(code)), sql


def test_classifier_joins_implicit_concatenation_and_fstrings_and_plus():
    code = (
        "def f(db, x):\n"
        '    db.execute("DELETE FROM memory_suggest_jobs " f"WHERE owner_id = {x}")\n'
        "def g(db, w):\n"
        '    db.execute("DELETE FROM memory_suggest_jobs WHERE " + w)\n'
    )
    assert {s.qualname for s in violations(_statements(code))} == {"f", "g"}


def test_classifier_ignores_docstrings_and_unrelated_update_text():
    code = (
        "def f(db):\n"
        '    """DELETE FROM memory_suggest_jobs WHERE owner_id = %s"""\n'
        '    db.execute("INSERT INTO memory_suggest_jobs (a) VALUES (%s) '
        'ON CONFLICT (a) DO UPDATE SET b = memory_suggest_jobs.b + 1")\n'
    )
    assert not violations(_statements(code))
