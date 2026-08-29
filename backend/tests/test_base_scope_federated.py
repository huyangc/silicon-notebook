"""参考库按库勾选 —— 在**真实检索链**上跑（不是纯函数/替身）。

上面 ``test_base_scope.py`` 覆盖的是契约层与各个收口的单元行为；这里跑一个真的
``SQLiteRepository`` + 一个真的挂载参考库，走真的联邦候选生产者，覆盖：

* 参考库能贡献的**每一类候选**各一条（知识对象 / 关系 / 元素 / chunk）；
* 生产事故的形状（本地一篇 + 参考库若干，取消勾选后结果只剩本地来源）；
* 效率半边：被取消勾选的库**不是搜完再丢**，而是根本不搜；
* 装配点（``knowledge_context``）与 graph 全图漫游两处「变成 prompt 文本 + 活锚点」的出口；
* ``AskRequest.base_scope`` 这唯一一条把提交载荷变成检索范围的真实接线。
"""
import pytest

from app.models.source_scope import BaseNotebookScope
from app.services.source_scope import source_scope_context


_MOE = "Mixture-of-Experts"
_QUERY = "Mixture-of-Experts routing"


def _seed_library(repo, notebook_id: str, prefix: str, *, name: str = _MOE) -> str:
    """One parsed source (2 elements -> real chunks) + two concepts joined by a
    relation, all evidence-bound to those elements.

    ``name`` is shared across libraries on purpose: ``rebuild_unified_kg``
    derives concept_clusters' canonical_id from it, which is what lets PPR
    bridge a chunk from the mounted library into an active-notebook question.
    """
    from app.services.sqlite_repository import _now

    source_id = f"src-{prefix}"
    element_ids = [f"el-{prefix}-{index}" for index in (1, 2)]
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,"
            "file_name,file_path,file_size,file_hash,summary,doc_type,"
            "parse_status,created_at,updated_at) "
            "VALUES (?,?,?,'markdown','ready',?,'',0,?,'','','parsed',?,?)",
            (source_id, notebook_id, f"Doc {prefix}", f"{prefix}.md",
             f"hash-{prefix}", now, now),
        )
        for index, element_id in enumerate(element_ids, 1):
            db.execute(
                "INSERT INTO source_elements (id,source_id,element_type,"
                "location_label,text,metadata,created_at) "
                "VALUES (?,?,'paragraph',?,?,'{}',?)",
                (element_id, source_id, f"p{index}",
                 f"{name} routing is described in document {prefix} part {index}.",
                 now),
            )
    repo._chunk_and_embed_source(source_id)

    def _evidence(element_id: str) -> list[dict]:
        return [{
            "source_id": source_id, "source_title": f"Doc {prefix}",
            "element_id": element_id, "element_type": "paragraph",
            "location_label": "p1", "quoted_span": f"{name} routing",
            "confidence": 1.0,
        }]

    repo.store_kg(notebook_id, source_id, [
        {"local_id": f"{prefix}-A", "object_type": "concept",
         "payload": {"name": name, "section_path": "1"},
         "evidence": _evidence(element_ids[0])},
        {"local_id": f"{prefix}-B", "object_type": "concept",
         "payload": {"name": f"{name} routing gate", "section_path": "1"},
         "evidence": _evidence(element_ids[1])},
    ], [
        {"source_local_id": f"{prefix}-A", "target_local_id": f"{prefix}-B",
         "edge_type": "kind_of", "evidence": _evidence(element_ids[0])},
    ])
    repo.rebuild_unified_kg(notebook_id)
    return source_id


