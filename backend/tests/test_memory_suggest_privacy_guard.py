"""Auto user-memory suggestions: the owner predicate lives in the SQL
(design §6.1; plan T7b).

Three static checks, read through the shared ``tests.architecture.source_trees``
entry:

1. **Every read statement binds its owner.** For both
   ``memory_suggest_store.py`` backends, every SQL string literal of
   ``MemorySuggestStore`` is taken one statement at a time (module-level SQL
   constants spliced into f-strings are resolved; any other interpolation is
   an opaque placeholder), its ``--`` and ``/* */`` comments are stripped, and
   each READ statement (``SELECT``, or a ``WITH`` that writes nothing) must
   contain the owner predicate registered for its method. A predicate moved
   into a comment, or deleted, is red; so is a read method nobody registered,
   and a registered method that no longer reads.
2. **The pipeline modules stay on the ports.** The job, the trigger, the
   signal functions and the switch service import no repository module except
   ``app.repositories.ports`` (G28), and nothing from global Ask or the report
   engine.
3. **The job reaches data through its three seats only**, and only through
   the methods registered here (every one of them owner-bound in SQL by
   check 1, or the shared read-access predicate).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.domain.memory_suggest_contracts import (
    MEMORY_SUGGEST_FORBIDDEN_EVENT_KEYS,
    MEMORY_SUGGEST_LIFECYCLE_EVENT_KEYS,
    MEMORY_SUGGEST_RUN_EVENT_KEYS,
)
from tests.architecture.source_trees import read_source_tree

APP = Path(__file__).resolve().parents[1] / "app"
STORES = {
    "sqlite": APP / "repositories" / "sqlite" / "memory_suggest_store.py",
    "postgres": APP / "repositories" / "postgres" / "memory_suggest_store.py",
}
STORE_CLASS = "MemorySuggestStore"

_PH = r"(?:\?|%s)"


def _owner(column: str) -> str:
    return rf"(?<![\w.]){re.escape(column)}\s*=\s*{_PH}"


#: Read method -> the owner predicate every read statement in it must carry.
READ_PREDICATES: dict[str, str] = {
    # the switch row is keyed by its owner
    "user_state": _owner("owner_id"),
    # the existence probe of the user whose mode is written
    "set_memory_mode": _owner("id"),
    "unadopted_candidate_count": _owner("m.created_by"),
    # ask jobs by their creator
    "clarified_scan": _owner("a.created_by"),
    "corrected_scan": rf"(?s)^(?=.*{_owner('a.created_by')})(?=.*{_owner('p.created_by')})(?=.*{_owner('c.created_by')})",
    "earliest_unfinished_ask": _owner("a.created_by"),
    "repeated_window": _owner("a.created_by"),
    # answers and feedback through the conversation's creator
    "answer_projections": _owner("c.created_by"),
    "latest_feedback": _owner("c.created_by"),
    # memories by their creator
    "answers_with_memory": _owner("m.created_by"),
    "suggest_neighbors": _owner("m.created_by"),
    # fingerprints by their owner
    "fingerprints_present": _owner("fp.owner_id"),
}

#: Modules of the suggestion pipeline (G28) and the import prefixes they may
#: never reach.
PIPELINE_MODULES = (
    "services/memory_suggest_job.py",
    "services/memory_suggest_trigger.py",
    "services/memory_suggest_signals.py",
    "services/memory_suggest_service.py",
)
FORBIDDEN_IMPORTS = (
    "app.repositories.",
    "app.services.global_ask",
    "app.services.report_",
    "app.services.reports",
    "app.services.ask_service",
)
ALLOWED_REPOSITORY_IMPORT = "app.repositories.ports"

#: Seat -> methods the job may call on it.
JOB_SEAT_CALLS: dict[str, frozenset[str]] = {
    "store": frozenset({
        "user_state", "claim", "settle", "sweep_stale_on_start",
        "reserve_daily_quota", "unadopted_candidate_count", "clarified_scan",
        "repeated_window", "corrected_scan", "answer_projections", "latest_feedback",
        "answers_with_memory", "fingerprints_present", "earliest_unfinished_ask",
        "suggest_neighbors",
    }),
    "memory": frozenset({"create_suggestions", "expire_suggestions"}),
    "access": frozenset({"user_can_read_notebook"}),
}

_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.DOTALL)
_COMMENT_LINE = re.compile(r"--[^\n]*")
_STATEMENT_HEAD = re.compile(r"^\s*(SELECT|WITH|INSERT|UPDATE|DELETE)\b", re.IGNORECASE)
_WRITE_WORD = re.compile(r"\b(INSERT|UPDATE|DELETE)\b", re.IGNORECASE)


def strip_sql_comments(sql: str) -> str:
    return _COMMENT_LINE.sub(" ", _COMMENT_BLOCK.sub(" ", sql))


def _module_strings(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "..."`` string constants (SQL fragments)."""
    found: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            found[node.targets[0].id] = node.value.value
    return found


