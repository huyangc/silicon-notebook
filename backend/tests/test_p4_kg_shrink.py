import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository, _now
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate
from tests.model_testkit import bind_all_embedding_clients
from tests.model_testkit import bind_chat_client


def test_kg_auto_extract_env_override(monkeypatch):
    monkeypatch.setenv("KG_AUTO_EXTRACT", "true")
    assert Settings().kg_auto_extract is True


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def _seed_concept(repo, nb_id, local_id="K1", name="Engram"):
    repo.store_kg(nb_id, None, [
        {"local_id": local_id, "object_type": "concept",
         "payload": {"name": name, "section_path": "1"}, "evidence": []},
    ], [])


def test_notebook_has_kg(repo):
    nb = repo.create_notebook(NotebookCreate(name="n"))
    assert repo._notebook_has_kg(nb.id) is False
    _seed_concept(repo, nb.id)
    assert repo._notebook_has_kg(nb.id) is True


def test_should_extract_false_when_auto_off_and_no_kg(repo):
    nb = repo.create_notebook(NotebookCreate(name="n"))
    assert repo._should_extract_kg(nb.id) is False


def test_should_extract_true_when_auto_on(repo, monkeypatch):
    monkeypatch.setattr(repo.settings, "kg_auto_extract", True)
    nb = repo.create_notebook(NotebookCreate(name="n"))
    assert repo._should_extract_kg(nb.id) is True


def test_should_extract_true_when_notebook_already_has_kg(repo):
    nb = repo.create_notebook(NotebookCreate(name="n"))
    _seed_concept(repo, nb.id)
    assert repo._should_extract_kg(nb.id) is True


# ---------------------------------------------------------------------------
# P4-3: build_notebook_kg
# ---------------------------------------------------------------------------

def _make_source(repo, nb_id, sid, status="extracted", with_elements=True):
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
            "file_name,file_path,file_size,file_hash,summary,doc_type,created_at,updated_at) "
            "VALUES (?,?,?,'markdown',?,'parsed','a.md','',0,'','','academic_paper',?,?)",
            (sid, nb_id, "Doc", status, _now(), _now()))
        if with_elements:   # 默认建「已成功 parse」源(有 elements);False = parse 未落地
            db.execute(
                "INSERT INTO source_elements (id,source_id,element_type,location_label,"
                "text,metadata,created_at) VALUES (?,?,'paragraph','p1','body','{}',?)",
                (f"el-{sid}", sid, _now()))
    return sid


def _configure_llm(repo):
    bind_chat_client(repo, "kg_extract", type(
        "C",
        (),
        {
            "configured": True,
            "chat_json": lambda self, messages, hint, **kwargs: '{"ok":true}',
        },
    )())


def _configure_ask(repo):
    bind_chat_client(repo, "ask_answer", type(
        "AnswerClient",
        (),
        {
            "configured": True,
            "chat_json": lambda self, messages, hint, **kwargs: (
                '{"answer":"ok","grounded":false}'
            ),
        },
    )())


def test_build_notebook_kg_runs_extraction_per_kgless_source(repo, monkeypatch):
    _configure_llm(repo)
    nb = repo.create_notebook(NotebookCreate(name="n"))
    s1 = _make_source(repo, nb.id, "s1")
    s2 = _make_source(repo, nb.id, "s2")
    calls = []
    monkeypatch.setattr(
        repo._runtime.source_ingestion,
        "run_extraction",
        lambda sid, **kwargs: calls.append(sid),
    )
    repo.build_notebook_kg(nb.id)
    assert set(calls) == {"s1", "s2"}


def test_build_notebook_kg_skips_sources_with_kg(repo, monkeypatch):
    _configure_llm(repo)
    nb = repo.create_notebook(NotebookCreate(name="n"))
    s1 = _make_source(repo, nb.id, "s1")
    repo.store_kg(nb.id, "s1", [
        {"local_id": "K1", "object_type": "concept",
         "payload": {"name": "X"}, "evidence": []}], [])   # s1 already has KG
    s2 = _make_source(repo, nb.id, "s2")
    calls = []
    monkeypatch.setattr(
        repo._runtime.source_ingestion,
        "run_extraction",
        lambda sid, **kwargs: calls.append(sid),
    )
    repo.build_notebook_kg(nb.id)
    assert calls == ["s2"]                                  # idempotent: skip s1