@pytest.fixture
def federated_corpus(tmp_path, monkeypatch):
    """(repo, active_id, base_id, local_source_id, base_source_ids)."""
    from app.core.config import Settings
    from app.models.schemas import NotebookCreate
    from app.services.embedding import FakeEmbedder
    from app.services.sqlite_repository import SQLiteRepository
    from tests.model_testkit import bind_all_embedding_clients

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repo = SQLiteRepository(Settings(_env_file=None))
    bind_all_embedding_clients(repo, FakeEmbedder(dim=16))

    base = repo.create_notebook(NotebookCreate(name="reference library"))
    repo.mark_notebook_base(base.id)
    active = repo.create_notebook(NotebookCreate(name="my notebook"))
    # The incident shape: one local document, a mounted library holding more.
    base_sources = [_seed_library(repo, base.id, f"base{n}") for n in (1, 2, 3)]
    local_source = _seed_library(repo, active.id, "local")
    repo.replace_notebook_bases(active.id, [base.id], "user-local")
    return repo, active.id, base.id, local_source, base_sources


def _base_excluded(active_id: str):
    """The library checkbox exactly as the API entry point freezes it."""
    return source_scope_context(
        active_id, None, BaseNotebookScope(mode="include", notebook_ids=[])
    )


def test_chain_knowledge_objects_from_an_unchecked_library_are_gone(
    federated_corpus,
):
    repo, active, base, _local_source, _base_sources = federated_corpus

    unscoped = repo.retrieval.federated_retrieve(active, _QUERY)
    assert {hit.notebook_id for hit in unscoped} == {active, base}, (
        "baseline: the mounted library must be reachable at all, otherwise the "
        "scoped assertion below proves nothing"
    )

    with _base_excluded(active):
        scoped = repo.retrieval.federated_retrieve(active, _QUERY)
    assert scoped, "unchecking a reference library must not empty the local hits"
    assert {hit.notebook_id for hit in scoped} == {active}


def test_chain_relations_from_an_unchecked_library_are_gone(federated_corpus):
    repo, active, base, _local_source, _base_sources = federated_corpus

    unscoped = repo.retrieval.federated_retrieve_relations(active, _QUERY)
    assert {hit.notebook_id for hit in unscoped} == {active, base}

    with _base_excluded(active):
        scoped = repo.retrieval.federated_retrieve_relations(active, _QUERY)
    assert scoped, "当前库自己的关系检索不得被库维度收窄顺带关掉(R1)"
    assert {hit.notebook_id for hit in scoped} == {active}


def test_chain_elements_from_an_unchecked_library_are_gone(federated_corpus):
    """``RetrievedElement`` carries no notebook_id, so ``filter_retrieval_items``
    can only ever judge one against the ACTIVE notebook — the per-library skip
    is what keeps the origin known.

    What this asserts is the OUTCOME, which survives deleting that skip (the
    inner ``_retrieve_elements`` intersects the same per-notebook allow-list and
    comes back empty either way). The test that actually pins the skip is
    ``test_chain_never_queries_an_unchecked_library``.
    """
    repo, active, base, local_source, base_sources = federated_corpus
    keys = [(active, local_source)] + [(base, sid) for sid in base_sources]

    unscoped = repo.retrieval.federated_retrieve_elements(
        active, _QUERY, allowed_source_keys=keys, limit=8,
    )
    assert {item.source_id for item in unscoped} & set(base_sources)

    with _base_excluded(active):
        scoped = repo.retrieval.federated_retrieve_elements(
            active, _QUERY, allowed_source_keys=keys, limit=8,
        )
    assert scoped
    assert {item.source_id for item in scoped} == {local_source}


def test_chain_chunks_from_an_unchecked_library_are_gone(federated_corpus):
    repo, active, _base, local_source, base_sources = federated_corpus

    unscoped = repo.retrieval.ppr_retrieve(active, _QUERY)
    assert {chunk.source_id for chunk in unscoped} & set(base_sources), (
        "baseline: PPR must actually bridge a mounted-library chunk in"
    )

    with _base_excluded(active):
        scoped = repo.retrieval.ppr_retrieve(active, _QUERY)
    # `<=` (subset) would also pass on an empty result -- PPR over-filtering to
    # nothing is a different bug than a library leak, and this assertion must
    # not paper over it.
    assert {chunk.source_id for chunk in scoped} == {local_source}