def _render(node: ast.AST, constants: dict[str, str]) -> str | None:
    """The text of a string literal: a constant, or an f-string with module
    constants spliced in and every other interpolation as ``{…}``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(str(value.value))
            elif (
                isinstance(value, ast.FormattedValue)
                and isinstance(value.value, ast.Name)
                and value.value.id in constants
            ):
                parts.append(constants[value.value.id])
            else:
                parts.append("{…}")
        return "".join(parts)
    return None


def sql_statements(function: ast.AST, constants: dict[str, str]) -> list[str]:
    """Every outermost string literal in ``function`` that is an SQL
    statement, comments stripped."""
    nested: set[int] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.JoinedStr):
            nested.update(id(child) for child in ast.walk(node) if child is not node)
    statements: list[str] = []
    for node in ast.walk(function):
        if id(node) in nested:
            continue
        text = _render(node, constants)
        if text is None:
            continue
        stripped = strip_sql_comments(text)
        if _STATEMENT_HEAD.match(stripped):
            statements.append(stripped)
    return statements


def is_read(statement: str) -> bool:
    head = _STATEMENT_HEAD.match(statement)
    if head is None:
        return False
    keyword = head.group(1).upper()
    if keyword == "SELECT":
        return True
    return keyword == "WITH" and not _WRITE_WORD.search(statement)


def store_methods(path: Path) -> tuple[dict[str, ast.FunctionDef], dict[str, str]]:
    tree = read_source_tree(path)
    constants = _module_strings(tree)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == STORE_CLASS:
            return (
                {
                    member.name: member
                    for member in node.body
                    if isinstance(member, ast.FunctionDef)
                },
                constants,
            )
    raise AssertionError(f"{STORE_CLASS} not found in {path}")


def owner_predicate_violations(path: Path) -> list[str]:
    """``[]`` when every read statement of the store carries its registered
    owner predicate and the registry matches the read methods exactly."""
    methods, constants = store_methods(path)
    problems: list[str] = []
    readers: set[str] = set()
    for name, function in methods.items():
        reads = [s for s in sql_statements(function, constants) if is_read(s)]
        if not reads:
            continue
        readers.add(name)
        predicate = READ_PREDICATES.get(name)
        if predicate is None:
            problems.append(f"{name}: a read method with no registered owner predicate")
            continue
        for statement in reads:
            if not re.search(predicate, statement):
                problems.append(f"{name}: read without its owner predicate: {statement[:120]}")
    for name in sorted(set(READ_PREDICATES) - readers):
        problems.append(f"{name}: registered as a reader but reads nothing (or is gone)")
    return problems


@pytest.mark.parametrize("backend", sorted(STORES))
def test_every_store_read_binds_its_owner_in_sql(backend):
    assert owner_predicate_violations(STORES[backend]) == []


@pytest.mark.parametrize("backend", sorted(STORES))
@pytest.mark.parametrize("column", ["p.created_by", "c.created_by"])
@pytest.mark.parametrize("move_to_comment", [False, True])
def test_corrected_pair_owner_guards_reject_deleted_or_commented_predicates(
    tmp_path, backend, column, move_to_comment,
):
    # Derive each mutant from the actual production method, using a new tree
    # so the shared syntax-tree cache remains immutable.
    tree = ast.parse(ast.unparse(read_source_tree(STORES[backend])))
    method = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.FunctionDef) and node.name == "corrected_scan")
    predicate = f"{column}={'?' if backend == 'sqlite' else '%s'}"
    changed = 0
    for node in ast.walk(method):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and predicate in node.value:
            replacement = f"1=1 /* {predicate} */" if move_to_comment else "1=1"
            node.value = node.value.replace(predicate, replacement)
            changed += 1
    assert changed == 1, "mutation must change the one intended owner predicate"
    mutant = tmp_path / f"{backend}_memory_suggest_store.py"
    mutant.write_text(ast.unparse(tree), encoding="utf-8")
    problems = owner_predicate_violations(mutant)
    assert any(problem.startswith("corrected_scan: read without its owner predicate") for problem in problems)


@pytest.mark.parametrize("backend", sorted(STORES))
def test_the_store_really_has_the_reads_this_guard_checks(backend):
    """Empty-run protection: the statements are found at all, and the
    sampling reads resolve the shared predicate constant."""
    methods, constants = store_methods(STORES[backend])
    reads = {
        name: [s for s in sql_statements(fn, constants) if is_read(s)]
        for name, fn in methods.items()
    }
    assert sum(len(found) for found in reads.values()) >= len(READ_PREDICATES)
    assert all("a.status = 'done'" in s for s in reads["clarified_scan"])
    assert all("submitted_via = 'web'" in s for s in reads["repeated_window"])


def test_a_predicate_in_a_comment_does_not_count(tmp_path):
    """The comment stripping is what makes "moved into a comment" red."""
    source = STORES["sqlite"].read_text(encoding="utf-8")
    needle = (
        '"SELECT m.source_answer_id FROM memory_items m "\n'
        '                f"WHERE m.created_by = ? AND {drive_by(\'m.source_answer_id\', bound)}",'
    )
    assert needle in source
    mutated = source.replace(
        needle,
        '"SELECT m.source_answer_id FROM memory_items m "\n'
        '                f"WHERE /* m.created_by = ? AND */ {drive_by(\'m.source_answer_id\', bound)}",',
    )
    path = tmp_path / "memory_suggest_store.py"
    path.write_text(mutated, encoding="utf-8")
    problems = owner_predicate_violations(path)
    assert any(p.startswith("answers_with_memory:") for p in problems), problems
    stripped = strip_sql_comments("SELECT 1 FROM t WHERE x = 1 -- AND a.created_by = ?")
    assert not re.search(READ_PREDICATES["clarified_scan"], stripped)


def test_an_unregistered_read_method_is_red(tmp_path):
    source = STORES["postgres"].read_text(encoding="utf-8")
    mutated = source.replace(
        "    def unadopted_candidate_count(",
        "    def peek_everything(self):\n"
        "        return 'SELECT * FROM memory_items m WHERE m.created_by = %s'\n\n"
        "    def unadopted_candidate_count(",
        1,
    )
    path = tmp_path / "memory_suggest_store.py"
    path.write_text(mutated, encoding="utf-8")
    assert any(
        p.startswith("peek_everything:") for p in owner_predicate_violations(path)
    )


def _imports(tree: ast.Module) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append(node.module)
            found.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return found


@pytest.mark.parametrize("module", PIPELINE_MODULES)
def test_the_pipeline_imports_repositories_only_through_ports(module):
    imported = _imports(read_source_tree(APP / module))
    assert imported, module
    offenders = [
        name for name in imported
        if any(name.startswith(prefix) for prefix in FORBIDDEN_IMPORTS)
        and not (
            name == ALLOWED_REPOSITORY_IMPORT
            or name.startswith(ALLOWED_REPOSITORY_IMPORT + ".")
        )
    ]
    assert offenders == [], (module, offenders)


def _job_seat_calls() -> dict[str, set[str]]:
    """``self.<seat>.<method>(...)`` calls in the job module, plus the
    ``memory`` seat read through a local (``memory = self.memory``)."""
    tree = read_source_tree(APP / "services" / "memory_suggest_job.py")
    calls: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        owner = node.func.value
        if (
            isinstance(owner, ast.Attribute)
            and isinstance(owner.value, ast.Name)
            and owner.value.id == "self"
            and owner.attr in JOB_SEAT_CALLS
        ):
            calls.setdefault(owner.attr, set()).add(node.func.attr)
        elif isinstance(owner, ast.Name) and owner.id in JOB_SEAT_CALLS:
            calls.setdefault(owner.id, set()).add(node.func.attr)
    return calls


def test_the_job_calls_only_registered_seat_methods():
    calls = _job_seat_calls()
    for seat, methods in calls.items():
        assert methods <= JOB_SEAT_CALLS[seat], (seat, sorted(methods - JOB_SEAT_CALLS[seat]))
    # Empty-run protection: the job really samples and really writes.
    assert {"clarified_scan", "repeated_window", "answer_projections"} <= calls["store"]
    assert {"create_suggestions", "expire_suggestions"} <= calls["memory"]
    assert calls["access"] == {"user_can_read_notebook"}


def test_the_run_event_literal_carries_exactly_the_whitelisted_keys():
    """Static half of ``test_event_keys_whitelisted``: every dict literal the
    job hands to ``event_log.emit`` has exactly the closed key set (plus the
    ``kind`` envelope) and nothing from the forbidden content keys."""
    tree = read_source_tree(APP / "services" / "memory_suggest_job.py")
    emitted: list[set[str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "emit"
        ):
            for argument in node.args:
                assert isinstance(argument, ast.Dict), "emit a dict literal only"
                keys = {key.value for key in argument.keys if isinstance(key, ast.Constant)}
                assert len(keys) == len(argument.keys), "no ** spreads in the event"
                emitted.append(keys)
    assert emitted, "the job emits its run event"
    for keys in emitted:
        assert keys == set(MEMORY_SUGGEST_RUN_EVENT_KEYS) | {"kind"}, keys
        assert not keys & MEMORY_SUGGEST_FORBIDDEN_EVENT_KEYS


def _lifecycle_event_violations(tree: ast.Module) -> list[str]:
    service = next(node for node in tree.body
                   if isinstance(node, ast.ClassDef) and node.name == "MemoryService")
    methods = {node.name: node for node in service.body if isinstance(node, ast.FunctionDef)}
    helper = methods["_event"]
    base = next(node for node in ast.walk(helper) if isinstance(node, ast.Dict))
    base_keys = {key.value for key in base.keys if isinstance(key, ast.Constant)}
    actions = {
        "LIFECYCLE_SUGGESTION_CREATED": "create_suggestions",
        "LIFECYCLE_SUGGESTION_EXPIRED": "expire_suggestions",
    }
    found = []
    for method, function in methods.items():
        for call in ast.walk(function):
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                continue
            if call.func.attr == "_event":
                action = next((kw.value.id for kw in call.keywords
                               if kw.arg == "action" and isinstance(kw.value, ast.Name)
                               and kw.value.id in actions), None)
                if action:
                    keys = base_keys | {kw.arg for kw in call.keywords}
                    kind = call.args[0].value if call.args and isinstance(call.args[0], ast.Constant) else None
                    found.append((method, action, keys, kind))
            elif call.func.attr == "emit":
                for argument in call.args:
                    if not isinstance(argument, ast.Dict):
                        continue
                    values = {key.value: value for key, value in zip(argument.keys, argument.values)
                              if isinstance(key, ast.Constant)}
                    action = values.get("action")
                    if isinstance(action, ast.Name) and action.id in actions:
                        keys = {key.value if isinstance(key, ast.Constant) else None for key in argument.keys}
                        kind = values.get("kind")
                        found.append((method, action.id, keys, kind.value if isinstance(kind, ast.Constant) else None))
    errors = []
    expected_keys = set(MEMORY_SUGGEST_LIFECYCLE_EVENT_KEYS) | {"kind"}
    for method, action, keys, kind in found:
        if keys != expected_keys or keys & MEMORY_SUGGEST_FORBIDDEN_EVENT_KEYS:
            errors.append(f"non-whitelisted suggestion lifecycle keys: {method}")
        if kind != "memory_lifecycle":
            errors.append(f"wrong suggestion lifecycle kind: {method}")
    if sorted((method, action) for method, action, _keys, _kind in found) != sorted(
        (method, action) for action, method in actions.items()
    ):
        errors.append("suggestion lifecycle action sites changed")
    return errors


def test_suggestion_lifecycle_events_have_only_the_closed_content_free_keys():
    tree = read_source_tree(APP / "services" / "memory_service.py")
    assert not _lifecycle_event_violations(tree)


def test_lifecycle_key_removal_or_emission_moved_to_another_site_is_rejected():
    source = '''
class MemoryService:
    def _event(self, kind, item, **extra):
        self.log.emit({"kind":kind,"memory_id":item.id,"notebook_id":item.nb,
                       "owner_id":item.owner,"origin":item.origin,"status":item.status,**extra})
    def create_suggestions(self, item):
        self._event("memory_lifecycle",item,action=LIFECYCLE_SUGGESTION_CREATED,suggested_signal=item.signal)
    def expire_suggestions(self, item):
        self.log.emit({"kind":"memory_lifecycle","memory_id":item.id,"notebook_id":item.nb,
                       "owner_id":item.owner,"origin":item.origin,"status":item.status,
                       "action":LIFECYCLE_SUGGESTION_EXPIRED,"suggested_signal":item.signal})
'''
    assert not _lifecycle_event_violations(ast.parse(source))
    missing = source.replace(",suggested_signal=item.signal", "")
    assert any("non-whitelisted" in error for error in _lifecycle_event_violations(ast.parse(missing)))
    moved = source.replace("def create_suggestions", "def unreviewed_lifecycle")
    assert "suggestion lifecycle action sites changed" in _lifecycle_event_violations(ast.parse(moved))


def test_every_owner_bound_store_read_the_job_uses_is_registered():
    """The store methods the job reads with are exactly ones check 1 pins (or
    job-row / quota / fingerprint writes keyed by the owner)."""
    writes = {
        "claim", "settle", "sweep_stale_on_start", "reserve_daily_quota",
        "write_fingerprints",
    }
    used = _job_seat_calls()["store"]
    assert used - writes <= set(READ_PREDICATES), sorted(used - writes - set(READ_PREDICATES))
