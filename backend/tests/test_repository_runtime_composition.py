"""Guards for the domain-split composition root (architecture review B4).

``RepositoryRuntime.__init__`` used to inline every wiring detail in one
428-line constructor.  It is now an orchestration of module-level ``_build_*``
domain constructors, each returning a frozen bundle.  Two properties keep that
split load-bearing rather than cosmetic, and neither is observable from a
behavioural test:

  * **no builder can reach the runtime.**  A ``_build_*`` that took ``self``
    could read a *later* domain and re-create exactly the cycle the split
    removes -- and nothing would fail, because the runtime would still compose.
  * **every seat is mounted explicitly.**  Dropping one ``self.x = domain.x``
    line leaves a runtime that constructs fine and only explodes later, in
    whichever request first touches the missing attribute.

The frozen attribute set below is the cheap, permanent version of the one-off
construction-graph diff run against ``origin/master`` in the B4 PR (key set +
per-attribute type + intra-runtime reference topology, 85 attributes and 200
reference edges, all identical).
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from app.core.config import Settings
from app.services import repository_runtime
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture()
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    return SQLiteRepository(Settings(_env_file=None))._runtime


# Every attribute ``RepositoryRuntime.__init__`` mounts, sorted.  This is a
# *frozen set*, not a floor: adding a seat is a deliberate act that belongs in
# the same diff as this list, and removing one must never pass silently.
RUNTIME_ATTRIBUTES = [
    "_ask_retrieval",
    "_ask_wire_lock",
    "_closed",
    "_embedder",
    "_notebook_languages",
    "_retrieval_wire_lock",
    "agent_observations",
    "agent_profile",
    "agent_profile_jobs",
    "ask",
    "ask_cancellations",
    "ask_completed_observers",
    "ask_execution",
    "ask_state",
    "candidate_retrieval",
    "catalog",
    "chunk_question_index",
    "chunk_store",
    "collection_catalog",
    "collection_enumeration",
    "command_catalog",
    "database",
    "embedding_store",
    "event_log",
    "evidence_context",
    "governance",
    "graph_retrieval",
    "groups",
    "identity",
    "index_projections",
    "kg_analysis",
    "kg_build_jobs",
    "kg_mutations",
    "knowhow_history_store",
    "knowhow_store",
    "knowhow_transfer_store",
    "knowledge",
    "knowledge_governance",
    "knowledge_lifecycle",
    "knowledge_query",
    "memory_retriever",
    "memory_service",
    "memory_store",
    "model_status",
    "model_status_store",
    "models",
    "notebook_copies",
    "notebook_store",
    "notebook_summaries",
    "parser_provider_chain",
    "pending_actions_service",
    "queries",
    "report_application",
    "report_cancellations",
    "report_completed_observers",
    "report_execution",
    "report_store",
    "retrieval",
    "retrieval_contributors",
    "retrieval_experience_jobs",
    "retrieval_experiences",
    "retrieval_snapshots",
    "root_dir",
    "scale_artifact_store",
    "scale_artifacts",
    "scale_builder",
    "scale_catalog",
    "schema_registry",
    "seams",
    "search_profile_jobs",
    "selected_source_graph",
    "settings",
    "sharing",
    "sharing_store",
    "source_chunking",
    "source_embedding",
    "source_files",
    "source_graph_enrichment",
    "source_graph_primitives",
    "source_ingestion",
    "source_partitioned_ppr",
    "source_store",
    "source_subgraph_ppr",
    "source_subgraphs",
    "unified_kg",
]


def _runtime_tree() -> ast.Module:
    return ast.parse(
        Path(repository_runtime.__file__).read_text(encoding="utf-8"),
        filename=repository_runtime.__file__,
    )


def _init_node(tree: ast.Module) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "RepositoryRuntime":
            for member in node.body:
                if isinstance(member, ast.FunctionDef) and member.name == "__init__":
                    return member
    raise AssertionError("RepositoryRuntime.__init__ not found")


def _builders(tree: ast.Module) -> list[ast.FunctionDef]:
    return [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("_build_")
    ]


def _parameter_names(node: ast.FunctionDef) -> list[str]:
    args = node.args
    names = [
        arg.arg for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)
    ]
    for extra in (args.vararg, args.kwarg):
        if extra is not None:
            names.append(extra.arg)
    return names


def test_the_module_really_has_the_domain_builders():
    """Empty-run protection: every assertion below is a loop over the builder
    list, so a rename of the ``_build_*`` convention would make three guards
    pass vacuously."""

    names = sorted(builder.name for builder in _builders(_runtime_tree()))
    assert len(names) >= 8, names
    assert "_build_process_foundation" in names
    assert "_build_persistence_seats" in names


def test_no_domain_builder_can_reach_the_runtime():
    """The rule that makes the split a topology instead of a rename.

    A builder that accepted ``self``/``runtime``/``repo`` could read a domain
    built *after* it and reintroduce the cycle -- and every test would stay
    green, because the runtime would still compose.  Parameter names plus the
    ``__self__``/``__closure__`` ban below are the checkable form of "inputs
    come only from earlier domains".
    """

    forbidden = {"self", "runtime", "repo", "repository", "facade"}
    offenders = [
        f"{builder.name}{sorted(set(_parameter_names(builder)) & forbidden)}"
        for builder in _builders(_runtime_tree())
        if set(_parameter_names(builder)) & forbidden
    ]
    assert offenders == [], (
        "domain builders must take earlier bundles, never the runtime itself: "
        f"{offenders}"
    )


_RUNTIME_ESCAPE_HATCH_ATTRS = {"__self__", "__closure__", "__func__", "__dict__"}


def test_no_domain_builder_reads_the_runtime_class():
    """The other half of the same rule: a builder that never *takes* the
    runtime could still reach it through the class (``RepositoryRuntime`` is a
    module global), which is the same cycle with an extra hop.

    It could also reach it through the back door of the callables it *is*
    handed: three of them (``self._current_user_id``, ``self.ask_service``,
    ``self._note_ask_completed``) are late-bound bound methods passed to
    builders precisely so no builder has to close over the runtime -- but a
    bound method's ``__self__`` *is* the composition root, and its
    ``__closure__``/``__func__``/``__dict__`` are one hop further into the
    same object. Reading any of those off anything inside a builder is banned
    the same way the ``RepositoryRuntime`` name itself is.
    """

    offenders = []
    for builder in _builders(_runtime_tree()):
        for node in ast.walk(builder):
            if isinstance(node, ast.Name) and node.id == "RepositoryRuntime":
                offenders.append(builder.name)
            elif (
                isinstance(node, ast.Attribute)
                and node.attr in _RUNTIME_ESCAPE_HATCH_ATTRS
            ):
                offenders.append(f"{builder.name}:{node.attr}")
    assert offenders == [], offenders


def test_every_domain_builder_documents_where_its_inputs_come_from():
    for builder in _builders(_runtime_tree()):
        doc = ast.get_docstring(builder)
        assert doc, f"{builder.name} needs a docstring naming its inputs"
        assert "omain" in doc, (
            f"{builder.name}'s docstring must say which domain it is and what "
            "earlier bundles it reads"
        )


def test_the_composition_root_only_orchestrates():
    """``__init__`` calls builders and mounts fields -- it does not construct
    services itself.  Its only permitted direct construction is the runtime's
    own private state (``threading.Lock``); anything else means a domain leaked
    back into the composition root."""

    init = _init_node(_runtime_tree())
    called = set()
    for node in ast.walk(init):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                called.add(f"{func.value.id}.{func.attr}")
    unexpected = sorted(
        name
        for name in called
        if not name.startswith("_build_") and name not in {"threading.Lock"}
    )
    assert unexpected == [], (
        f"RepositoryRuntime.__init__ must only call domain builders: {unexpected}"
    )


def test_every_runtime_attribute_is_mounted_by_an_explicit_line():
    """The frozen seat list, read off the source rather than a live runtime.

    Source-level so it also proves the mounts are *explicit* assignments: a
    ``self.__dict__.update(...)`` shortcut would keep a constructed runtime
    identical while making "which line owns this seat" unanswerable.
    """

    init = _init_node(_runtime_tree())
    mounted = sorted(
        {
            target.attr
            for node in ast.walk(init)
            for target in (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.target]
                if isinstance(node, ast.AnnAssign)
                else []
            )
            if isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
        }
    )
    assert mounted == sorted(RUNTIME_ATTRIBUTES)


def test_a_live_runtime_exposes_exactly_the_frozen_attribute_set(runtime):
    assert sorted(vars(runtime)) == sorted(RUNTIME_ATTRIBUTES)


def test_the_shared_instances_the_domains_hand_around_stay_shared(runtime):
    """Cross-domain identity edges that a re-composition could silently break.

    Each of these was a single local variable inside the old constructor; after
    the split they cross a bundle boundary, and rebuilding one instead of
    passing it would leave two half-populated caches / two storage roots that
    only diverge under load.
    """

    assert runtime.catalog._summaries is runtime.notebook_summaries
    assert runtime.report_application.notebooks is runtime.catalog
    assert runtime.collection_enumeration._catalog is runtime.collection_catalog
    assert runtime.selected_source_graph._snapshots is runtime.source_subgraphs
    assert runtime.selected_source_graph._primitives is runtime.source_graph_primitives
    assert runtime.selected_source_graph._online_ppr is runtime.source_subgraph_ppr
    assert (
        runtime.selected_source_graph._partitioned_ppr
        is runtime.source_partitioned_ppr
    )
    assert (
        runtime.selected_source_graph._enrichment is runtime.source_graph_enrichment
    )
    assert runtime.source_graph_primitives._snapshots is runtime.source_subgraphs
    assert runtime.source_partitioned_ppr._artifacts is runtime.scale_artifact_store
    assert runtime.ask_execution.cancellations is runtime.ask_cancellations
    assert runtime.ask_execution.ask_state is runtime.ask_state


def test_the_storage_root_callable_does_not_retain_the_runtime(runtime):
    """The catalog's ``storage_dir`` seat is default-bound to the eager
    ``SourceFileStore`` -- closing over the runtime instead would chain
    catalog -> runtime -> facade and keep the whole repository alive for as
    long as anything retains the catalog."""

    closure = runtime.catalog._storage_dir
    assert closure() is runtime.source_files.storage_dir
    retained = list(closure.__defaults__ or ()) + [
        cell.cell_contents for cell in (closure.__closure__ or ())
    ]
    assert runtime.source_files in retained
    assert not any(item is runtime for item in retained)


LAZY_WIRING_SEATS = {
    "_build_ask_domain": frozenset({"ask"}),
    "_build_knowledge_domain": frozenset(
        {
            "kg_mutations",
            "knowledge_governance",
            "knowledge_lifecycle",
            "knowledge_query",
            "pending_actions_service",
        }
    ),
    "_build_notebook_domain": frozenset({"notebook_copies", "sharing"}),
    "_build_report_domain": frozenset({"report_execution"}),
    "_build_retrieval_domain": frozenset(
        {
            "candidate_retrieval",
            "evidence_context",
            "graph_retrieval",
            "memory_retriever",
            "memory_service",
            "retrieval",
        }
    ),
    "_build_source_graph_domain": frozenset(
        {"scale_artifacts", "scale_builder", "scale_catalog"}
    ),
    "_build_source_pipeline": frozenset(
        {"source_chunking", "source_embedding", "source_ingestion"}
    ),
}


def test_the_lazy_seats_are_left_empty_by_their_builders():
    """Everything a ``wire_*`` finishes must leave its builder as ``None`` -- a
    builder that filled one eagerly would move a facade-bound seam call into
    the composition root, where the seam does not exist yet.

    Keyed by builder rather than flattened into one set on purpose: a seat
    left ``None`` by the *wrong* builder would satisfy a flat membership
    check while actually meaning the seat moved between domains -- e.g.
    ``sharing`` migrating from ``_build_notebook_domain`` into
    ``_build_report_domain`` changes which later ``wire_*`` call is allowed
    to fill it, and a flat set can't see that.

    Asserted on the builders and not on a composed runtime on purpose: the
    facade's own constructor calls most of the ``wire_*`` methods, so by the
    time anyone can hold a ``RepositoryRuntime`` all but five of these are
    already filled and a live check would be nearly vacuous.
    """

    empty_by_builder: dict[str, set[str]] = {}
    for builder in _builders(_runtime_tree()):
        empty: set[str] = set()
        for node in ast.walk(builder):
            if (
                isinstance(node, ast.keyword)
                and node.arg
                and isinstance(node.value, ast.Constant)
                and node.value.value is None
            ):
                empty.add(node.arg)
        empty_by_builder[builder.name] = empty

    for builder_name, seats in LAZY_WIRING_SEATS.items():
        missing = sorted(seats - empty_by_builder.get(builder_name, set()))
        assert missing == [], (
            f"{builder_name} no longer leaves these seats empty: {missing}"
        )


def test_the_current_user_accessor_is_late_bound(runtime):
    """The two domains that need the request user get a zero-argument accessor,
    not a resolved id: resolving at build time would freeze whichever user the
    composition root happened to see into every later request."""

    assert runtime.schema_registry.current_user_id == runtime._current_user_id
    assert runtime.command_catalog.current_user_id == runtime._current_user_id
    assert inspect.signature(runtime._current_user_id).parameters == {}