def test_build_notebook_kg_skips_sources_missing_elements(repo, monkeypatch):
    """无 source_elements 的源(parse 未落地)不进抽取 targets——避免接地校验空转
    (LLM 抽出的节点无 element 可绑、被整源丢弃 → objects=0);记入
    result['skipped_no_elements'],有 elements 的源正常抽。"""
    _configure_llm(repo)
    nb = repo.create_notebook(NotebookCreate(name="n"))
    _make_source(repo, nb.id, "s-ok")                              # 有 elements
    _make_source(repo, nb.id, "s-bad", with_elements=False)        # 无 elements
    calls = []
    monkeypatch.setattr(
        repo._runtime.source_ingestion,
        "run_extraction",
        lambda sid, **kwargs: calls.append(sid),
    )
    out = repo.build_notebook_kg(nb.id)
    assert calls == ["s-ok"]                                  # 只抽有 elements 的
    assert out["skipped_no_elements"] == ["s-bad"]            # 无 elements 记 skipped
    assert "s-bad" not in out["built"]


def test_build_notebook_kg_requires_llm(repo):
    bind_chat_client(repo, "kg_extract", type("C", (), {"configured": False})())
    nb = repo.create_notebook(NotebookCreate(name="n"))
    with pytest.raises(RuntimeError):
        repo.build_notebook_kg(nb.id)


def test_build_notebook_kg_reconciles_doc_type_changed_mid_extraction(repo, monkeypatch):
    """notebook KG 构建路径（_extract_one）也要有 doc_type 终态收口，和上传流水线
    process_source 共用同一套 _extract_reconciling_doc_type。

    run_extraction 开头就读走 doc_type 快照（它选抽取 profile、进抽取 prompt，因而进
    LLM 缓存键）。抽取跑到一半并发重传改了 doc_type，若无条件落 'extracted'，存库的
    新类型会配着**旧 profile 抽出的 KG**，而且没有任何东西回来纠正。收口改为：守卫落
    终态（WHERE doc_type=本轮值）→ rowcount 0（窗口里被改了）就带新类型再抽一轮。

    确定性注入：run_extraction 桩在第一次抽取（此刻这一行就是 'extracting'）里模拟并发
    retype——直接改 doc_type 列。跑完守卫比对发现列已变，带新类型补跑一轮，最终落库的
    doc_type 与真正用来抽取的那一轮一致。

    变异验证：把 _extract_one 的收口换回无条件
    ``self._set_source_status(source_id, "extracted")`` → 只抽一次、不补跑 →
    seen == ["academic_paper"] → 本测试转红。"""
    _configure_llm(repo)
    nb = repo.create_notebook(NotebookCreate(name="n"))
    sid = _make_source(repo, nb.id, "s1", status="parsed")   # doc_type='academic_paper'
    store = repo._runtime.source_store
    seen = []

    def fake_extract(source_id, **_kw):
        seen.append(repo.get_source(source_id).doc_type)
        if len(seen) == 1:
            # 抽取正在跑（此刻这一行就是 'extracting'）——模拟并发重传把类型改成 textbook。
            assert repo.get_source(source_id).parse_status == "extracting"
            store.set_doc_type(source_id, "textbook")

    monkeypatch.setattr(repo._runtime.source_ingestion, "run_extraction", fake_extract)

    repo.build_notebook_kg(nb.id)

    assert seen == ["academic_paper", "textbook"], (
        "抽取期间改的类型必须被补跑一次；只有一次说明构建路径缺 doc_type 终态收口"
    )
    assert repo.get_source(sid).doc_type == "textbook"
    assert repo.get_source(sid).parse_status == "extracted", "补跑完照样落终态"


# ---------------------------------------------------------------------------
# P4-4: base_kg_available signal
# ---------------------------------------------------------------------------

