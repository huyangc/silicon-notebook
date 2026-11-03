"""Closed inventory of Memory SQL sites, including drafts' explicit exceptions.

The inventory is by module/qualified name, never source position. Every string
literal mentioning memory_items belongs to an inventoried function or a module/
class constant. Docstrings and comments are prose. Generic count SQL and catalog-
driven interpolated readers are included too. Behaviour is covered by the paired
reader/count/adoption cases; this guard prevents a new site from escaping review.
"""
from __future__ import annotations

import ast
import copy
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.architecture.source_trees import app_source_trees

pytestmark = pytest.mark.xdist_group(name="source_scans_core")
ROOT = Path(__file__).resolve().parents[1] / "app" / "repositories"
UNADOPTED_EXCLUDED = "unadopted_excluded"
ALL_STATES_EXEMPT = "all_states_exempt"
CONFIRMED_ONLY = "confirmed_only"
WRITE_ONLY = "write_only"
NOT_A_PERSONAL_READER = "not_a_personal_reader"
CATEGORIES = frozenset({
    UNADOPTED_EXCLUDED, ALL_STATES_EXEMPT, CONFIRMED_ONLY,
    WRITE_ONLY, NOT_A_PERSONAL_READER,
})
TABLE = re.compile(r"\bmemory_items\b", re.I)
INTERPOLATED_READ = re.compile(r"\b(?:FROM|JOIN)\s*(?:\{\}|$)", re.I | re.M)


@dataclass(frozen=True)
class Site:
    path: str
    name: str
    text: str
    names: frozenset[str]


# Each explicit registration carries the reason drafts are safe here. Wildcards
# mean the same reviewed method in the two adapters, not arbitrary new methods.
REGISTRY: dict[tuple[str, str], tuple[str, str]] = {}
# These readers delegate their actual predicate. Track the fragment itself
# even though it mentions an alias rather than the physical table name.
EXPORT_HELPERS = {
    "MemoryStore._export_page_on": ("_export_where",),
    "MemoryStore.memory_export_snapshot": ("_export_where", "_export_page_on"),
}
FRAGMENT_SITES = frozenset(
    (f"{backend}/memory_store.py", "MemoryStore._export_where")
    for backend in ("sqlite", "postgres")
)


def _register(path: str, names: str, category: str, reason: str) -> None:
    for name in names.split():
        assert (path, name) not in REGISTRY
        REGISTRY[path, name] = category, reason


_register("*/memory_store.py", """
MemoryStore.notebook_content_overview MemoryStore.answer_memory_links
MemoryStore.group_exit_memory_preview
""", UNADOPTED_EXCLUDED, "Public counts, saved-answer flags and export content exclude unadopted suggestions through the shared fragment.")
_register("*/memory_store.py", "MemoryStore._export_page_on MemoryStore.memory_export_snapshot",
          UNADOPTED_EXCLUDED, "Export content and its total use _export_where's default exclusion; only the separately labelled omitted-suggestion count uses its explicit unadopted=True branch. Content pages always remain excluded.")
_register("*/memory_store.py", "MemoryStore._export_where", UNADOPTED_EXCLUDED,
          "Explicit export predicate fragment: default content scope excludes unadopted suggestions; the inverse branch serves only the separate omitted count, never an export page.")
_register("*/query_store.py", """
QueryStore.memory_counts_by_owner_notebook QueryStore.list_user_usage
QueryStore.pending_delete_notices
""", UNADOPTED_EXCLUDED, "Usage and deletion counts subtract the partial-index unadopted count using the shared fragment.")
_register("*/memory_store.py", "MemoryStore._validate_evidence_ref_on", CONFIRMED_ONLY,
          "A submitted Memory reference is trusted only while confirmed and owned by the actor.")
_register("*/memory_store.py", """
MemoryStore.delete_window_transferable_ids MemoryStore.delete_window_transferable_count
""", CONFIRMED_ONLY, "The shared _transferable_where helper restricts delete-window transfers to confirmed memories.")
_register("*/query_store.py", "QueryStore.notebook_has_confirmed_memory", CONFIRMED_ONLY,
          "The existence probe explicitly requires confirmed status.")
