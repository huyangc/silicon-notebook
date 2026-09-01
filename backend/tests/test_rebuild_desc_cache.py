"""Tests for the incremental cache + parallelization of concept-description
generation in rebuild_unified_kg (perf fix).

The description path only fires for multi-member (cross-doc merged) canonicals.
We build that by storing the SAME normalized concept name from two "sources",
each carrying evidence with a quoted_span, so the rebuild fuses them into one
canonical with 2 members and asks the (fake) LLM for a description.
"""
import json
import threading
import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository, _concept_desc_sig
from tests.model_testkit import RecordingModelProvider, bind_chat_client
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    provider = RecordingModelProvider()
    r = SQLiteRepository(Settings(), model_provider=provider)
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))  # inject; no real model loads (lazy)
    r.recording_model_provider = provider
    return r


class _DescLLM:
    """Fake KG LLM: counts description calls and returns a distinct description
    per concept name so we can assert which cluster got which description.
    Thread-safe counter because PHASE 2 runs chat_json concurrently."""

    configured = True

    def __init__(self):
        self.calls = 0
        self.names_seen = []
        self._lock = threading.Lock()

    def chat_json(self, messages, schema):
        content = messages[0]["content"]
        # concept_description_prompt embeds "Concept: <name>\n"
        name = ""
        for line in content.splitlines():
            if line.startswith("Concept: "):
                name = line[len("Concept: "):].strip()
                break
        with self._lock:
            self.calls += 1
            self.names_seen.append(name)
        return json.dumps({"description": f"desc-for::{name}"})


def _concept_with_evidence(local_id, name, span, source_title="D"):
    return {
        "local_id": local_id,
        "object_type": "concept",
        "payload": {"name": name, "section_path": "1"},
        "evidence": [{
            "source_id": "s", "source_title": source_title,
            "element_id": "e", "element_type": "p", "location_label": "1",
            "quoted_span": span, "confidence": 1.0,
        }],
    }