def test_any_base_notebook_has_kg(repo):
    active = repo.create_notebook(NotebookCreate(name="active"))
    base = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(base.id)
    repo.replace_notebook_bases(active.id, [base.id], "user-local")
    assert repo._any_base_notebook_has_kg(active.id) is False
    # personal notebook with KG must NOT count as base:
    pers = repo.create_notebook(NotebookCreate(name="p"))
    repo.store_kg(pers.id, None, [
        {"local_id": "P1", "object_type": "concept",
         "payload": {"name": "P"}, "evidence": []}], [])
    assert repo._any_base_notebook_has_kg(active.id) is False
    # now give the BASE notebook KG:
    repo.store_kg(base.id, None, [
        {"local_id": "B1", "object_type": "concept",
         "payload": {"name": "B"}, "evidence": []}], [])
    assert repo._any_base_notebook_has_kg(active.id) is True
    # unrelated notebook that never mounted base must NOT see the gate open:
    unmounted = repo.create_notebook(NotebookCreate(name="unmounted"))
    assert repo._any_base_notebook_has_kg(unmounted.id) is False


def test_base_kg_available_on_notebook_summary(repo):
    nb = repo.create_notebook(NotebookCreate(name="n"))
    assert repo.get_notebook(nb.id).base_kg_available is False
    base = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(base.id)
    repo.store_kg(base.id, None, [
        {"local_id": "B1", "object_type": "concept",
         "payload": {"name": "B"}, "evidence": []}], [])
    # 已发布且有 KG,但未挂载 → 门仍关(多领域基准库:不再隐式全局参与)。
    assert repo.get_notebook(nb.id).base_kg_available is False
    repo.replace_notebook_bases(nb.id, [base.id], "user-local")
    assert repo.get_notebook(nb.id).base_kg_available is True


# ---------------------------------------------------------------------------
# P4-5: 退役 fast/global 模式(graph 模式随后同样退役,并入同一批断言)
# ---------------------------------------------------------------------------

from app.services.ask_modes import resolve_mode, UnknownAskMode, ASK_MODES


def test_fast_global_graph_removed_from_registry():
    assert "fast" not in ASK_MODES
    assert "global" not in ASK_MODES
    assert "graph" not in ASK_MODES


def test_retired_modes_alias_to_chunk():
    assert resolve_mode("fast").id == "chunk"
    assert resolve_mode("global").id == "chunk"
    assert resolve_mode("graph").id == "chunk"


def test_strict_modes_and_default_intact():
    assert resolve_mode("reasoning").id == "reasoning"
    assert resolve_mode(None).id == "chunk"


def test_unknown_mode_still_raises():
    with pytest.raises(UnknownAskMode):
        resolve_mode("bogus")


# ---------------------------------------------------------------------------
# P4-6: 严格推理门控(kg_required) + federated_retrieve 接通
# ---------------------------------------------------------------------------

from app.models.schemas import AskRequest


def test_strict_blocked_when_no_kg_no_base(repo):
    _configure_ask(repo)
    nb = repo.create_notebook(NotebookCreate(name="n"))
    resp = repo.ask_reasoning(nb.id, AskRequest(question="q", mode="reasoning"))
    assert resp.kg_required is True
    assert resp.reasoning_trace is None       # did NOT run the agentic loop


def test_strict_no_kg_keeps_the_trace_it_already_streamed(repo):
    """有流消费者时,短路响应必须带上已经推送出去的那几步。

    否则 final 事件替换在途 turn 的同一刻,用户刚看着走过的「理解」步就被抹掉,
    重开会话的历史里也留不下(codex 第 2 轮 P2)。没有 on_trace 的直调仍保持
    上面那条的语义:空轨迹 = agentic loop 没跑。"""
    _configure_ask(repo)
    nb = repo.create_notebook(NotebookCreate(name="n"))
    streamed = []
    resp = repo.ask_reasoning(
        nb.id, AskRequest(question="q", mode="reasoning"), streamed.append,
    )
    assert resp.kg_required is True
    assert [step.step_type for step in streamed] == ["intent"]
    assert [step.step_type for step in (resp.reasoning_trace or [])] == ["intent"]


