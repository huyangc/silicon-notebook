from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.models.schemas import AskRequest, AskResponse, MemoryHit, NotebookCreate
from app.services.memory_retrieval import MemoryRetriever
from app.services.sqlite_repository import (
    SQLiteRepository,
    reset_request_user,
    set_request_user,
)
from tests.model_testkit import RecordingModelProvider


@pytest.fixture
def memory_data(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'memory-retrieval.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    provider = RecordingModelProvider()
    repo = SQLiteRepository(
        Settings(_env_file=None, model_services_config=""),
        model_provider=provider,
    )
    alice = repo.create_user("a00123456", "pw")
    bob = repo.create_user("b00654321", "pw")

    token = set_request_user(alice)
    try:
        notebook = repo.create_notebook(NotebookCreate(name="Timing notebook"))
        other = repo.create_notebook(NotebookCreate(name="Other notebook"))
    finally:
        reset_request_user(token)
    repo.add_member(notebook.id, bob.id)

    with repo._write() as db:
        db.executemany(
            "INSERT INTO agent_profiles "
            "(id,owner_id,name,description,status,created_at,updated_at) "
            "VALUES (?,?,?,'','active','t','t')",
            [
                ("agent-a", alice.id, "Agent A"),
                ("agent-b", alice.id, "Agent B"),
                ("agent-bob", bob.id, "Agent Bob"),
            ],
        )

    service = repo._runtime.memory_service

    def candidate(nb, user, agent, request, title, content):
        return service.create_candidate(
            nb.id, user.id, agent, request, title, content, [], "task"
        )

    relevant_candidate = candidate(
        notebook, alice, "agent-a", "candidate", "Candidate-only timing", "Fix setup timing closure"
    )
    confirmed = candidate(
        notebook, alice, "agent-b", "confirmed", "Confirmed timing decision", "Fix setup timing closure"
    )
    confirmed = service.confirm(confirmed.id, alice.id)
    unrelated = candidate(
        notebook, alice, "agent-a", "unrelated", "Lunch", "Banana inventory"
    )
    unrelated = service.confirm(unrelated.id, alice.id)
    rejected = candidate(
        notebook, alice, "agent-a", "rejected", "Timing rejected", "Timing closure"
    )
    rejected = service.reject(rejected.id, alice.id)
    deprecated = candidate(
        notebook, alice, "agent-a", "deprecated", "Timing old", "Timing closure"
    )
    deprecated = service.confirm(deprecated.id, alice.id)
    deprecated = service.deprecate(deprecated.id, alice.id)
    other_memory = candidate(
        other, alice, "agent-a", "other", "Timing other", "Timing closure"
    )
    other_memory = service.confirm(other_memory.id, alice.id)
    bob_memory = candidate(
        notebook, bob, "agent-bob", "bob", "Timing Bob", "Timing closure"
    )
    bob_memory = service.confirm(bob_memory.id, bob.id)

    return SimpleNamespace(
        repo=repo,
        retriever=repo._runtime.memory_retriever,
        alice=alice,
        bob=bob,
        notebook=notebook,
        other=other,
        candidate=relevant_candidate,
        confirmed=confirmed,
        unrelated=unrelated,
        rejected=rejected,
        deprecated=deprecated,
        other_memory=other_memory,
        bob_memory=bob_memory,
        provider=provider,
    )


def test_notebook_plane_returns_relevant_confirmed_only(memory_data):
    data = memory_data
    assert ("embedding", "memory_embedding") in data.provider.calls
    assert ("embedding", "retrieval_query_embedding") in data.provider.calls
    hits = data.retriever.notebook_memory_hits(
        data.alice.id, data.notebook.id, "timing closure", 10
    )

    assert [hit.memory_id for hit in hits] == [data.confirmed.id]
    assert all(hit.status == "confirmed" for hit in hits)


def test_agent_plane_shares_candidates_across_same_owner_profiles_only(memory_data):
    data = memory_data
    alice_ids = {
        hit.memory_id
        for hit in data.retriever.agent_memory_hits(
            data.alice.id, data.notebook.id, "timing closure", True, 10
        )
    }
    bob_ids = {
        hit.memory_id
        for hit in data.retriever.agent_memory_hits(
            data.bob.id, data.notebook.id, "timing closure", True, 10
        )
    }

    assert data.candidate.id in alice_ids
    assert data.confirmed.id in alice_ids
    assert data.candidate.id not in bob_ids
    assert data.bob_memory.id in bob_ids


def test_planes_never_leak_terminal_or_cross_notebook_memory(memory_data):
    data = memory_data
    hits = data.retriever.agent_memory_hits(
        data.alice.id, data.notebook.id, "timing", True, 20
    )
    ids = {hit.memory_id for hit in hits}

    assert data.rejected.id not in ids
    assert data.deprecated.id not in ids
    assert data.other_memory.id not in ids
    assert data.bob_memory.id not in ids


def test_agent_include_candidates_false_matches_notebook_status_boundary(memory_data):
    data = memory_data
    hits = data.retriever.agent_memory_hits(
        data.alice.id, data.notebook.id, "timing closure", False, 10
    )

    assert [hit.memory_id for hit in hits] == [data.confirmed.id]
    assert all(hit.status == "confirmed" for hit in hits)


def test_public_retrieval_port_exposes_both_memory_planes(memory_data):
    data = memory_data
    notebook_hits = data.repo.retrieval.notebook_memory_hits(
        data.alice.id, data.notebook.id, "timing closure", 10
    )
    agent_hits = data.repo.retrieval.agent_memory_hits(
        data.alice.id, data.notebook.id, "timing closure", True, 10
    )

    assert [hit.memory_id for hit in notebook_hits] == [data.confirmed.id]
    assert data.candidate.id in {hit.memory_id for hit in agent_hits}


def test_memory_hit_retains_provenance_without_fabricated_source_identity(memory_data):
    data = memory_data
    hit = data.retriever.notebook_memory_hits(
        data.alice.id, data.notebook.id, "timing closure", 10
    )[0]

    assert hit.provenance == data.confirmed.provenance
    assert "source_id" not in type(hit).model_fields
    assert "element_id" not in type(hit).model_fields


class _MemoryAnswerLLM:
    configured = True
    model = "memory-test"

    def __init__(self):
        self.prompts = []

    def chat_json(self, messages, schema_hint, **kwargs):
        self.prompts.append(messages[-1]["content"])
        return '{"answer":"Use the saved timing decision [k3001].","grounded":true}'


def test_ask_uses_confirmed_memory_anchor_without_fake_source_ids(memory_data):
    data = memory_data
    data.repo.settings.query_rewrite_enabled = False
    llm = _MemoryAnswerLLM()
    data.provider.chat_clients = {"ask_answer": llm}
    token = set_request_user(data.alice)
    try:
        response = data.repo._runtime.ask_service().ask_chunk(
            data.notebook.id,
            AskRequest(question="timing closure", mode="chunk"),
            user_id=data.alice.id,
        )
    finally:
        reset_request_user(token)

    anchor = next(item for item in response.anchors if item.object_type == "memory")
    citation = next(item for item in response.citations if item.memory_id == anchor.object_id)
    assert anchor.object_id == data.confirmed.id
    assert anchor.provenance == data.confirmed.provenance
    assert citation.source_id == ""
    assert citation.element_id == ""
    assert citation.provenance == data.confirmed.provenance
    assert data.candidate.title not in llm.prompts[-1]


def test_reasoning_synthesis_projects_confirmed_memory_as_first_class_anchor(memory_data):
    data = memory_data
    llm = _MemoryAnswerLLM()
    data.provider.chat_clients = {"ask_answer": llm}
    hits = data.retriever.notebook_memory_hits(
        data.alice.id, data.notebook.id, "timing closure", 8
    )

    answer, grounded, anchors, _counts = data.repo._runtime.ask_service()._answer_reasoning(
        data.notebook.id,
        "timing closure",
        [],
        [],
        memory_hits=hits,
    )

    assert answer and grounded
    assert [(item.object_type, item.object_id) for item in anchors] == [
        ("memory", data.confirmed.id)
    ]
    assert data.candidate.title not in llm.prompts[-1]


def test_reasoning_consumes_confirmed_memory_when_no_kg_exists(memory_data, monkeypatch):
    data = memory_data
    llm = _MemoryAnswerLLM()
    data.provider.chat_clients = {"ask_answer": llm}
    service = data.repo._runtime.ask_service()
    monkeypatch.setattr(service.candidates, "has_kg", lambda _nb: False)
    monkeypatch.setattr(service.candidates, "any_base_has_kg", lambda _nb: False)

    response = service.ask_reasoning(
        data.notebook.id,
        AskRequest(question="timing closure", mode="reasoning"),
        user_id=data.alice.id,
    )

    assert response.kg_required is False
    assert [(item.object_type, item.object_id) for item in response.anchors] == [
        ("memory", data.confirmed.id)
    ]
    assert response.citations[0].memory_id == data.confirmed.id


def test_knowledge_search_projects_confirmed_memory_only(memory_data):
    data = memory_data
    token = set_request_user(data.alice)
    try:
        hits = data.repo._runtime.knowledge_query.search(
            data.notebook.id, "timing closure", 30
        )
    finally:
        reset_request_user(token)

    memory_ids = {
        hit["object_id"] for hit in hits if hit.get("object_type") == "memory"
    }
    assert memory_ids == {data.confirmed.id}
    assert data.candidate.id not in memory_ids


def test_knowledge_search_unions_memory_before_limit_even_when_kg_fills_limit(
    memory_data, monkeypatch
):
    data = memory_data
    service = data.repo._runtime.knowledge_query
    monkeypatch.setattr(
        service.knowledge,
        "fts_search",
        # `**_kwargs` absorbs the corpus-language gate's `corpus_langs`; this
        # test is about the Memory union, not about which terms get probed.
        lambda _db, _nb, _query, _limit, **_kwargs: [
            {
                "object_id": "ko-low",
                "name": "Low KG hit",
                "object_type": "claim",
                "score": 0.01,
                "match": "lexical",
            }
        ],
    )
    monkeypatch.setattr(service, "semantic_search", lambda *_args: [])
    monkeypatch.setattr(service, "hydrate_search_hits", lambda _nb, hits: hits)
    monkeypatch.setattr(
        service, "fold_hits_to_canonical", lambda _nb, hits, _limit: hits
    )

    token = set_request_user(data.alice)
    try:
        hits = service.search(data.notebook.id, "timing closure", limit=1)
    finally:
        reset_request_user(token)

    assert len(hits) == 1
    assert hits[0]["object_type"] == "memory"
    assert hits[0]["object_id"] == data.confirmed.id


def test_memory_prompt_context_has_per_hit_and_total_caps_without_losing_provenance():
    hits = [
        MemoryHit(
            memory_id=f"memory-{index}",
            title="T" * 500,
            text=(str(index) * 5000),
            status="confirmed",
            authority=3,
            score=1.0 - index / 100,
            provenance={"full": "P" * 2000},
        )
        for index in range(8)
    ]

    block, id_map = MemoryRetriever.context(hits)

    assert len(block) <= MemoryRetriever.CONTEXT_TOTAL_CHAR_LIMIT
    assert all(
        len(line) <= MemoryRetriever.CONTEXT_HIT_CHAR_LIMIT
        for line in block.splitlines()
    )
    assert id_map["k3001"]["provenance"] == hits[0].provenance
    assert id_map["k3001"]["definition"] == hits[0].text


def test_notebook_search_projects_confirmed_memory_without_fake_source_ids(memory_data):
    data = memory_data
    token = set_request_user(data.alice)
    try:
        response = data.repo._runtime.catalog.search_notebook(
            data.notebook.id, "timing closure"
        )
    finally:
        reset_request_user(token)

    memory_hits = [item for item in response.hits if item.scope == "Memory"]
    assert [item.memory_id for item in memory_hits] == [data.confirmed.id]
    assert memory_hits[0].source_id == ""
    assert memory_hits[0].element_id == ""


def test_deep_report_corpus_map_projects_confirmed_memory_only(memory_data):
    data = memory_data
    token = set_request_user(data.alice)
    try:
        engine = data.repo._runtime.report_execution.engine_factory(
            user_id=data.alice.id, cancel_event=None
        )
        corpus = engine._build_corpus_map(data.notebook.id, "timing closure")
    finally:
        reset_request_user(token)

    assert data.confirmed.title in corpus
    assert data.candidate.title not in corpus


def test_quoted_query_splits_candidate_probe_from_phrase_scoring(memory_data):
    """引号在 Memory 上的分工:候选去引号、评分留引号。

    候选生成是对**整串**做 FTS/trigram 探测(不是拆好的词项表),引号字符直接进
    pattern 就什么都匹配不到——所以候选侧必须先把标记去掉。评分侧相反:拿到的
    是原串,短语仍须整段命中。

    每段被接受的短语还会作为**独立探测**一并下发(round-6),否则「只含该短语、
    不含整句」的记忆进不了候选池——那是 Memory 候选「整句作为一个 FTS 短语」的
    既有粗粒度,引号语法必须能穿过它。
    """
    data = memory_data
    retriever = data.repo._runtime.memory_retriever
    seen: list[str] = []
    original = retriever.store.memory_retrieval_rows

    def spy(user_id, notebook_id, statuses, query, **kwargs):
        seen.append((query, tuple(kwargs.get("phrase_queries", ()))))
        return original(user_id, notebook_id, statuses, query, **kwargs)

    retriever.store = SimpleNamespace(
        memory_retrieval_rows=spy,
        **{
            name: getattr(retriever.store, name)
            for name in dir(retriever.store)
            if not name.startswith("_") and name != "memory_retrieval_rows"
        },
    )
    token = set_request_user(data.alice)
    try:
        retriever.notebook_memory_hits(
            data.alice.id, data.notebook.id, 'how does "timing closure" work', 8
        )
    finally:
        reset_request_user(token)
        retriever.store = original.__self__

    # 探测串去掉标记,**并且**每段被接受的短语作为独立探测一并下发——后者是
    # round-6 的接线,少了它下面那条 store 级守卫照样绿(它自己传参),这一条才是
    # 「服务层真的把短语交下去了」的守卫。
    assert seen == [("how does timing closure work", ("timing closure",))]


def test_quoted_phrase_probes_memory_independently_of_the_sentence(memory_data):
    """带上下文的引号查询,必须能把「只含该短语、不含整句」的记忆捞进**候选池**。

    codex #410 round-6 P2:Memory 的候选是把**整串**当一个 FTS 短语探测,所以
    `how does "timing closure" work` 若只按整句探测,那条只写了 timing closure 的
    记忆根本进不了词法候选——引号语法在别处都生效、独独在 Memory 上失灵。短语作为
    额外的 OR 词项进同一条有界查询,不是多发一次查询。

    ⚠ 断言必须落在**词法候选**这一层:端到端的 hits 会经由「按 updated_at 取回的
    有界向量池」把同一条记忆捞回来,于是删掉短语探测也照样绿——第一版守卫正是这样
    打空的。这里把 vector_limit 压到最小,让向量池只可能带回最新的那一条(unrelated
    之后的 bob/other 不属本 notebook),差异才真正来自词法侧。
    """
    data = memory_data
    store = data.repo._runtime.memory_service.store
    sentence = "how does timing closure work"

    def probe(**kwargs):
        rows = store.memory_retrieval_rows(
            data.alice.id, data.notebook.id, ("confirmed",), sentence,
            lexical_limit=50, vector_limit=1, **kwargs,
        )
        return {row["record"].id for row in rows}

    without_phrase = probe()
    with_phrase = probe(phrase_queries=["timing closure"])

    assert data.confirmed.id not in without_phrase, (
        "整句作为一个 FTS 短语匹配不到它——这正是要修的形状")
    assert data.confirmed.id in with_phrase