_register("*/memory_store.py", """
MemoryStore.memory_for_user MemoryStore.memory_row_exists MemoryStore.memory_by_answer
MemoryStore.answer_memory_link_rows MemoryStore.memory_by_agent_request
MemoryStore.owned_memory_refs MemoryStore._exit_memory_snapshot_on MemoryStore._exit_memory_counts
MemoryStore.list_memories
MemoryStore.foreign_memory_sources_for_source_ids_sql
""", ALL_STATES_EXEMPT, "Actor-owned lifecycle/review reads, answer states and exit id snapshots must include drafts; this does not exempt content export.")
_register("*/query_store.py", "QueryStore.count_rows", ALL_STATES_EXEMPT,
          "Generic storage count primitive counts the requested table, not the personal Memory projection.")
_register("*/memory_store.py", "MemoryStore.memory_retrieval_rows", ALL_STATES_EXEMPT,
          "Explicit-status retrieval primitive also serves agent candidates; formal MemoryService retrieval selects confirmed, covered by paired reader tests.")
_register("*/memory_suggest_store.py", """
MemorySuggestStore.unadopted_candidate_count MemorySuggestStore.answers_with_memory
MemorySuggestStore.suggest_neighbors
""", ALL_STATES_EXEMPT, "Owner-bound suggestion backlog, fingerprint deduplication and neighbours deliberately see all relevant draft/declined states.")
_register("*/notebook_delete_job_store.py", "NotebookDeleteJobStore.delete_notice_audience",
          ALL_STATES_EXEMPT, "Deletion notice audience includes every affected Memory owner, including owners of drafts.")
_register("postgres/search.py", """
memory_candidate_ids memory_match_count memory_page_candidate_ids _memory_match_predicates
""", ALL_STATES_EXEMPT, "Owner/status-scoped lexical primitives also serve the draft review page; the caller chooses the formal confirmed-only scope.")
_register("*/memory_store.py", """
MemoryStore._insert_memory_on MemoryStore._save_answer MemoryStore._write_mutation_on
MemoryStore._lock_memory_aggregate_on MemoryStore.adopt_suggestion_from_answer
MemoryStore.propose_promotion_on MemoryStore.promotion_rows_on
MemoryStore.record_promotion_decision_on MemoryStore.update_fields MemoryStore.transition
MemoryStore.delete_memory_if_unchanged MemoryStore._hard_delete_on
MemoryStore._finish_memory_exit_on MemoryStore.embedding_revision MemoryStore.replace_embedding
MemoryStore._lock_memory_revision_on MemoryStore.mark_embedding_failed
MemoryStore.create_copy_with_initial_revision MemoryStore._lock_copy_source_on
""", WRITE_ONLY, "Transactional lifecycle, projection and cleanup helpers read/lock rows to write them; they are not independent personal readers.")
_register("postgres/memory_store.py", "MemoryStore._append_revision_on MemoryStore.memory_rows_lock_sql",
          WRITE_ONLY, "PostgreSQL row-lock statements protect revision/aggregate writes.")
_register("*/memory_suggest_store.py", "MemorySuggestStore.expire_overdue", WRITE_ONLY,
          "Expiry updates open suggestions, records revisions and returns content-free event identities.")
_register("*/notebook_delete_job_store.py", "NotebookDeleteJobStore.delete_memory_items_page",
          WRITE_ONLY, "Terminal notebook deletion removes every Memory state.")
_register("*/governance_store.py", """
GovernanceStore.approve_memory_promotion_in_transaction GovernanceStore.merge_followers_on
GovernanceStore.apply_merge_follow_actions_on GovernanceStore.heal_merged_away_candidates
""", WRITE_ONLY, "Promotion and merge-recovery transactions inspect linked memories to mutate approved knowledge, not expose draft Memory content.")
_register("*/memory_store.py", "_ORPHAN_MEMORY_SOURCE_WHERE", NOT_A_PERSONAL_READER,
          "Projection cleanup identifies orphan synthetic sources; no Memory content is returned.")