def test_short_circuit_run_never_claims_memory_was_used(repo, monkeypatch):
    """没产出答案的那几轮不得留下记忆痕迹(codex 第 4 轮 P2)。

    记忆步排在所有短路返回之后。若排在前面,「未配模型」这类根本没发生合成的
    轮次也会持久化一条记忆记录,读起来就像私有记忆参与了一个并不存在的答案。"""
    from app.models.memory import MemoryHit
    from app.services.memory_retrieval import MemoryRetriever

    _configure_ask(repo)
    monkeypatch.setattr(
        MemoryRetriever, "notebook_memory_hits",
        lambda self, user_id, notebook_id, query, limit=8: [
            MemoryHit(memory_id="m1", title="t", text="t", status="confirmed",
                      authority=3, score=0.5),
        ],
    )
    # 「运维配了系统模型服务,却漏绑 ask_answer」—— 那条短路的判据就是它。
    monkeypatch.setattr(
        type(repo._runtime.models), "primary_unconfigured", lambda self: True,
    )
    nb = repo.create_notebook(NotebookCreate(name="n"))
    streamed = []
    resp = repo.ask_reasoning(
        nb.id, AskRequest(question="q", mode="reasoning"), streamed.append,
    )
    assert resp.llm_mode == "deterministic"          # 确实走了短路,没有合成
    assert "memory" not in [step.step_type for step in streamed]
    assert "memory" not in [step.step_type for step in (resp.reasoning_trace or [])]
    # 已经推给客户端的 intent 步仍然要留在 final 里(第 2 轮 P2 的那条不能回退)。
    assert [step.step_type for step in (resp.reasoning_trace or [])] == ["intent"]


def test_strict_allowed_when_base_has_kg(repo):
    _configure_ask(repo)
    base = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(base.id)
    repo.store_kg(base.id, None, [
        {"local_id": "B1", "object_type": "concept",
         "payload": {"name": "Engram"}, "evidence": []}], [])
    empty = repo.create_notebook(NotebookCreate(name="empty"))   # own KG empty
    repo.replace_notebook_bases(empty.id, [base.id], "user-local")
    resp = repo.ask_reasoning(empty.id, AskRequest(question="Engram", mode="reasoning"))
    assert resp.kg_required is False           # mounted base satisfies the gate


def test_reasoning_search_federates_base(repo):
    from app.services.reasoning_retrieval import ReasoningRetriever
    base = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(base.id)
    repo.store_kg(base.id, None, [
        {"local_id": "B1", "object_type": "concept",
         "payload": {"name": "Engram"}, "evidence": []}], [])
    empty = repo.create_notebook(NotebookCreate(name="empty"))
    repo.replace_notebook_bases(empty.id, [base.id], "user-local")
    hits = ReasoningRetriever.from_repository(repo, repo.settings).search(empty.id, "Engram")
    names = {h.payload.get("name") for h in hits}
    assert "Engram" in names                   # base hit surfaced via federation


# ---------------------------------------------------------------------------
# Citation.tier: reasoning-mode citations must reflect the federated hit's
# tier, not silently default to personal. 真机 bug:DeepSeek-V4 一次 reasoning
# 问答 12 条 citations 里 8 条来自 base 库,前端徽章却只见 personal——根因是
# _citations_from/_citation 完全不读 RetrievedKnowledge.tier(federated_retrieve
# 早就打好了标)。这里直接跑 federated_retrieve → _citations_from(与
# ask_reasoning 12118-12120 同一调用形状),不依赖 LLM 配置。
# ---------------------------------------------------------------------------

_BASE_EVIDENCE = [{"source_id": "src-base", "source_title": "BaseDoc",
                   "element_id": "el-base-0001", "element_type": "paragraph",
                   "location_label": "1", "quoted_span": "Engram is a memory trace",
                   "confidence": 1.0}]
_PERSONAL_EVIDENCE = [{"source_id": "src-own", "source_title": "OwnDoc",
                       "element_id": "el-own-0001", "element_type": "paragraph",
                       "location_label": "1", "quoted_span": "my own note on Engram",
                       "confidence": 1.0}]


def test_citations_from_reasoning_hits_carries_base_tier(repo):
    base = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(base.id)
    repo.store_kg(base.id, None, [
        {"local_id": "B1", "object_type": "concept",
         "payload": {"name": "Engram"}, "evidence": _BASE_EVIDENCE}], [])
    active = repo.create_notebook(NotebookCreate(name="active"))
    repo.store_kg(active.id, None, [
        {"local_id": "A1", "object_type": "concept",
         "payload": {"name": "Engram encoding"}, "evidence": _PERSONAL_EVIDENCE}], [])
    repo.replace_notebook_bases(active.id, [base.id], "user-local")

    # Mirrors ask_reasoning's own call shape (sqlite_repository.py ~12118-12120):
    # top_hits federated across base ⊕ active, then bound to Citation objects.
    top_hits = repo.retrieval.federated_retrieve(active.id, "Engram")
    cited_element_ids = {ev.element_id for item in top_hits for ev in item.evidence}
    citations = repo._citations_from(
        top_hits, cited_element_ids, "KG evidence", notebook_id=active.id)

    tier_by_source = {c.source_id: c.tier for c in citations}
    assert tier_by_source.get("src-base") == "base", (
        f"base 库命中的 citation.tier 应为 base,实为 {tier_by_source.get('src-base')} "
        f"(全部 citations: {[(c.source_id, c.tier) for c in citations]})")
    assert tier_by_source.get("src-own") == "personal", (
        f"active 库自己命中的 citation.tier 应为 personal,实为 {tier_by_source.get('src-own')}")


