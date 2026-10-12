# backend/tests/test_global_answer_shape_guard.py
"""Every reader of a global job's answer reads BOTH persisted shapes, or says why not.

A global job's answer is persisted in one of two shapes: ``answer`` (the shared
engine's ``AskResponse``, what ``_run`` writes today, with the streamed
``trace`` cleared on completion) and ``response`` (``GlobalAskAnswer``, only on
rows written before the engine switch, with any streamed steps in the top-level
``trace``). The one reading of the pair is the projection family in
``app/models/global_ask.py`` (``global_answer_text`` / ``_citations`` /
``_trace`` / ``_payload`` / ``global_citation_flagged``): engine answer first,
legacy shape as the fallback. A reader that picks one shape on its own reads
history differently from every other surface, and nothing raises -- the global
reasoning sampler did exactly that (legacy rows contributed empty runs) until
this guard registered it.

Two channels, both by AST (a comment or docstring never counts), both closed
inventories keyed by NAME (``module path relative to backend/app`` ->
``qualified function name``), never by line number. A registered name that no
longer reads fails as stale, so each list stays exact.

* SQL: every string literal under ``backend/app`` (plain, implicitly
  concatenated, or an f-string's literal parts) that names ``payload_json``
  and an answer path (``'$.answer…'`` / ``'$.response…'``,
  ``->'answer'`` / ``->>'response'`` …). Each such site is registered as

  - ``BOTH``: checked per literal -- every engine path ``answer.<f>`` is
    followed, in the same literal, by its legacy counterpart (``response.<f>``;
    for ``reasoning_trace`` the top-level ``trace``), and no legacy path comes
    without the engine path before it. That is the ``COALESCE(answer…,
    legacy…)`` order of the Python projections.
  - ``NEW_ONLY``: reads the engine shape with no legacy counterpart, with the
    reason written down.

  The unit is one literal, so a statement must keep ``payload_json`` and its
  path together (every reader today does); a path behind an alias of the
  column is outside what this channel sees.

* Python: in every module that works with global jobs (``_FIXED_SCOPE`` plus
  every module importing one of ``_GLOBAL_MODULES``), every READ (``Load``
  context only -- ``job.answer = …`` and the ``job.status, job.response,
  job.answer = …`` clears in the service are writes) of ``.response``,
  ``<…job>.answer``, ``getattr(x, "response"|"answer")``,
  ``x.get("response"|"answer")`` or ``x["response"|"answer"]``. Outside
  ``models/global_ask.py`` (the projections themselves) each reading function
  is registered as ``BOTH`` (reads the engine shape and the legacy one --
  checked), ``NEW_ONLY`` (engine shape only, no legacy read -- checked, reason
  required), ``PROJECTED`` (reads a value a registered reader already resolved
  from both shapes: an SQL projection's ``answer`` column, or the answer object
  ``_public_turn_row`` chose; reason required) or ``NOT_GLOBAL`` (the object
  read is not a global job: a notebook turn, a notebook answer payload; reason
  required).

A new reader therefore either goes through a projection or is registered
here with its reason. The probes at the bottom pin each detector on synthetic
code, positive and negative, and the floors keep a broken scanner from
passing by finding nothing.
"""
from __future__ import annotations

import ast
import functools
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "backend" / "app"

BOTH = "both"
NEW_ONLY = "new-only"
NOT_GLOBAL = "not-global"
PROJECTED = "projected"

PROJECTION_MODULE = "models/global_ask.py"

# -- SQL channel ---------------------------------------------------------------

# Any answer path next to ``payload_json`` makes a literal an answer reader;
# ``_SQL_READ`` then has to account for every one of them (an unrecognised
# spelling fails as unparsed rather than slipping through).
_ANSWER_PATH = re.compile(
    r"'\$\.(?:answer|response)\b"
    r"|->>?\s*'(?:answer|response)'"
    r"|#>>?\s*'\{\s*(?:answer|response)\b"
    r"|extract_path(?:_text)?\s*\([^)]*'(?:answer|response)'",
)
_SQL_READ = re.compile(
    # SQLite: json_extract(payload_json,'$.answer.answer') / '$.trace'
    r"json_extract\(\s*payload_json\s*,\s*'\$\.(?P<s_shape>answer|response|trace)"
    r"(?:\.(?P<s_field>\w+))?'\s*\)"
    # PostgreSQL: payload_json::jsonb->'answer'->>'answer' / ->'trace'
    r"|payload_json::jsonb\s*->>?\s*'(?P<p_shape>answer|response|trace)'"
    r"(?:\s*->>?\s*'(?P<p_field>\w+)')?",
)
_LEGACY_COUNTERPART = {"reasoning_trace": ("trace", None)}