_register("*/memory_sql.py", "memory_source_readable _foreign_memory_excluded foreign_memory_in_notebook_excluded",
          NOT_A_PERSONAL_READER, "Derived-source ownership fragments protect KG/chunk views; unadopted suggestions never create a synthetic source.")
_register("*/chunk_store.py", "ChunkStore.question_index_rows ChunkStore.retrieval_contribution_rows",
          NOT_A_PERSONAL_READER, "Derived chunk/index reads recheck source ownership; drafts have no derived source or chunk projection.")
_register("postgres/search.py", "_SEARCH_TARGETS lexical_candidate_sql _candidate_rows_for_terms",
          NOT_A_PERSONAL_READER, "Reviewed lexical SQL catalog/builders select structural targets; Memory content is read only through the scoped Memory primitives.")
_register("postgres/query_store.py", "_COUNT_IDENTIFIERS", NOT_A_PERSONAL_READER,
          "Identifier allowlist for the generic count primitive; no row content.")
_register("postgres/hotpath_indexes.py", "HOTPATH_INDEX_SPECS", NOT_A_PERSONAL_READER,
          "Index DDL catalog describes storage/performance only.")
_register("postgres/schema_manifest.py", "POSTGRES_BUSINESS_TABLES POSTGRES_JSON_COLUMNS", NOT_A_PERSONAL_READER,
          "Schema metadata for migration/parity validation, not personal retrieval.")
_register("sqlite/migrations.py", """
SqliteMigrator._migration_13 SqliteMigrator._migration_61
SqliteMigrator._migration_87 SqliteMigrator._migration_88 SqliteMigrator._migration_106
""", NOT_A_PERSONAL_READER, "Sealed migrations and FTS rebuild handle storage states without exposing a reader projection.")
_register("user_references.py", "USER_REFERENCE_COLUMNS", NOT_A_PERSONAL_READER,
          "Account merge/deletion reference catalog classifies Memory ownership, not content.")


def _scope_nodes(node: ast.AST):
    """Own a declaration's body, never a nested declaration's SQL or names."""
    yield node
    for child in ast.iter_child_nodes(node):
        if not isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            yield from _scope_nodes(child)


def _names(node: ast.AST) -> frozenset[str]:
    return frozenset(
        child.id if isinstance(child, ast.Name) else child.attr
        for child in _scope_nodes(node) if isinstance(child, (ast.Name, ast.Attribute))
    )


def scan(tree: ast.Module, path: str) -> list[Site]:
    prose = {id(node.value) for node in ast.walk(tree)
             if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
             and isinstance(node.value.value, str)}
    catalogs = {target.id for node in tree.body if isinstance(node, (ast.Assign, ast.AnnAssign))
                for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
                if isinstance(target, ast.Name) and any(
                    isinstance(value, ast.Constant) and isinstance(value.value, str)
                    and TABLE.search(value.value) for value in ast.walk(node))}
    result: list[Site] = []

    def consider(node: ast.AST, name: str) -> None:
        text = "\n".join(value.value for value in _scope_nodes(node)
                         if isinstance(value, ast.Constant) and isinstance(value.value, str)
                         and id(value) not in prose)
        names = _names(node)
        dynamic = bool(INTERPOLATED_READ.search(text)) and (
            bool(names & catalogs) or name == "QueryStore.count_rows")
        if TABLE.search(text) or dynamic or (path, name) in FRAGMENT_SITES:
            result.append(Site(path, name, text, names))

    def walk(parent: ast.AST, prefix: str = "", *, constants: bool = True) -> None:
        for node in ast.iter_child_nodes(parent):
            if isinstance(node, ast.ClassDef):
                walk(node, prefix + node.name + ".")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                consider(node, prefix + node.name)
                walk(node, prefix + node.name + ".", constants=False)
            elif isinstance(node, ast.Lambda):
                consider(node, prefix + "<lambda>")
                walk(node, prefix + "<lambda>.", constants=False)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and constants:
                for target in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                    if isinstance(target, ast.Name):
                        consider(node, prefix + target.id)
                walk(node, prefix, constants=constants)
            else:
                walk(node, prefix, constants=constants)
    walk(tree)
    return result