def test_chain_never_queries_an_unchecked_library(federated_corpus, monkeypatch):
    """Efficiency half of the contract: the excluded library is not searched
    and then discarded — it is never searched."""
    repo, active, base, local_source, base_sources = federated_corpus
    candidates = repo.retrieval.candidates
    seen: dict[str, list[str]] = {}

    for method in ("_retrieve_scored", "_retrieve_relations_scored",
                   "_retrieve_elements"):
        original = getattr(candidates, method)
        seen[method] = []

        def _spy(notebook_id, *args, _m=method, _o=original, **kwargs):
            seen[_m].append(notebook_id)
            return _o(notebook_id, *args, **kwargs)

        monkeypatch.setattr(candidates, method, _spy)

    keys = [(active, local_source)] + [(base, sid) for sid in base_sources]
    with _base_excluded(active):
        repo.retrieval.federated_retrieve(active, _QUERY)
        repo.retrieval.federated_retrieve_relations(active, _QUERY)
        repo.retrieval.federated_retrieve_elements(
            active, _QUERY, allowed_source_keys=keys, limit=8,
        )

    for method, notebook_ids in seen.items():
        assert notebook_ids, f"{method} was never reached -- the spy is blind"
        assert base not in notebook_ids, (
            f"{method} still queried the unchecked library {base}: {notebook_ids}"
        )


def test_incident_shape_local_document_is_not_drowned_by_the_mounted_library(
    federated_corpus,
):
    """真机事故的形状:限定到自己那一篇的提问,引用却全来自挂载的参考库。

    取消勾选参考库后,**任何一类**候选都不许再带上它的来源。
    """
    repo, active, _base, local_source, base_sources = federated_corpus
    keys = [(active, local_source)] + [(_base, sid) for sid in base_sources]

    unscoped_sources = {
        evidence.source_id
        for hit in repo.retrieval.federated_retrieve(active, _QUERY)
        for evidence in hit.evidence
    }
    assert unscoped_sources & set(base_sources), (
        "baseline: without the checkbox the mounted library does reach the answer"
    )

    with _base_excluded(active):
        surfaced = {
            evidence.source_id
            for hit in repo.retrieval.federated_retrieve(active, _QUERY)
            for evidence in hit.evidence
        }
        surfaced |= {
            evidence.source_id
            for hit in repo.retrieval.federated_retrieve_relations(active, _QUERY)
            for evidence in hit.evidence
        }
        surfaced |= {
            item.source_id
            for item in repo.retrieval.federated_retrieve_elements(
                active, _QUERY, allowed_source_keys=keys, limit=8,
            )
        }
        surfaced |= {
            chunk.source_id for chunk in repo.retrieval.ppr_retrieve(active, _QUERY)
        }

    assert surfaced == {local_source}, (
        f"mounted-library sources leaked into the answer: "
        f"{sorted(surfaced - {local_source})}"
    )


def test_knowledge_context_gates_the_library_at_the_assembly_point(
    federated_corpus,
):
    """``knowledge_context()`` is where a KG hit BECOMES a prompt line and a
    live ``k{n}`` anchor, so that is where the library ceiling has to sit.

    Gating its ``node_context()`` re-read instead is not enough and this is the
    case that proves it: ``node_context`` supplies only the definition/snippet,
    while the object's NAME comes off the hit itself — an emptied row still
    renders ``k1: [concept][base] <library's concept name>`` and still resolves
    as a citation.

    Run against the REAL composition (``EvidenceContextService`` is constructed
    with ``knowledge=GraphRetrievalService``, not with this service): a double
    would prove a gate works while proving nothing about whether this path
    reaches it.
    """
    repo, active, base, _local_source, _base_sources = federated_corpus
    library_hits = [
        hit for hit in repo.retrieval.federated_retrieve(active, _QUERY)
        if hit.notebook_id == base
    ]
    assert library_hits, "baseline: the mounted library must produce a hit at all"
    names = {str(hit.payload.get("name") or "") for hit in library_hits}
    assert all(names), "baseline: the library's hits must carry renderable names"

    block, id_map = repo._answer_context(active, library_hits)
    assert id_map and any(name in block for name in names), (
        "baseline: unscoped, the library's node does render into the prompt"
    )

    with _base_excluded(active):
        block, id_map = repo._answer_context(active, library_hits)
    assert not id_map, f"unchecked library became citable: {id_map}"
    assert not [name for name in names if name in block], (
        f"unchecked library reached the prompt: {block!r}"
    )