# Registered SQL readers: {module: {site: (kind, reason)}}.
_SQL_REGISTRY: dict[str, dict[str, tuple[str, str]]] = {
    "repositories/global_ask_store.py": {
        "GlobalAskStore.__init__": (
            BOTH, "the conversation-history projection (``completed_history``): "
                  "answer text, engine shape first"),
    },
    "repositories/postgres/ask_state_store.py": {
        "AskStateStore.recent_user_ask_traces": (
            BOTH, "agent-profile overlay sample: reasoning_trace, else the streamed "
                  "or legacy top-level trace"),
        "AskStateStore.recent_completed_ask_runs": (
            BOTH, "global retrieval-experience sample: reasoning_trace, else a legacy "
                  "row's top-level trace (``global_answer_trace`` order)"),
    },
    "repositories/sqlite/ask_state_store.py": {
        "AskStateStore.recent_user_ask_traces": (
            BOTH, "SQLite twin of the PostgreSQL overlay sample"),
        "AskStateStore.recent_completed_ask_runs": (
            BOTH, "SQLite twin of the PostgreSQL global experience sample"),
    },
}
_SQL_SITE_FLOOR = 5

# -- Python channel ------------------------------------------------------------

_FIXED_SCOPE = frozenset({
    "services/global_ask.py",
    "services/global_citation_check.py",
    "api/mcp_tools/global_ask.py",
    "api/admin_routes.py",
    "api/global_ask_routes.py",
    "repositories/global_ask_store.py",
    "domain/global_ask_attribution.py",
})
_GLOBAL_MODULES = frozenset({
    "app.models.global_ask",
    "app.services.global_ask",
    "app.services.global_citation_check",
    "app.repositories.global_ask_store",
    "app.repositories.global_ask_ports",
    "app.domain.global_ask_attribution",
    "app.api.mcp_tools.global_ask",
})

# Every module in ``_GLOBAL_MODULES`` spells one of these, so a module whose
# source has neither word cannot import one (``_FIXED_SCOPE`` is always read).
_SCOPE_NEEDLES = ("global_ask", "global_citation_check")
assert all(any(n in name for n in _SCOPE_NEEDLES) for name in _GLOBAL_MODULES)

# Registered Python readers: {module: {site: (kind, reason)}}.
_PY_REGISTRY: dict[str, dict[str, tuple[str, str]]] = {
    "services/global_ask.py": {
        "_payload_notebook_ids": (
            BOTH, "stored payload dict: top-level list, else nested in answer, else "
                  "in the legacy response"),
        "_turn_evidence_notebook_ids": (
            BOTH, "share re-authorization: anchors and citations of either shape"),
        "_public_turn_row": (
            BOTH, "public share turn: the stored answer dict, else the legacy one"),
        "GlobalAskService._history": (
            PROJECTED, "conversation history turns from ``completed_history`` (the SQL "
                       "projection's ``answer`` column)"),
    },
    "api/mcp_tools/global_ask.py": {
        "global_view": (
            BOTH, "fields the projections do not carry (answer_id/grounded/"
                  "completeness_notice) from either shape; ``answer_object`` is the "
                  "engine object only, a legacy turn has none to hand over"),
    },
    "services/global_citation_check.py": {
        "global_answer_check": (
            NEW_ONLY, "the terminal citation check only ever ran on engine answers; a "
                      "legacy response row was never checked and keeps its stored text"),
    },
    "repositories/global_ask_store.py": {
        "GlobalAskStore.completed_history": (
            PROJECTED, "the ``answer`` column of ``_history_projection`` (registered SQL, "
                       "both shapes)"),
    },
    "services/conversation_public_view.py": {
        "_turn_body_and_references": (
            PROJECTED, "the text field of the answer object ``_public_turn_row`` already "
                       "chose from either shape (both shapes carry ``answer``)"),
    },
    "api/mcp_tools/ask.py": {
        "notebook_view": (
            NOT_GLOBAL, "a notebook ask's ``AskResponse`` text; global jobs go through "
                        "``mcp_tools/global_ask.global_view``"),
    },
    "repositories/postgres/ask_state_store.py": {
        "AskStateStore.answer_memory_source": (
            NOT_GLOBAL, "a notebook ``answers`` row payload"),
        "AskStateStore.get_conversation": (
            NOT_GLOBAL, "a notebook conversation turn (``AskTurn.response``)"),
    },
    "repositories/sqlite/ask_state_store.py": {
        "AskStateStore.answer_memory_source": (
            NOT_GLOBAL, "a notebook ``answers`` row payload"),
        "AskStateStore.get_conversation": (
            NOT_GLOBAL, "a notebook conversation turn (``AskTurn.response``)"),
    },
}
_PY_SITE_FLOOR = 5