def registration(site: Site):
    direct = REGISTRY.get((site.path, site.name))
    if direct is not None:
        return direct
    if site.path.split("/", 1)[0] in {"sqlite", "postgres"}:
        return REGISTRY.get(("*/" + site.path.rsplit("/", 1)[-1], site.name))
    return None


def violations(sites: list[Site]) -> list[str]:
    errors = []
    seen = set()
    for site in sites:
        entry = registration(site)
        if entry is None:
            errors.append(f"unregistered Memory SQL: {site.path}:{site.name}")
            continue
        key = (site.path, site.name)
        seen.add(key)
        category, reason = entry
        if category not in CATEGORIES or not reason.strip():
            errors.append(f"invalid registration: {key}")
        if category == UNADOPTED_EXCLUDED:
            helpers = EXPORT_HELPERS.get(site.name, ()) if site.path.endswith("/memory_store.py") else ()
            if helpers:
                for helper in helpers:
                    if helper not in site.names:
                        errors.append(f"missing shared unadopted predicate helper: {key}: {helper}")
            elif "unadopted_suggestion" not in site.names:
                errors.append(f"missing shared unadopted predicate: {key}")
        if category == CONFIRMED_ONLY and not (
            "confirmed" in site.text or "_transferable_where" in site.names
        ):
            errors.append(f"missing confirmed restriction: {key}")
    expected = {(backend + "/" + path[2:], name) for path, name in REGISTRY
                if path.startswith("*/") for backend in ("sqlite", "postgres")}
    expected.update(key for key in REGISTRY if not key[0].startswith("*/"))
    for key in expected - seen:
        errors.append(f"stale Memory SQL registration: {key}")
    return errors


@pytest.fixture(scope="module")
def sources():
    return app_source_trees(ROOT)


@pytest.fixture(scope="module")
def sites(sources):
    return [site for source in sources for site in scan(source.tree, source.relative)]


def test_every_memory_sql_site_has_a_live_justified_registration(sites):
    assert not violations(sites), "\n".join(violations(sites))


def test_scanner_includes_functions_and_constants_but_not_prose():
    source = ast.parse('''"""memory_items"""
SQL = "SELECT id FROM memory_items"
class Store:
    SQL = "SELECT id FROM memory_items"
    def read(self):
        """FROM memory_items is prose."""
        return "SELECT id FROM memory_items"
    def prose(self):
        """SELECT id FROM memory_items"""
''')
    assert {site.name for site in scan(source, "fixture.py")} == {"SQL", "Store.SQL", "Store.read"}


def test_deleting_exclusion_or_moving_a_reader_is_rejected(sites):
    target = next(site for site in sites if site.path == "sqlite/query_store.py"
                  and site.name == "QueryStore.list_user_usage")
    removed = Site(target.path, target.name, target.text, target.names - {"unadopted_suggestion"})
    assert any("missing shared unadopted predicate" in error for error in violations(
        [removed if site is target else site for site in sites]))
    moved = Site(target.path, "QueryStore.unreviewed_usage", target.text, target.names)
    errors = violations([moved if site is target else site for site in sites])
    assert any("unregistered Memory SQL" in error for error in errors)
    assert any("stale Memory SQL registration" in error for error in errors)


def _mutated_sites(sources, path, change):
    result = []
    for source in sources:
        tree = source.tree
        if source.relative == path:
            tree = copy.deepcopy(tree)
            change(tree)
        result.extend(scan(tree, source.relative))
    return result