# codex r4 review: citations_from() 此前只对 chunk_context/knowledge_context/
# render_follow_chain_context 三条路径做了「命中的 notebook_id 等于调用方
# active notebook_id 就归零」的归一化,citations_from 自己漏了——评审当时误判
# 「不可达」,实际可达:上面 test_citations_from_reasoning_hits_carries_base_tier
# 已经证明 federated_retrieve 对 active 自己的命中同样会打上 active 自己的
# notebook_id(_federated_retrieve_impl 对 participant_notebook_ids 里的每一本
# 都无条件执行 h.notebook_id = nid,首项恒为 active 自己),citations_from 原样
# 透传就会让「本库自己」的证据带一个非空 notebook_id——当答案合成失败/模型没
# 吐出任何 [k] 锚点时,前端 buildAnswerReferences 回退展示这批 citation(见
# frontend/app/answer-formatting.ts 的 `if (references.length > 0) return
# references;` 之后的 citations 兜底分支),会渲染出一个多余的
# 「来自「当前笔记本」」徽章。这里复刻 test_citations_from_reasoning_hits_
# carries_base_tier 同一套 fixture(真实 federated_retrieve,非假协作者,与
# ask_reasoning 12118-12120 同一调用形状),额外验证 notebook_id 字段本身。
def test_citations_from_reasoning_hits_blanks_self_notebook_id(repo):
    base = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(base.id)
    repo.store_kg(base.id, None, [
        {"local_id": "B1", "object_type": "concept",
         "payload": {"name": "Engram"}, "evidence": _BASE_EVIDENCE}], [])
    active = repo.create_notebook(NotebookCreate(name="active"))
    repo.store_kg(active.id, None, [
        {"local_id": "A1", "object_type": "concept",
         "payload": {"name": "Engram encoding"}, "evidence": _PERSONAL_EVIDENCE}], [])
    repo.replace_notebook_bases(active.id, [base.id], "user-local")

    # Mirrors ask_reasoning's own call shape (sqlite_repository.py ~12118-12120).
    top_hits = repo.retrieval.federated_retrieve(active.id, "Engram")
    cited_element_ids = {ev.element_id for item in top_hits for ev in item.evidence}
    citations = repo._citations_from(
        top_hits, cited_element_ids, "KG evidence", notebook_id=active.id)

    nb_by_source = {c.source_id: c.notebook_id for c in citations}
    assert nb_by_source.get("src-own") == "", (
        "active 库自己命中(src-own)的 citation.notebook_id 必须归一成空串"
        f"(不是「跨库」),实为 {nb_by_source.get('src-own')!r}——否则前端会显示"
        "一个多余的「来自「当前笔记本」」徽章。")
    assert nb_by_source.get("src-base") == base.id, (
        f"真正跨库命中(src-base 来自 base)必须原样带出 notebook_id,实为 "
        f"{nb_by_source.get('src-base')!r}(应为 {base.id!r})")


def test_tier_map_for_batches_and_defaults_unknown_to_personal(repo):
    base = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(base.id)
    personal = repo.create_notebook(NotebookCreate(name="personal"))

    tier_map = repo._tier_map_for({base.id, personal.id, "nonexistent-nb-id"})

    assert tier_map[base.id] == "base"
    assert tier_map[personal.id] == "personal"
    # Unknown ids are simply absent (caller's .get(..., "personal") supplies the
    # safe default) — _tier_map_for itself only returns rows it actually found.
    assert "nonexistent-nb-id" not in tier_map


def test_tier_map_for_empty_input_returns_empty_dict_without_querying(repo):
    assert repo._tier_map_for(set()) == {}
    assert repo._tier_map_for([]) == {}