# -- scanner -------------------------------------------------------------------

def _docstring_ids(tree: ast.AST) -> set[int]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                found.add(id(body[0].value))
    return found


def _with_sites(tree: ast.AST):
    """Yield ``(site, node)`` for every node, ``site`` being the dotted chain of
    enclosing classes/functions (``"<module>"`` at module level)."""
    stack = [(child, "") for child in ast.iter_child_nodes(tree)]
    while stack:
        node, chain = stack.pop()
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            chain = f"{chain}.{node.name}" if chain else node.name
        yield chain or "<module>", node
        stack.extend((child, chain) for child in ast.iter_child_nodes(node))


def _string_literals(tree: ast.AST):
    """``(site, text, line)`` for each string literal; an f-string is one literal
    with ``{}`` where its placeholders were."""
    skip = _docstring_ids(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            skip.update(id(part) for part in node.values)
    for site, node in _with_sites(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield site, node.value, node.lineno
        elif isinstance(node, ast.JoinedStr):
            text = "".join(
                part.value if isinstance(part, ast.Constant) else "{}" for part in node.values
            )
            yield site, text, node.lineno


def is_answer_sql(text: str) -> bool:
    return "payload_json" in text and bool(_ANSWER_PATH.search(text))


def sql_reads(text: str) -> list[tuple[int, str, str | None]]:
    """``(offset, shape, field)`` for each recognised payload path in one literal."""
    reads = []
    for match in _SQL_READ.finditer(text):
        shape = match.group("s_shape") or match.group("p_shape")
        field = match.group("s_field") or match.group("p_field")
        reads.append((match.start(), shape, field))
    return reads


def sql_problems(text: str, kind: str) -> list[str]:
    """Why one answer-reading literal does not match its registered ``kind``."""
    reads = sql_reads(text)
    # Blank every recognised read out; an answer path left over is a spelling
    # the field check below cannot see, so it fails here instead of passing.
    remainder = _SQL_READ.sub(lambda match: " " * len(match.group(0)), text)
    if _ANSWER_PATH.search(remainder):
        return ["an answer path in a spelling this guard does not parse; spell it "
                "json_extract(payload_json,'$.answer.<f>') / payload_json::jsonb->'answer'->'<f>'"]
    problems = []
    unmatched_new = []
    for offset, shape, field in reads:
        if shape == "answer":
            legacy = _LEGACY_COUNTERPART.get(field, ("response", field))
            if not any(o > offset and (s, f if s == "response" else None) == legacy
                       for o, s, f in reads):
                unmatched_new.append(field)
        elif shape == "response":
            if not any(o < offset and s == "answer" and f == field for o, s, f in reads):
                problems.append(f"legacy path response.{field} without the engine path "
                                f"answer.{field} before it")
    if kind == BOTH:
        problems += [
            f"answer.{field} has no legacy fallback after it "
            f"({'.'.join(x for x in _LEGACY_COUNTERPART.get(field, ('response', field)) if x)})"
            for field in unmatched_new
        ]
    elif kind == NEW_ONLY:
        if not unmatched_new:
            problems.append("registered new-only but every engine path has its legacy "
                            "fallback: register it as both")
    else:
        problems.append(f"unknown SQL kind {kind!r}")
    return problems


def _imports_global_module(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module in _GLOBAL_MODULES:
                return True
            if any(f"{node.module}.{alias.name}" in _GLOBAL_MODULES for alias in node.names):
                return True
        elif isinstance(node, ast.Import):
            if any(alias.name in _GLOBAL_MODULES for alias in node.names):
                return True
    return False


def _receiver_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _shape_of_read(node: ast.AST) -> str | None:
    """``"new"`` / ``"legacy"`` when ``node`` READS one answer shape, else ``None``."""
    shapes = {"answer": "new", "response": "legacy"}
    if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
        if node.attr == "response":
            return "legacy"
        if node.attr == "answer" and _receiver_name(node.value).lower().endswith("job"):
            return "new"
    elif isinstance(node, ast.Call):
        func, args = node.func, node.args
        key = args[1] if isinstance(func, ast.Name) and func.id == "getattr" and len(args) >= 2 \
            else args[0] if isinstance(func, ast.Attribute) and func.attr == "get" and args \
            else None
        if isinstance(key, ast.Constant) and key.value in shapes:
            return shapes[key.value]
    elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
        if isinstance(node.slice, ast.Constant) and node.slice.value in shapes:
            return shapes[node.slice.value]
    return None


def python_reads(tree: ast.AST) -> dict[str, dict[str, list[int]]]:
    """``{site: {"new"|"legacy": [lines]}}`` for every answer-shape read."""
    found: dict[str, dict[str, list[int]]] = {}
    for site, node in _with_sites(tree):
        shape = _shape_of_read(node)
        if shape:
            found.setdefault(site, {}).setdefault(shape, []).append(node.lineno)
    return found


def python_problems(shapes: set[str], kind: str, reason: str) -> list[str]:
    problems = [] if reason.strip() else ["registered without a reason"]
    if kind == BOTH:
        problems += [f"registered both but never reads the {name} shape"
                     for name in ("new", "legacy") if name not in shapes]
    elif kind == NEW_ONLY:
        if "new" not in shapes:
            problems.append("registered new-only but reads no engine answer")
        if "legacy" in shapes:
            problems.append("registered new-only but also reads the legacy shape: "
                            "register it as both")
    elif kind not in (NOT_GLOBAL, PROJECTED):
        problems.append(f"unknown Python kind {kind!r}")
    return problems


@functools.cache
def _sources() -> tuple[tuple[str, str], ...]:
    return tuple(
        (path.relative_to(APP).as_posix(), path.read_text(encoding="utf-8"))
        for path in sorted(APP.rglob("*.py"))
    )


@functools.cache
def _modules(*needles: str, always: frozenset = frozenset()) -> tuple[tuple[str, ast.AST], ...]:
    """Parsed modules whose source mentions one of ``needles`` (a module that
    does not even spell the word cannot match the channel), plus ``always``;
    the guard parses a few dozen files instead of all of ``backend/app``."""
    return tuple(
        (rel, ast.parse(text)) for rel, text in _sources()
        if rel in always or any(needle in text for needle in needles)
    )


def _sql_inventory():
    sites: dict[tuple[str, str], list[tuple[str, int]]] = {}
    for rel, tree in _modules("payload_json"):
        for site, text, line in _string_literals(tree):
            if is_answer_sql(text):
                sites.setdefault((rel, site), []).append((text, line))
    return sites


def _python_scope() -> dict[str, ast.AST]:
    return {
        rel: tree for rel, tree in _modules(*_SCOPE_NEEDLES, always=_FIXED_SCOPE)
        if rel != PROJECTION_MODULE and (rel in _FIXED_SCOPE or _imports_global_module(tree))
    }


# -- the guard -----------------------------------------------------------------

def test_every_sql_reader_of_a_global_answer_path_is_registered_and_reads_as_registered():
    found = _sql_inventory()
    registered = {(rel, site) for rel, sites in _SQL_REGISTRY.items() for site in sites}
    failures = []
    for (rel, site), literals in sorted(found.items()):
        if (rel, site) not in registered:
            failures.append(
                f"{rel}:{literals[0][1]} {site}: reads a global answer path out of "
                "payload_json -- read both shapes (engine path, then its legacy "
                "fallback) and register it here, or register it new-only with a reason")
            continue
        kind, reason = _SQL_REGISTRY[rel][site]
        if not reason.strip():
            failures.append(f"{rel} {site}: registered without a reason")
        for text, line in literals:
            failures += [f"{rel}:{line} {site}: {problem}" for problem in sql_problems(text, kind)]
    failures += [f"{rel} {site}: registered but no longer reads an answer path (stale)"
                 for rel, site in sorted(registered - set(found))]
    assert not failures, "\n".join(failures)
    # Floor: a scanner that silently stopped matching would pass with nothing.
    assert len(found) >= _SQL_SITE_FLOOR


def test_every_python_reader_of_a_global_answer_shape_goes_through_the_projection_or_is_registered():
    scope = _python_scope()
    missing = sorted(_FIXED_SCOPE - set(scope))
    assert not missing, f"fixed-scope modules not found: {missing}"
    found = {(rel, site): reads for rel, tree in scope.items()
             for site, reads in python_reads(tree).items()}
    registered = {(rel, site) for rel, sites in _PY_REGISTRY.items() for site in sites}
    failures = []
    for (rel, site), reads in sorted(found.items()):
        lines = sorted(line for per_shape in reads.values() for line in per_shape)
        if (rel, site) not in registered:
            failures.append(
                f"{rel}:{lines[0]} {site}: reads a global job's answer/response directly -- "
                "use a global_answer_* projection (app/models/global_ask.py) or register "
                "it here with its reason")
            continue
        kind, reason = _PY_REGISTRY[rel][site]
        failures += [f"{rel}:{lines[0]} {site}: {problem}"
                     for problem in python_problems(set(reads), kind, reason)]
    failures += [f"{rel} {site}: registered but no longer reads an answer shape (stale)"
                 for rel, site in sorted(registered - set(found))]
    assert not failures, "\n".join(failures)
    assert len(found) >= _PY_SITE_FLOOR
    assert len(scope) >= len(_FIXED_SCOPE)


def test_the_projection_module_is_seen_reading_both_shapes():
    """Positive probe on real code: the scanner finds the projections' own
    two-shape reads, so an empty inventory elsewhere means "no reader", not "no
    scanner"."""
    tree = ast.parse((APP / PROJECTION_MODULE).read_text(encoding="utf-8"))
    reads = python_reads(tree)
    for projection in ("global_answer_text", "global_answer_citations", "global_answer_payload",
                       "global_citation_flagged"):
        assert set(reads.get(projection, {})) == {"new", "legacy"}, projection


# -- probes --------------------------------------------------------------------

def _py(source: str) -> dict[str, set[str]]:
    return {site: set(reads) for site, reads in python_reads(ast.parse(source)).items()}


def test_python_probe_writes_are_not_reads():
    assert _py(
        "def f(job, response):\n"
        "    job.answer = response\n"
        "    job.status, job.response, job.answer = 'cancelled', None, None\n"
        "    del job.answer\n"
    ) == {}


def test_python_probe_each_read_form_is_seen():
    assert _py("def f(job):\n    return job.response.answer\n") == {"f": {"legacy"}}
    assert _py("def f(job):\n    return job.answer.answer\n") == {"f": {"new"}}
    assert _py("def f(record_job):\n    return record_job.answer\n") == {"f": {"new"}}
    assert _py("class C:\n    def f(self):\n        return self.job.answer\n") == {"C.f": {"new"}}
    assert _py("def f(job):\n    return getattr(job, 'answer', None)\n") == {"f": {"new"}}
    assert _py("def f(job):\n    return getattr(job, 'response', None)\n") == {"f": {"legacy"}}
    assert _py("def f(p):\n    return p.get('response') or p['answer']\n") == {"f": {"new", "legacy"}}
    assert _py("def f(turn):\n    return turn.response\n") == {"f": {"legacy"}}
    # nested helpers are their own site
    assert _py("def f(job):\n    def g():\n        return job.response\n    return g\n") == {
        "f.g": {"legacy"}}


def test_python_probe_non_reads_stay_quiet():
    # ``.answer`` on something that is not a job (an AskResponse's own text).
    assert _py("def f(answer):\n    return answer.answer\n") == {}
    assert _py("def f(p):\n    return p.get('question'), p['citations']\n") == {}
    assert _py("def f(p):\n    p['answer'] = 1\n") == {}


def test_python_probe_registration_kinds_are_checked():
    assert python_problems({"new", "legacy"}, BOTH, "r") == []
    assert python_problems({"new"}, BOTH, "r") == ["registered both but never reads the legacy shape"]
    assert python_problems({"new"}, NEW_ONLY, "r") == []
    assert python_problems({"new", "legacy"}, NEW_ONLY, "r")
    assert python_problems({"legacy"}, NOT_GLOBAL, "r") == []
    assert python_problems({"legacy"}, NOT_GLOBAL, " ") == ["registered without a reason"]


def test_sql_probe_detection():
    assert is_answer_sql("SELECT json_extract(payload_json,'$.answer.answer') FROM t")
    assert is_answer_sql("SELECT payload_json::jsonb->'response'->>'answer' FROM t")
    assert is_answer_sql("SELECT payload_json::jsonb #>> '{answer,answer}' FROM t")
    assert not is_answer_sql("SELECT json_extract(payload_json,'$.question') FROM t")
    assert not is_answer_sql("SELECT payload_json::jsonb->'trace' FROM t")
    # an answer path on another column is not this guard's business
    assert not is_answer_sql("SELECT json_extract(sections_json,'$.answer') FROM t")


def test_sql_probe_both_requires_the_legacy_fallback_after_the_engine_path():
    new_only = "SELECT json_extract(payload_json,'$.answer.answer') AS a FROM t"
    assert sql_problems(new_only, BOTH)
    assert sql_problems(new_only, NEW_ONLY) == []
    both = ("SELECT COALESCE(json_extract(payload_json,'$.answer.answer'), "
            "json_extract(payload_json,'$.response.answer')) AS a FROM t")
    assert sql_problems(both, BOTH) == []
    assert sql_problems(both, NEW_ONLY)  # misregistered: it reads both
    reversed_order = ("SELECT COALESCE(json_extract(payload_json,'$.response.answer'), "
                      "json_extract(payload_json,'$.answer.answer')) AS a FROM t")
    assert sql_problems(reversed_order, BOTH)
    other_field = ("SELECT COALESCE(json_extract(payload_json,'$.answer.answer'), "
                   "json_extract(payload_json,'$.response.citations')) AS a FROM t")
    assert sql_problems(other_field, BOTH)


def test_sql_probe_trace_falls_back_to_the_top_level_trace():
    pg_new_only = ("COALESCE(payload_json::jsonb->'answer'->'reasoning_trace', "
                   "'[]'::jsonb) AS trace FROM global_ask_jobs")
    assert sql_problems(pg_new_only, BOTH)
    pg_both = ("COALESCE(payload_json::jsonb->'answer'->'reasoning_trace', "
               "payload_json::jsonb->'trace', '[]'::jsonb) AS trace FROM global_ask_jobs")
    assert sql_problems(pg_both, BOTH) == []
    sqlite_both = ("COALESCE(json_extract(payload_json,'$.answer.reasoning_trace'), "
                   "json_extract(payload_json,'$.trace'), '[]') AS trace FROM global_ask_jobs")
    assert sql_problems(sqlite_both, BOTH) == []
    sqlite_new_only = ("COALESCE(json_extract(payload_json,'$.answer.reasoning_trace'), "
                       "'[]') AS trace FROM global_ask_jobs")
    assert sql_problems(sqlite_new_only, BOTH)


def test_sql_probe_an_unparsed_spelling_fails_instead_of_slipping_through():
    assert sql_problems("SELECT payload_json::jsonb #>> '{answer,answer}' FROM t", BOTH)
    assert sql_problems("SELECT payload_json::jsonb #>> '{answer,answer}' FROM t", NEW_ONLY)


def test_sql_probe_literals_are_collected_from_code_not_docstrings():
    tree = ast.parse(
        'def f(db):\n'
        '    """json_extract(payload_json,\'$.answer.answer\')"""\n'
        '    return db.execute(f"SELECT json_extract(payload_json,\'$.answer.answer\') '
        'FROM {db}")\n'
    )
    literals = [(site, text) for site, text, _line in _string_literals(tree) if is_answer_sql(text)]
    assert literals == [("f", "SELECT json_extract(payload_json,'$.answer.answer') FROM {}")]