def _method(tree, name):
    owner, method = name.split(".")
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == owner)
    return next(node for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == method)


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_export_content_and_its_fragment_are_excluded_readers(backend, sites):
    path = f"{backend}/memory_store.py"
    for name in (*EXPORT_HELPERS, "MemoryStore._export_where"):
        site = next(site for site in sites if site.path == path and site.name == name)
        assert registration(site)[0] == UNADOPTED_EXCLUDED


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_removing_the_real_export_fragment_predicate_is_rejected(backend, sources):
    path = f"{backend}/memory_store.py"

    def change(tree):
        class RemovePredicate(ast.NodeTransformer):
            def visit_Call(self, node):
                if isinstance(node.func, ast.Name) and node.func.id == "unadopted_suggestion":
                    return ast.Constant("1=1")
                return self.generic_visit(node)
        RemovePredicate().visit(_method(tree, "MemoryStore._export_where"))

    errors = violations(_mutated_sites(sources, path, change))
    assert any("missing shared unadopted predicate" in error and "MemoryStore._export_where" in error
               for error in errors), errors


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_export_page_cannot_stop_using_its_reviewed_predicate_helper(backend, sources):
    path = f"{backend}/memory_store.py"

    def change(tree):
        class RemoveHelper(ast.NodeTransformer):
            def visit_Call(self, node):
                if isinstance(node.func, ast.Attribute) and node.func.attr == "_export_where":
                    return ast.Tuple([ast.Constant("1=1"), ast.List([], ast.Load())], ast.Load())
                return self.generic_visit(node)
        RemoveHelper().visit(_method(tree, "MemoryStore._export_page_on"))

    errors = violations(_mutated_sites(sources, path, change))
    assert any("missing shared unadopted predicate helper" in error and "MemoryStore._export_page_on" in error
               for error in errors), errors


@pytest.mark.parametrize("parent", ["MemoryStore._write_mutation_on", "MemoryStore.owned_memory_refs"])
@pytest.mark.parametrize("kind", ["def", "async def", "lambda"])
def test_nested_sql_cannot_inherit_a_writer_or_lifecycle_exemption(parent, kind, sources):
    path = "sqlite/memory_store.py"

    def change(tree):
        declaration = ("reader = lambda: 'SELECT content_md FROM memory_items'" if kind == "lambda"
                       else f"{kind} reader():\n    return 'SELECT content_md FROM memory_items'")
        _method(tree, parent).body.append(ast.parse(declaration).body[0])

    errors = violations(_mutated_sites(sources, path, change))
    nested = parent + (".<lambda>" if kind == "lambda" else ".reader")
    assert any("unregistered Memory SQL" in error and nested in error for error in errors), errors


def test_moving_real_export_sql_into_a_nested_reader_is_rejected(sources):
    path = "sqlite/memory_store.py"

    def change(tree):
        method = _method(tree, "MemoryStore._export_page_on")
        query = next(node for node in _scope_nodes(method)
                     if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                     and node.func.attr == "execute")
        nested = ast.parse("def content_page():\n    pass").body[0]
        nested.body = [ast.Return(copy.deepcopy(query))]

        class MoveQuery(ast.NodeTransformer):
            def visit_Call(self, node):
                if node is query:
                    return ast.Call(ast.Name("content_page", ast.Load()), [], [])
                return self.generic_visit(node)
        MoveQuery().visit(method)
        method.body.insert(0, nested)

    errors = violations(_mutated_sites(sources, path, change))
    assert any("unregistered Memory SQL" in error and "MemoryStore._export_page_on.content_page" in error
               for error in errors), errors


def test_nested_sql_and_predicate_names_belong_only_to_their_own_scopes():
    tree = ast.parse('''
class Store:
    def writer(self):
        query = "UPDATE memory_items SET status = 'confirmed'"
        def reader():
            return "SELECT content_md FROM memory_items", unadopted_suggestion("m")
        class Local:
            SQL = "SELECT id FROM memory_items"
''')
    found = {site.name: site for site in scan(tree, "fixture.py")}
    assert set(found) == {"Store.writer", "Store.writer.reader", "Store.writer.Local.SQL"}
    assert "SELECT" not in found["Store.writer"].text
    assert "unadopted_suggestion" not in found["Store.writer"].names
    assert "unadopted_suggestion" in found["Store.writer.reader"].names
