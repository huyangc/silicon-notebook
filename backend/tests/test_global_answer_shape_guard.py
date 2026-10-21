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
reasoning sampler's SQL read the engine shape only until this guard registered
it (no real legacy row is a reasoning run, so it had no visible effect yet).

Two channels, both by AST (a comment or docstring never counts), both closed
inventories keyed by NAME (``module path relative to backend/app`` ->
``qualified function name``), never by line number. A registered name that no
longer reads fails as stale, so each list stays exact.

* SQL: every string literal under ``backend/app`` (plain, implicitly
  concatenated, or an f-string's literal parts) that names ``payload_json``
  and an answer path (``'$.answer…'`` / ``'$.response…'``,
  ``->'answer'`` / ``->>'response'`` …) -- and, in a module that names the
  ``global_ask_jobs`` table, every literal with an answer path at all, so a
  column spliced in through a placeholder (``json_extract({col},'$.answer…')``)
  fails as unparsed instead of passing unseen. Each site is registered as

  - ``BOTH``: checked per literal -- every engine path ``answer.<f>`` is
    followed, in the same literal, by its legacy counterpart (``response.<f>``;
    for ``reasoning_trace`` the top-level ``trace``), and no legacy path comes
    without the engine path before it. That is the ``COALESCE(answer…,
    legacy…)`` order of the Python projections.
  - ``NEW_ONLY``: reads the engine shape with no legacy counterpart, with the
    reason written down.

  The unit is one literal, so a statement must keep ``payload_json`` and its
  path together (every reader today does). Outside a ``global_ask_jobs``
  module, a path behind an alias or a spliced column name is not seen.

* Python: in every module that works with global jobs (``_FIXED_SCOPE`` plus
  every module importing one of ``_GLOBAL_MODULES``, relative imports
  resolved), every READ (``Load`` context only -- ``job.answer = …`` and the
  ``job.status, job.response, job.answer = …`` clears in the service are
  writes) of ``.response``, ``X.answer`` on a job receiver (a name ending in
  ``job``, or a ``[…"job"]`` subscript), ``X.answer.<attr>`` for any ``attr``
  a ``str`` does not have (``AskResponse.answer`` is text, so such an ``X`` is
  a job), ``getattr(x, "response"|"answer")``, ``x.get("response"|"answer")``
  or ``x["response"|"answer"]``. Outside ``models/global_ask.py`` (the
  projections themselves) each reading function is registered with its exact
  READ SIGNATURE -- the sorted ``shape: source`` of every read in it -- so a
  read added to an already-registered function fails until the registration
  is updated, and as

  - ``BOTH``: reads the engine shape and the legacy one (checked);
  - ``NEW_ONLY``: engine shape only, no legacy read (checked, reason required);
  - ``PROJECTED``: reads a value a registered reader already resolved from both
    shapes (an SQL projection's ``answer`` column, or the answer object
    ``_public_turn_row`` chose; reason required);
  - ``NOT_GLOBAL``: the object read is not a global job (a notebook turn or
    answer payload; reason required).

  A ``PROJECTED`` or ``NOT_GLOBAL`` site may not hold a job-shaped read (an
  ``.answer``/``.response`` on a job receiver, a chained ``X.answer.<attr>``,
  or a ``getattr`` on a job receiver): those are a global job by
  construction, so the two reason-only kinds cannot be used to file one away.

  Not covered: a ``getattr`` whose attribute name is computed, and a module
  that handles global jobs without importing any global-ask module or naming
  it in ``_FIXED_SCOPE``.

A new reader therefore either goes through a projection or is registered
here with its reason and read signature. The probes at the bottom pin each
detector on synthetic code, positive and negative. The site floors stay
although a broken scanner would already turn every registration stale: they
also catch the registries themselves being emptied, at the price of being
lowered when a reader is legitimately removed.
"""
from __future__ import annotations

import ast
import functools
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.architecture.source_trees import read_source_tree

pytestmark = pytest.mark.xdist_group(name="source_scans_core")

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
_GLOBAL_TABLE = "global_ask_jobs"

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

# Registered Python readers: {module: {site: (kind, reason, read signature)}}.
# The signature is ``sorted(f"{shape}: {ast.unparse(read)}")`` over the site's
# reads; the failure message prints the current one to paste after review.
_PY_REGISTRY: dict[str, dict[str, tuple[str, str, tuple[str, ...]]]] = {
    "services/global_ask.py": {
        "_payload_notebook_ids": (
            BOTH, "stored payload dict: top-level list, else nested in answer, else "
                  "in the legacy response",
            ("legacy: row.get('response')", "new: row.get('answer')")),
        "_turn_evidence_notebook_ids": (
            BOTH, "share re-authorization: anchors and citations of either shape",
            ("legacy: row.get('response')", "new: row.get('answer')")),
        "_public_turn_row": (
            BOTH, "public share turn: the stored answer dict, else the legacy one",
            ("legacy: payload.get('response')", "new: payload.get('answer')")),
        "GlobalAskService._history": (
            PROJECTED, "conversation history turns from ``completed_history`` (the SQL "
                       "projection's ``answer`` column)",
            ("new: turn['answer']",)),
    },
    "api/mcp_tools/global_ask.py": {
        "global_view": (
            BOTH, "fields the projections do not carry (answer_id/grounded/"
                  "completeness_notice) from either shape; ``answer_object`` is the "
                  "engine object only, a legacy turn has none to hand over",
            ("legacy: data.get('response')", "new: data.get('answer')",
             "new: getattr(job, 'answer', None)")),
    },
    "services/global_citation_check.py": {
        "global_answer_check": (
            NEW_ONLY, "the terminal citation check only ever ran on engine answers; a "
                      "legacy response row was never checked and keeps its stored text",
            ("new: getattr(job, 'answer', None)", "new: payload.get('answer')")),
    },
    "repositories/global_ask_store.py": {
        "GlobalAskStore.completed_history": (
            PROJECTED, "the ``answer`` column of ``_history_projection`` (registered SQL, "
                       "both shapes)",
            ("new: row['answer']", "new: row['answer']")),
    },
    "services/conversation_public_view.py": {
        "_turn_body_and_references": (
            PROJECTED, "the text field of the answer object ``_public_turn_row`` already "
                       "chose from either shape (both shapes carry ``answer``)",
            ("new: row.get('answer')",)),
    },
    "api/mcp_tools/ask_pages.py": {
        "answer_page": (
            PROJECTED, "pages the normalised view ``global_view``/``notebook_view`` built, "
                       "whose ``answer`` is already the projected text, and its own "
                       "budgeted copy of that page",
            ("new: packed.get('answer')", "new: view.get('answer')")),
    },
    "api/mcp_tools/ask.py": {
        "notebook_view": (
            NOT_GLOBAL, "a notebook ask's ``AskResponse`` text; global jobs go through "
                        "``mcp_tools/global_ask.global_view``",
            ("new: getattr(response, 'answer', '')",)),
        "_deliver": (
            PROJECTED, "previews the normalised view ``global_view``/``notebook_view`` built "
                       "(the same view ``answer_page`` pages), whose ``answer`` is already "
                       "the projected text",
            ("new: view.get('answer')",)),
    },
    "api/mcp_tools/conversations.py": {
        "turn_page": (
            PROJECTED, "pages one turn of the normalised view ``global_view``/"
                       "``notebook_view`` built, whose ``answer`` is already the "
                       "projected text, and its own budgeted copy of that page",
            ("new: packed.get('answer')", "new: view.get('answer')")),
        "_notebook_turn": (
            NOT_GLOBAL, "a notebook conversation turn (``ConversationTurn.response``); "
                        "a global turn goes through ``_global_turn`` and "
                        "``mcp_tools/global_ask.global_view``",
            ("legacy: turn.response", "legacy: turn.response")),
    },
    "repositories/postgres/ask_state_store.py": {
        "AskStateStore.answer_memory_source": (
            NOT_GLOBAL, "a notebook ``answers`` row payload",
            ("new: payload.get('answer')",)),
        "AskStateStore.get_conversation": (
            NOT_GLOBAL, "a notebook conversation turn (``AskTurn.response``)",
            ("legacy: turns[-1].response",)),
    },
    "repositories/sqlite/ask_state_store.py": {
        "AskStateStore.answer_memory_source": (
            NOT_GLOBAL, "a notebook ``answers`` row payload",
            ("new: payload.get('answer')",)),
        "AskStateStore.get_conversation": (
            NOT_GLOBAL, "a notebook conversation turn (``AskTurn.response``)",
            ("legacy: turns[-1].response",)),
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
    """Yield ``(site, node, parent)`` for every node, ``site`` being the dotted
    chain of enclosing classes/functions (``"<module>"`` at module level)."""
    stack = [(child, "", tree) for child in ast.iter_child_nodes(tree)]
    while stack:
        node, chain, parent = stack.pop()
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            chain = f"{chain}.{node.name}" if chain else node.name
        yield chain or "<module>", node, parent
        stack.extend((child, chain, node) for child in ast.iter_child_nodes(node))


def _string_literals(tree: ast.AST):
    """``(site, text, node)`` for each string literal; an f-string is one literal
    with ``{}`` where its placeholders were."""
    skip = _docstring_ids(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            skip.update(id(part) for part in node.values)
    for site, node, _parent in _with_sites(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield site, node.value, node
        elif isinstance(node, ast.JoinedStr):
            text = "".join(
                part.value if isinstance(part, ast.Constant) else "{}" for part in node.values
            )
            yield site, text, node


def is_answer_sql(text: str, *, global_table_module: bool = False) -> bool:
    if not _ANSWER_PATH.search(text):
        return False
    return global_table_module or "payload_json" in text


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


def _module_package(rel: str) -> list[str]:
    parts = ["app", *rel[:-len(".py")].split("/")]
    return parts[:-1]  # a module's package; ``pkg/__init__`` -> ``pkg``


def _imports_global_module(tree: ast.AST, rel: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                package = _module_package(rel)
                base = package[:len(package) - (node.level - 1)]
                module = ".".join([*base, *([node.module] if node.module else [])])
            else:
                module = node.module or ""
            if module in _GLOBAL_MODULES:
                return True
            if any(f"{module}.{alias.name}" in _GLOBAL_MODULES for alias in node.names):
                return True
        elif isinstance(node, ast.Import):
            if any(alias.name in _GLOBAL_MODULES for alias in node.names):
                return True
    return False


_STR_ATTRIBUTES = frozenset(dir(str))


def _is_job_receiver(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        name = node.id
    elif isinstance(node, ast.Attribute):
        name = node.attr
    elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) \
            and isinstance(node.slice.value, str):
        name = node.slice.value
    else:
        return False
    return name.lower().endswith("job")


@dataclass(frozen=True)
class Read:
    shape: str        # "new" | "legacy"
    job_shaped: bool  # unambiguously a read on a global job
    node: ast.AST

    @property
    def signature(self) -> str:
        return f"{self.shape}: {ast.unparse(self.node)}"


def _read_of(node: ast.AST, parent: ast.AST) -> Read | None:
    """The answer-shape READ ``node`` is, or ``None``."""
    shapes = {"answer": "new", "response": "legacy"}
    if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
        if node.attr == "response":
            return Read("legacy", _is_job_receiver(node.value), node)
        if node.attr == "answer":
            if _is_job_receiver(node.value):
                return Read("new", True, node)
            chained = isinstance(parent, ast.Attribute) and parent.value is node
            if chained and parent.attr not in _STR_ATTRIBUTES:
                return Read("new", True, node)
    elif isinstance(node, ast.Call):
        func, args = node.func, node.args
        if isinstance(func, ast.Name) and func.id == "getattr" and len(args) >= 2:
            key, job_shaped = args[1], _is_job_receiver(args[0])
        elif isinstance(func, ast.Attribute) and func.attr == "get" and args:
            key, job_shaped = args[0], False
        else:
            return None
        if isinstance(key, ast.Constant) and key.value in shapes:
            return Read(shapes[key.value], job_shaped, node)
    elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
        if isinstance(node.slice, ast.Constant) and node.slice.value in shapes:
            return Read(shapes[node.slice.value], False, node)
    return None


def python_reads(tree: ast.AST) -> dict[str, list[Read]]:
    """``{site: [Read, …]}`` for every answer-shape read."""
    found: dict[str, list[Read]] = {}
    for site, node, parent in _with_sites(tree):
        read = _read_of(node, parent)
        if read is not None:
            found.setdefault(site, []).append(read)
    return found


def read_signature(reads: list[Read]) -> tuple[str, ...]:
    return tuple(sorted(read.signature for read in reads))


def python_problems(reads: list[Read], kind: str, reason: str,
                    signature: tuple[str, ...]) -> list[str]:
    shapes = {read.shape for read in reads}
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
    elif kind in (NOT_GLOBAL, PROJECTED):
        problems += [f"a {kind} site reads a global job directly: {read.signature}"
                     for read in reads if read.job_shaped]
    else:
        problems.append(f"unknown Python kind {kind!r}")
    actual = read_signature(reads)
    if actual != tuple(sorted(signature)):
        problems.append(
            f"its reads changed -- review each and re-register; now: {actual!r}")
    return problems


@functools.cache
def _sources() -> tuple[tuple[str, str], ...]:
    return tuple(
        (path.relative_to(APP).as_posix(), path.read_text(encoding="utf-8"))
        for path in sorted(APP.rglob("*.py"))
    )


@functools.cache
def _modules(*needles: str, always: frozenset = frozenset()) -> tuple[tuple[str, str, ast.AST], ...]:
    """``(rel, source, tree)`` of the modules whose source mentions one of
    ``needles`` (a module that does not even spell the word cannot match the
    channel), plus ``always``; the guard parses a few dozen files, not all."""
    return tuple(
        (rel, text, read_source_tree(APP / rel)) for rel, text in _sources()
        if rel in always or any(needle in text for needle in needles)
    )


def _sql_inventory():
    sites: dict[tuple[str, str], list[tuple[str, ast.AST]]] = {}
    for rel, source, tree in _modules("payload_json", _GLOBAL_TABLE):
        strict = _GLOBAL_TABLE in source
        for site, text, node in _string_literals(tree):
            if is_answer_sql(text, global_table_module=strict):
                sites.setdefault((rel, site), []).append((text, node))
    return sites


def _python_scope() -> dict[str, ast.AST]:
    return {
        rel: tree for rel, _source, tree in _modules(*_SCOPE_NEEDLES, always=_FIXED_SCOPE)
        if rel != PROJECTION_MODULE
        and (rel in _FIXED_SCOPE or _imports_global_module(tree, rel))
    }


# -- the guard -----------------------------------------------------------------

def test_every_sql_reader_of_a_global_answer_path_is_registered_and_reads_as_registered():
    found = _sql_inventory()
    registered = {(rel, site) for rel, sites in _SQL_REGISTRY.items() for site in sites}
    violations = []
    for (rel, site), literals in sorted(found.items()):
        if (rel, site) not in registered:
            violations.append(
                f"{rel}:{literals[0][1].lineno} {site}: reads a global answer path out of "
                "payload_json -- read both shapes (engine path, then its legacy "
                "fallback) and register it here, or register it new-only with a reason")
            continue
        kind, reason = _SQL_REGISTRY[rel][site]
        if not reason.strip():
            violations.append(f"{rel} {site}: registered without a reason")
        for text, node in literals:
            violations.extend(f"{rel}:{node.lineno} {site}: {problem}"
                              for problem in sql_problems(text, kind))
    violations.extend(f"{rel} {site}: registered but no longer reads an answer path (stale)"
                      for rel, site in sorted(registered - set(found)))
    assert not violations, "\n".join(violations)
    # Floor: an emptied registry plus a scanner that matches nothing would pass.
    assert len(found) >= _SQL_SITE_FLOOR


def test_every_python_reader_of_a_global_answer_shape_goes_through_the_projection_or_is_registered():
    scope = _python_scope()
    missing = sorted(_FIXED_SCOPE - set(scope))
    assert not missing, f"fixed-scope modules not found: {missing}"
    found = {(rel, site): reads for rel, tree in scope.items()
             for site, reads in python_reads(tree).items()}
    registered = {(rel, site) for rel, sites in _PY_REGISTRY.items() for site in sites}
    violations = []
    for (rel, site), reads in sorted(found.items()):
        if (rel, site) not in registered:
            violations.append(
                f"{rel}:{reads[0].node.lineno} {site}: reads a global job's answer/response "
                "directly -- use a global_answer_* projection (app/models/global_ask.py) or "
                f"register it here with its reason; reads: {read_signature(reads)!r}")
            continue
        kind, reason, signature = _PY_REGISTRY[rel][site]
        violations.extend(f"{rel}:{reads[0].node.lineno} {site}: {problem}"
                          for problem in python_problems(reads, kind, reason, signature))
    violations.extend(f"{rel} {site}: registered but no longer reads an answer shape (stale)"
                      for rel, site in sorted(registered - set(found)))
    assert not violations, "\n".join(violations)
    assert len(found) >= _PY_SITE_FLOOR
    assert len(scope) >= len(_FIXED_SCOPE)


def test_the_projection_module_is_seen_reading_both_shapes():
    """Positive probe on real code: the scanner finds the projections' own
    two-shape reads, so an empty inventory elsewhere means "no reader", not "no
    scanner"."""
    tree = read_source_tree(APP / PROJECTION_MODULE)
    reads = python_reads(tree)
    for projection in ("global_answer_text", "global_answer_citations", "global_answer_payload",
                       "global_citation_flagged"):
        assert {read.shape for read in reads.get(projection, [])} == {"new", "legacy"}, projection


def test_relative_importers_of_global_modules_are_in_scope():
    """Real-code probe for the relative-import resolution: these two MCP modules
    reach the global tools only through ``from .global_ask import …``."""
    scope = _python_scope()
    assert {"api/mcp_tools/ask_pages.py", "api/mcp_tools/citations.py"} <= set(scope)


# -- probes --------------------------------------------------------------------

def _py(source: str) -> dict[str, set[str]]:
    return {site: {read.shape for read in reads}
            for site, reads in python_reads(ast.parse(source)).items()}


def _site_reads(source: str, site: str = "f") -> list[Read]:
    return python_reads(ast.parse(source))[site]


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


def test_python_probe_job_receivers_beyond_the_name():
    """V3/V5: the receiver is a ``["job"]`` subscript, or any receiver whose
    ``.answer`` is used as an object (``AskResponse.answer`` is text)."""
    reads = _site_reads("def f(record):\n    return record['job'].answer.answered_at\n")
    assert [(read.shape, read.job_shaped) for read in reads] == [("new", True)]
    reads = _site_reads("def f(record):\n    entry = record['job']\n"
                        "    return entry.answer.model_dump()\n")
    assert [(read.shape, read.job_shaped) for read in reads] == [("new", True)]
    # a string method on the answer TEXT is not a job read
    assert _py("def f(response):\n    return response.answer.strip()\n") == {}
    assert _py("def f(answer):\n    return answer.answer\n") == {}


def test_python_probe_non_reads_stay_quiet():
    assert _py("def f(p):\n    return p.get('question'), p['citations']\n") == {}
    assert _py("def f(p):\n    p['answer'] = 1\n") == {}


def test_python_probe_registration_kinds_are_checked():
    both = _site_reads("def f(p):\n    return p.get('answer') or p.get('response')\n")
    both_sig = read_signature(both)
    assert python_problems(both, BOTH, "r", both_sig) == []
    new = _site_reads("def f(p):\n    return p.get('answer')\n")
    new_sig = read_signature(new)
    assert python_problems(new, BOTH, "r", new_sig) == [
        "registered both but never reads the legacy shape"]
    assert python_problems(new, NEW_ONLY, "r", new_sig) == []
    assert python_problems(both, NEW_ONLY, "r", both_sig)
    legacy = _site_reads("def f(turn):\n    return turn.response\n")
    assert python_problems(legacy, NOT_GLOBAL, "r", read_signature(legacy)) == []
    assert python_problems(legacy, NOT_GLOBAL, " ", read_signature(legacy)) == [
        "registered without a reason"]


def test_python_probe_reason_only_kinds_refuse_a_job_read():
    reads = _site_reads("def f(turns, job):\n    return turns[0]['answer'], job.answer\n")
    for kind in (PROJECTED, NOT_GLOBAL):
        problems = python_problems(reads, kind, "r", read_signature(reads))
        assert problems == [f"a {kind} site reads a global job directly: new: job.answer"]
    reads = _site_reads("def f(job):\n    return getattr(job, 'response', None)\n")
    assert python_problems(reads, NOT_GLOBAL, "r", read_signature(reads))
    # ``getattr`` on a notebook response is not job-shaped
    reads = _site_reads("def f(response):\n    return getattr(response, 'answer', '')\n")
    assert python_problems(reads, NOT_GLOBAL, "r", read_signature(reads)) == []


def test_python_probe_a_read_added_to_a_registered_site_fails():
    """V1/V2: moving a one-shape read into an already-registered function no
    longer hides it -- the registered read signature no longer matches."""
    registered = read_signature(_site_reads(
        "def f(row):\n    return row.get('answer'), row.get('response')\n"))
    grown = _site_reads(
        "def f(row):\n    trace = (row.get('answer') or {}).get('reasoning_trace')\n"
        "    return row.get('answer'), row.get('response'), trace\n")
    problems = python_problems(grown, BOTH, "r", registered)
    assert len(problems) == 1 and problems[0].startswith("its reads changed")
    history = read_signature(_site_reads("def f(turn):\n    return turn['answer']\n"))
    grown = _site_reads("def f(turn, last_job):\n"
                        "    return turn['answer'], last_job.answer.answer\n")
    problems = python_problems(grown, PROJECTED, "r", history)
    assert problems == [
        "a projected site reads a global job directly: new: last_job.answer",
        "its reads changed -- review each and re-register; now: "
        + repr(("new: last_job.answer", "new: turn['answer']")),
    ]


def test_python_probe_relative_imports_are_resolved():
    """V11: ``from .global_ask import x`` / ``from . import global_ask`` /
    ``from ...services import global_ask`` reach a global module."""
    def imports(source, rel="api/mcp_tools/citations.py"):
        return _imports_global_module(ast.parse(source), rel)

    assert imports("from .global_ask import global_view\n")
    assert imports("from . import global_ask\n")
    assert imports("from ...services import global_ask\n")
    assert imports("from ...models.global_ask import GlobalAskJob\n")
    assert not imports("from ._shared import RESULT_LIMIT\n")
    assert not imports("from ...services import ask_service\n")
    # ``..`` from api/mcp_tools is app.api: app.api.services is not a global module
    assert not imports("from ..services import global_ask\n")
    # a package's ``__init__`` resolves against the package itself
    assert imports("from .global_ask import x\n", rel="api/mcp_tools/__init__.py")
    assert not imports("from .global_ask import x\n", rel="api/__init__.py")
    assert imports("from .mcp_tools.global_ask import x\n", rel="api/__init__.py")


def test_sql_probe_detection():
    assert is_answer_sql("SELECT json_extract(payload_json,'$.answer.answer') FROM t")
    assert is_answer_sql("SELECT payload_json::jsonb->'response'->>'answer' FROM t")
    assert is_answer_sql("SELECT payload_json::jsonb #>> '{answer,answer}' FROM t")
    assert not is_answer_sql("SELECT json_extract(payload_json,'$.question') FROM t")
    assert not is_answer_sql("SELECT payload_json::jsonb->'trace' FROM t")
    # an answer path on another column is not this guard's business ...
    assert not is_answer_sql("SELECT json_extract(sections_json,'$.answer') FROM t")
    # ... unless the module queries global_ask_jobs: a spliced column (V8) is
    # then seen, and fails as unparsed.
    spliced = "SELECT json_extract({},'$.answer.answer') FROM global_ask_jobs"
    assert is_answer_sql(spliced, global_table_module=True)
    assert sql_problems(spliced, BOTH) and sql_problems(spliced, NEW_ONLY)


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
    literals = [(site, text) for site, text, _node in _string_literals(tree) if is_answer_sql(text)]
    assert literals == [("f", "SELECT json_extract(payload_json,'$.answer.answer') FROM {}")]