# test_ask_graph_walk_never_renders_a_node_from_an_unchecked_library was removed
# with the retired full-graph ask engine: it spied on graph_reason.render_subgraph_context
# during a full-graph BFS that a PLAIN question triggered end-to-end — that
# whole-graph-walk-on-every-question behavior no longer exists anywhere.
# scoped_subgraph_nodes (source_scope.py) — the node-level notebook_id filter
# this test asserted on — is still live infra consumed by reasoning's
# follow_chain (retrieval_candidates.py), but follow_chain only activates for
# an explicit two-hop derivation question, not this fixture's generic _QUERY,
# so no equivalent end-to-end regression test currently covers that path.


def test_ask_payload_base_scope_alone_narrows_without_manual_context(
    federated_corpus,
):
    """Every other scoped case in these files enters ``source_scope_context``
    itself. The ONE real seam that turns a submitted ``AskRequest.base_scope``
    into that context for Ask is ``AskService.ask()``'s third argument to
    ``source_scope_context`` — a mutation that drops it makes the whole feature
    a no-op for Ask while every ContextVar-driven test stays green. This test
    relies on the payload alone, exactly like a real HTTP request would.
    """
    import json
    import re

    from app.models.schemas import AskRequest
    from tests.model_testkit import bind_chat_client, bind_rerank_client

    class _IdentityReranker:
        configured = True

        def rerank(self, query, documents, on_error=None):
            return list(range(len(documents)))

    class _MirrorLLM:
        """Cites back every `kN` evidence key the real prompt actually offered,
        so anchor-binding (which gates mix-mode citations) never hides which
        library the retrieval boundary let through."""
        configured = True
        model = "test"

        def chat_json(self, messages, schema_hint, **kwargs):
            prompt = messages[0]["content"] if messages else ""
            keys = sorted({int(m) for m in re.findall(r"\bk(\d+):", prompt)})
            markers = " ".join(f"[k{k}]" for k in keys)
            return json.dumps({"answer": f"summary. {markers}", "grounded": True})

    repo, active, _base, local_source, base_sources = federated_corpus
    repo.settings.query_rewrite_enabled = False
    # chunk mode only crosses into a mounted library through the "mix" overlay's
    # PPR path (kg_overlay_enabled AND a configured reranker AND a KG on either
    # side) -- without it `_retrieve_chunks` is single-notebook by construction
    # and this test could not prove anything either way.
    bind_rerank_client(repo, _IdentityReranker())
    bind_chat_client(repo, "ask_answer", _MirrorLLM())

    baseline = repo.ask(active, AskRequest(question=_QUERY, mode="chunk"))
    baseline_sources = {c.source_id for c in baseline.citations}
    assert baseline_sources & set(base_sources), (
        "baseline: without narrowing, the mounted library must reach the "
        "chunk-mode answer -- otherwise this test cannot prove anything"
    )

    narrowed = repo.ask(active, AskRequest(
        question=_QUERY, mode="chunk",
        base_scope=BaseNotebookScope(mode="include", notebook_ids=[]),
    ))
    narrowed_sources = {c.source_id for c in narrowed.citations}
    assert narrowed_sources, "local evidence must still come back"
    assert narrowed_sources == {local_source}