def _make_merged_notebook(repo, name="MOSFET", spans=("span-a", "span-b")):
    """Create a notebook whose two sources share a normalized concept name so
    they fuse into ONE canonical with 2 members (fires the description path)."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, "s1", [_concept_with_evidence("a", name, spans[0])], [])
    repo.store_kg(nb.id, "s2", [_concept_with_evidence("b", name.lower(), spans[1])], [])
    return nb


def _cluster_descs(repo, nb_id):
    """canonical_id -> (canonical_description, canonical_desc_sig) for concepts."""
    with repo._connect() as db:
        rows = db.execute(
            "SELECT DISTINCT canonical_id, canonical_description, canonical_desc_sig "
            "FROM concept_clusters WHERE notebook_id=? AND object_type='concept'",
            (nb_id,)).fetchall()
    return {r["canonical_id"]: (r["canonical_description"], r["canonical_desc_sig"]) for r in rows}


# --- 1. signature purity ----------------------------------------------------

def test_concept_desc_sig_deterministic_and_order_insensitive():
    # order-insensitive (we sort the quote set before hashing)
    assert _concept_desc_sig("N", ["a", "b"]) == _concept_desc_sig("N", ["b", "a"])
    # deterministic (same inputs -> same sig)
    assert _concept_desc_sig("N", ["a", "b"]) == _concept_desc_sig("N", ["a", "b"])
    # changing a quote changes the sig
    assert _concept_desc_sig("N", ["a", "b"]) != _concept_desc_sig("N", ["a", "c"])
    # changing the name changes the sig
    assert _concept_desc_sig("N", ["a", "b"]) != _concept_desc_sig("M", ["a", "b"])


# --- 6. migration -----------------------------------------------------------

# --- 4. parallel correctness / behavior preserved ---------------------------

def test_all_multimember_canonicals_get_descriptions(repo):
    llm = _DescLLM()
    bind_chat_client(repo, "kg_concept_description", llm)
    # two independent merged concepts (each 2 members across sources)
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, "s1", [
        _concept_with_evidence("a1", "MOSFET", "mosfet is a transistor"),
        _concept_with_evidence("b1", "cascode", "cascode boosts rout"),
    ], [])
    repo.store_kg(nb.id, "s2", [
        _concept_with_evidence("a2", "mosfet", "the MOSFET conducts"),
        _concept_with_evidence("b2", "Cascode", "cascode stage"),
    ], [])
    repo.rebuild_unified_kg(nb.id)

    descs = _cluster_descs(repo, nb.id)
    # both canonicals populated with the fake's per-name description
    populated = {d for (d, _sig) in descs.values() if d}
    assert "desc-for::MOSFET" in populated
    assert "desc-for::cascode" in populated
    assert llm.calls == 2   # exactly one LLM call per multi-member canonical
    # sigs persisted non-empty for the described canonicals
    assert all(sig for (d, sig) in descs.values() if d)


def test_parallel_concept_descriptions_propagate_model_artifact_scope(repo):
    from app.services.model_work import (
        ModelPriority,
        make_model_work_context,
    )

    class _ScopeCapturingDescLLM(_DescLLM):
        def __init__(self):
            super().__init__()
            self.notebook_ids = []

        def chat_json(self, messages, schema):
            context = make_model_work_context(
                workload_id="kg_concept_description",
                priority=ModelPriority.BACKGROUND,
            )
            with self._lock:
                self.notebook_ids.append(context.notebook_id)
            return super().chat_json(messages, schema)

    llm = _ScopeCapturingDescLLM()
    bind_chat_client(repo, "kg_concept_description", llm)
    nb = _make_merged_notebook(repo)

    repo.rebuild_unified_kg(nb.id)

    assert llm.notebook_ids == [nb.id]


# --- 2. cache reuse ---------------------------------------------------------

def test_rebuild_reuses_cached_descriptions(repo):
    llm = _DescLLM()
    bind_chat_client(repo, "kg_concept_description", llm)
    nb = _make_merged_notebook(repo)

    repo.rebuild_unified_kg(nb.id)
    first_calls = llm.calls
    assert first_calls >= 1
    before = _cluster_descs(repo, nb.id)
    assert any(d for (d, _s) in before.values())          # description persisted
    assert all(sig for (d, sig) in before.values() if d)   # with a non-empty sig

    # Rebuild again with NO changes -> zero new description calls, values preserved
    repo.rebuild_unified_kg(nb.id)
    assert llm.calls == first_calls                       # no new LLM calls
    after = _cluster_descs(repo, nb.id)
    assert {d for (d, _s) in after.values()} == {d for (d, _s) in before.values()}
    assert {sig for (_d, sig) in after.values()} == {sig for (_d, sig) in before.values()}


# --- 3. invalidation --------------------------------------------------------

def test_rebuild_regenerates_only_changed_cluster(repo):
    llm = _DescLLM()
    bind_chat_client(repo, "kg_concept_description", llm)
    # two independent merged concepts
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, "s1", [
        _concept_with_evidence("a1", "MOSFET", "mosfet A"),
        _concept_with_evidence("b1", "cascode", "cascode A"),
    ], [])
    repo.store_kg(nb.id, "s2", [
        _concept_with_evidence("a2", "mosfet", "mosfet B"),
        _concept_with_evidence("b2", "Cascode", "cascode B"),
    ], [])
    repo.rebuild_unified_kg(nb.id)
    assert llm.calls == 2
    baseline = _cluster_descs(repo, nb.id)

    # Change ONE cluster's evidence quotes (mutate a MOSFET member's quoted_span)
    # so ONLY the MOSFET canonical's sig changes; cascode stays byte-identical.
    with repo._write() as db:
        row = db.execute(
            "SELECT id, evidence FROM knowledge_objects "
            "WHERE notebook_id=? AND object_type='concept' "
            "AND json_extract(payload,'$.name')='MOSFET'", (nb.id,)).fetchone()
        ev = json.loads(row["evidence"])
        ev[0]["quoted_span"] = "mosfet A CHANGED"
        db.execute("UPDATE knowledge_objects SET evidence=? WHERE id=?",
                   (json.dumps(ev), row["id"]))

    # force=True here because THIS test mutates raw DB state directly (a bare
    # UPDATE on knowledge_objects.evidence) — a path that deliberately bypasses the
    # normal mutator, so it does NOT go through _mark_unified_kg_dirty and does NOT
    # bump kg_mutation_seq. Real evidence changes flow through store_kg/
    # _run_extraction, which DO bump the seq and would invalidate the gate on their
    # own. force is exercising the description sub-cache in isolation, NOT masking a
    # product bug (the outer gate is correct; the raw poke is just off-path).
    repo.rebuild_unified_kg(nb.id, force=True)
    # exactly ONE additional description call (the MOSFET cluster only)
    assert llm.calls == 3, f"expected 1 new call, names_seen={llm.names_seen}"
    assert llm.names_seen[-1] == "MOSFET"

    after = _cluster_descs(repo, nb.id)
    # cascode canonical's (desc, sig) unchanged (reused from cache)
    def _find(descs, wanted):
        for cid, (d, sig) in descs.items():
            if d == wanted:
                return (d, sig)
        return None
    assert _find(after, "desc-for::cascode") == _find(baseline, "desc-for::cascode")


# --- 5. progress callback ---------------------------------------------------

def test_progress_callback_invoked_per_work_item(repo):
    llm = _DescLLM()
    bind_chat_client(repo, "kg_concept_description", llm)
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, "s1", [
        _concept_with_evidence("a1", "MOSFET", "m1"),
        _concept_with_evidence("b1", "cascode", "c1"),
    ], [])
    repo.store_kg(nb.id, "s2", [
        _concept_with_evidence("a2", "mosfet", "m2"),
        _concept_with_evidence("b2", "Cascode", "c2"),
    ], [])

    events = []
    lock = threading.Lock()

    def progress(phase, i, n):
        with lock:
            events.append((phase, i, n))

    repo.rebuild_unified_kg(nb.id, progress=progress)

    work_n = 2   # two multi-member canonicals need descriptions
    # The progress channel now ALSO carries sub-stage banners (i==0, n==0);
    # isolate the concept_desc per-work-item progress events (n > 0).
    item_events = [(p, i, n) for (p, i, n) in events if n > 0]
    assert len(item_events) == work_n
    assert all(phase == "concept_desc" for (phase, _i, _n) in item_events)
    assert all(n == work_n for (_p, _i, n) in item_events)
    # final progress reports i == n (completed all)
    assert max(i for (_p, i, _n) in item_events) == work_n
    # i values are the 1..n sequence (order may vary due to concurrency)
    assert sorted(i for (_p, i, _n) in item_events) == list(range(1, work_n + 1))


# --- 7. 证据取数批量化(审计批4) -------------------------------------------
#
# 逐条时代:每个多成员 canonical 一次 cluster_evidence_rows 往返,10⁵–10⁶ 次全部串行堵在
# LLM 阶段之前。现在按 canonical 分批(≤_DESC_EVIDENCE_CID_BATCH 个 / ≤_DESC_EVIDENCE_SEED_BATCH
# 个 seed)一次 IN 查回,按行上的 seed 分组回内存。这一组用例钉三件事:
#   ① 往返次数真的降到 ⌈N/批⌉;
#   ② 分组不串组——每个 canonical 的 prompt 只含它自己的引文(批量化唯一的新失败模式);
#   ③ 切批保序、双上限都生效(work 顺序 = LLM 阶段输入,必须与逐条时代逐位一致)。

class _PromptRecordingLLM(_DescLLM):
    """在 _DescLLM 之上记下每次调用的完整 prompt,用于断言引文没有串组。"""

    def __init__(self):
        super().__init__()
        self.prompt_by_name = {}

    def chat_json(self, messages, schema):
        raw = super().chat_json(messages, schema)
        content = messages[0]["content"]
        for line in content.splitlines():
            if line.startswith("Concept: "):
                with self._lock:
                    self.prompt_by_name[line[len("Concept: "):].strip()] = content
                break
        return raw


def _spy_cluster_evidence_rows(monkeypatch):
    """统计 cluster_evidence_rows 的往返次数与每次问了多少 seed。"""
    from app.repositories.sqlite.unified_kg_store import UnifiedKgStore

    calls = []
    real = UnifiedKgStore.cluster_evidence_rows

    def spy(db, notebook_id, run_id, seeds):
        values = list(seeds)
        calls.append(len(values))
        return real(db, notebook_id, run_id, values)

    monkeypatch.setattr(UnifiedKgStore, "cluster_evidence_rows", staticmethod(spy))
    return calls


def test_concept_desc_evidence_is_fetched_in_one_batch(repo, monkeypatch):
    """4 个多成员 canonical → **一次** cluster_evidence_rows 往返(远小于批上限),
    且每个 canonical 仍拿到且只拿到自己的引文。

    变异锚点:把批量循环改回「for cid in eligible: 一次 cluster_evidence_rows」的逐条
    形态 → len(calls) 变成 4,断言红。"""
    llm = _PromptRecordingLLM()
    bind_chat_client(repo, "kg_concept_description", llm)
    calls = _spy_cluster_evidence_rows(monkeypatch)

    names = ["MOSFET", "cascode", "bandgap", "chopper"]
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, "s1", [
        _concept_with_evidence(f"a{i}", n, f"{n}-span-first") for i, n in enumerate(names)
    ], [])
    repo.store_kg(nb.id, "s2", [
        _concept_with_evidence(f"b{i}", n.lower(), f"{n}-span-second")
        for i, n in enumerate(names)
    ], [])
    repo.rebuild_unified_kg(nb.id)

    assert len(calls) == 1, f"expected one batched round trip, got {calls}"
    assert calls[0] == 4          # 一次问齐 4 个 canonical 的 seed
    assert llm.calls == 4         # LLM 调用次数一个没变(每个 canonical 一次)
    # 分组正确:每份 prompt 只含本 concept 的两条引文,不含任何别的 concept 的。
    for n in names:
        prompt = llm.prompt_by_name[n]
        assert f"{n}-span-first" in prompt and f"{n}-span-second" in prompt
        for other in names:
            if other != n:
                assert f"{other}-span-" not in prompt, (
                    f"{other} 的引文串进了 {n} 的 prompt"
                )


def _flat_totals(seeds_by_cid, per_cid=1):
    """每个 canonical 的总成员数(第三条上限的输入)。默认 1,让前两条上限单独可测。"""
    return {c: per_cid for c in seeds_by_cid}


def test_desc_evidence_batches_preserve_order_and_honour_all_caps(monkeypatch):
    """切批必须**保序**(work 顺序 = 喂给 LLM 阶段的输入,要与逐条时代逐位一致),
    canonical 数与 seed 数两个上限都要生效,单个超大 canonical 独占一批。"""
    from app.services import knowledge_lifecycle as kl

    monkeypatch.setattr(kl, "_DESC_EVIDENCE_CID_BATCH", 3)
    monkeypatch.setattr(kl, "_DESC_EVIDENCE_SEED_BATCH", 4)

    # 每个 canonical 一个 seed:cid 上限(3)先到。
    cids = [f"c{i}" for i in range(7)]
    one_seed = {c: [f"{c}-s"] for c in cids}
    batches = list(kl._desc_evidence_batches(cids, one_seed, _flat_totals(one_seed)))
    assert batches == [["c0", "c1", "c2"], ["c3", "c4", "c5"], ["c6"]]
    assert [c for b in batches for c in b] == cids        # 保序、不丢不重

    # 每个 canonical 两个 seed:seed 上限(4)先到,每批只装得下 2 个 canonical。
    two_seeds = {c: [f"{c}-s1", f"{c}-s2"] for c in cids}
    batches = list(kl._desc_evidence_batches(cids, two_seeds, _flat_totals(two_seeds)))
    assert batches == [["c0", "c1"], ["c2", "c3"], ["c4", "c5"], ["c6"]]
    assert [c for b in batches for c in b] == cids

    # 自身 seed 数就超上限的 canonical 独占一批(= 逐条时代对它的原样行为),
    # 且不会把它前后的 canonical 一起吞进来。
    fat = {"c0": ["a"], "c1": [f"x{i}" for i in range(9)], "c2": ["b"]}
    assert list(kl._desc_evidence_batches(["c0", "c1", "c2"], fat, _flat_totals(fat))) == [
        ["c0"], ["c1"], ["c2"],
    ]


def test_desc_evidence_batches_cap_the_rows_the_batch_will_fetch(monkeypatch):
    """⚠ 第三条上限(评审):前两条只数**参数**(往返数、绑定变量),数不出「一个 hub
    canonical 有上千成员」。cluster_evidence_rows 每个成员返一行整份 evidence JSON,所以
    「300 个 canonical / 900 个 seed」的一批照样能拉回几十万行——载荷必须自己有上限。

    变异锚点:删掉 ``rows + r > _DESC_EVIDENCE_ROW_BATCH`` 这一支(或把
    ``_DESC_EVIDENCE_ROW_BATCH`` 调到无穷大)→ 本条红。"""
    from app.services import knowledge_lifecycle as kl

    # 把前两条上限调到不会先触发,单独测第三条。
    monkeypatch.setattr(kl, "_DESC_EVIDENCE_CID_BATCH", 1000)
    monkeypatch.setattr(kl, "_DESC_EVIDENCE_SEED_BATCH", 100000)
    monkeypatch.setattr(kl, "_DESC_EVIDENCE_ROW_BATCH", 100)

    cids = [f"c{i}" for i in range(5)]
    seeds = {c: [f"{c}-s"] for c in cids}         # 每个只有 1 个 seed → 前两条永不触发
    totals = {c: 40 for c in cids}                # 但每个带 40 个成员 → 每批至多 2 个
    batches = list(kl._desc_evidence_batches(cids, seeds, totals))
    assert batches == [["c0", "c1"], ["c2", "c3"], ["c4"]]
    assert [c for b in batches for c in b] == cids            # 保序、不丢不重
    assert all(sum(totals[c] for c in b) <= 100 for b in batches)

    # 单个 canonical 自己就超行上限时独占一批(= 逐条时代对它的原样行为),
    # 且不会把前后的 canonical 一起吞进来。
    hub_totals = {"c0": 10, "c1": 900, "c2": 10}
    hub_seeds = {c: [f"{c}-s"] for c in hub_totals}
    assert list(
        kl._desc_evidence_batches(["c0", "c1", "c2"], hub_seeds, hub_totals)
    ) == [["c0"], ["c1"], ["c2"]]


def test_desc_evidence_batches_require_complete_lookup_tables():
    """两张表都**直取**(不 ``.get`` 兜底):缺键说明上游算错了,应当响亮失败——兜底成 0 会
    被当成「0 个 seed / 0 行」悄悄放大批容量。调用方下一行就是 ``seeds_by_cid[cid]``。"""
    from app.services import knowledge_lifecycle as kl

    with pytest.raises(KeyError):
        list(kl._desc_evidence_batches(["c0"], {}, {"c0": 1}))
    with pytest.raises(KeyError):
        list(kl._desc_evidence_batches(["c0"], {"c0": ["s"]}, {}))
