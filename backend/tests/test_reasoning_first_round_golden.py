"""首轮检索的**逐字等价**黄金门（结构项 B8）。

`ReasoningRetriever.run` 的首轮（run 状态初始化 → 理解块/打法块/集合地图注入 →
规划 → 初检索 → PPR seed → 精确查找 seed → 空证据兜底 → 已确认方向补种）被抽成
独立阶段时，唯一能证明「行为零变化」的东西不是逐行读 diff，而是把改动前的轨迹与
请求计数录成黄金件、改动后逐字比对。

录的是**三样**：
  * 轨迹步的 `step_type` / `summary` / `detail` 键集合（顺序敏感）——它同时钉住
    步的种类、顺序、措辞与每一步的计数（summary 里带数字），以及 result_ids 这类
    稀疏键的出现与否；
  * 每个被检索通道的调用次数（含 embedding 往返）——钉住「请求数不变」；
  * 结果规模（候选/原文/元素/attempted 账目）。

`detail` 默认只录**键集合**而不录值：值里含 object_id / chunk_id 这类随 seed 数据
变化的句柄，录值会把黄金件变成「数据库长什么样」的快照而不是「首轮做了什么」的
快照，计数由 summary 那一半承担。唯一的例外是 `_DETAIL_VALUE_WHITELIST`——一份
不含句柄的数字/布尔/枚举键白名单（`answer.{top_n,quota,kg,elements,chains,
enumerations,enumerated_items}` 与 `reflect.{no_progress,stale,sufficient,
next_action}`），这些键额外把**实际值**录进 `detail_values`：它们是计数/开关而
不是句柄，不随 seed 数据变化，而录值才能钉住"首轮 → reflect 循环"之间几处只
交接、不被任何断言消费就永远不会变红的状态（`state.steps`/`state.elements_
searches`/`state.used_queries` 的写入时序，B8 quality P2-1/P2-2）。
`result_ids`/`anchor_evidence_ids` 这类句柄键不在白名单内，仍然只进
`detail_keys`。

重录：`REGEN_FIRST_ROUND_GOLDEN=1 PYTHONPATH=backend .venv/bin/python -m pytest \
backend/tests/test_reasoning_first_round_golden.py -q`
（重录前必须先确认当前代码就是你要当成基准的那一版。）
"""
from __future__ import annotations

import collections
import json
import os
from pathlib import Path

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients, bind_chat_client

GOLDEN_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "reasoning_first_round_golden.json"
)


class _CountingEmbedder(FakeEmbedder):
    """FakeEmbedder + 调用计数：embedding 往返也是「请求数」的一部分。"""

    def __init__(self, counts, dim=16):
        super().__init__(dim=dim)
        self._counts = counts

    def embed_query(self, text):
        self._counts["embed_query"] += 1
        return super().embed_query(text)

    def embed_texts(self, texts):
        self._counts["embed_texts"] += 1
        return super().embed_texts(texts)


class _CountingRetrievalPort:
    """透明代理：把 `RetrievalPort` 上每次方法调用记一笔。

    用 `__getattr__` 而不是逐个包装，是为了让将来新增的检索方法自动进入计数——
    黄金件因此能钉住「首轮没有偷偷多发一次别的查询」。
    """

    def __init__(self, inner, counts):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_counts", counts)

    def __getattr__(self, name):
        attr = getattr(object.__getattribute__(self, "_inner"), name)
        if not callable(attr):
            return attr
        counts = object.__getattribute__(self, "_counts")

        def _wrapper(*args, **kwargs):
            counts[f"retrieval.{name}"] += 1
            return attr(*args, **kwargs)

        return _wrapper


class _StubLLM:
    """plan 出一个子查询；reflect 按 `reflect_sequence` 逐次决定动作，用尽后
    重复最后一项。

    省略 `reflect_sequence`（默认）时 reflect 永远直接作答 —— 首轮之后立刻
    收尾，四个既有场景的黄金值逐字不变。

    带 `reflect_sequence` 的两个新场景专为钉住首轮 → reflect 循环之间此前从未
    被观测的两处交接（B8 quality P2-1）：`state.steps`（预算耗尽场景——reflect
    连续选 add_subquery，直到 `run()` 自己的步数预算耗尽为止）与
    `state.elements_searches`（search_elements 场景——reflect 的第一次
    search_elements 动作命中的是它从首轮空证据兜底继承来的计数，不是自己新
    累积出来的）。
    """

    configured = True

    def __init__(self, counts, reflect_sequence=None):
        self._counts = counts
        self._reflect_sequence = reflect_sequence
        self._reflect_calls = 0

    def chat_json(self, messages, schema_hint, **kwargs):
        if "sub_queries" in schema_hint:
            self._counts["llm.plan"] += 1
            return json.dumps({"sub_queries": [{"query": "MoE 架构"}]})
        if "next_action" in schema_hint:
            self._counts["llm.reflect"] += 1
            if self._reflect_sequence:
                idx = min(self._reflect_calls, len(self._reflect_sequence) - 1)
                self._reflect_calls += 1
                return json.dumps(self._reflect_sequence[idx])
            return json.dumps({"next_action": "answer", "sufficient": True})
        self._counts["llm.other"] += 1
        return json.dumps({"answer": "见 [k1]。", "grounded": True})


# 需要**录值**（而不只录键）的白名单：只收不含句柄的数字/布尔/枚举字段——
# `result_ids`/`anchor_evidence_ids` 这类值随 seed 数据变化的句柄键刻意不在
# 这里，仍然只进 `detail_keys`（见模块 docstring）。
_DETAIL_VALUE_WHITELIST = {
    "answer": ("top_n", "quota", "kg", "elements", "chains",
               "enumerations", "enumerated_items"),
    "reflect": ("no_progress", "stale", "sufficient", "next_action"),
}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'golden.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings(_env_file=None))


def _seed(repo):
    """两篇文档：一篇讲 MoE（跨文档 PPR 用），一篇是带 `set_db` 小节的命令手册
    （精确查找 seed 用）。两者都带 KG 节点与 evidence，保证初检索非空。"""
    notebook = repo.create_notebook(NotebookCreate(name="kb"))
    now = "2026-06-22T00:00:00"
    with repo._write() as db:
        for source_id, title in (
            ("src-a", "DeepSeek paper"),
            ("src-b", "GLM paper"),
            ("src-c", "Tool manual"),
        ):
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,source_type,status,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                (source_id, notebook.id, title, "md", "ready", now, now),
            )
        rows = (
            ("c-a", "src-a", "DeepSeek-V3 uses a Mixture-of-Experts (MoE) "
                             "architecture.", "Arch", "el-a"),
            ("c-b", "src-b", "GLM-4.5 is a Mixture-of-Experts (MoE) model.",
             "Arch", "el-b"),
            ("c-c", "src-c", "set_db is the command used for static timing "
                             "analysis setup.", "Commands > set_db", "el-c"),
        )
        for chunk_id, source_id, text, section, element_id in rows:
            db.execute(
                "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,"
                "element_ids,created_at) VALUES (?,?,?,?,?,?,?)",
                (chunk_id, notebook.id, source_id, text, section,
                 json.dumps([element_id]), now),
            )
            db.execute(
                "INSERT INTO source_elements (id,source_id,element_type,"
                "location_label,text,created_at) VALUES (?,?,?,?,?,?)",
                (element_id, source_id, "paragraph", section, text, now),
            )
            # `chunks_fts` 无触发器,写 chunk 的路径都要手工补一行(见
            # chunk_store.replace_source_chunks)。精确查找 seed 的探针只走
            # 这张表 —— 不补,它就永远返回 0 行,黄金件也就钉不住「精确查找排在
            # PPR 之后」那条红线真正要护的东西(两步的计数互相影响)。
            db.execute(
                "INSERT INTO chunks_fts(chunk_id,notebook_id,text) "
                "VALUES (?,?,?)",
                (chunk_id, notebook.id, text),
            )
        objects = (
            ("obj-a", "src-a", "el-a", "Mixture-of-Experts (MoE)"),
            ("obj-b", "src-b", "el-b", "Mixture-of-Experts (MoE)"),
            ("obj-c", "src-c", "el-c", "set_db"),
        )
        for object_id, source_id, element_id, name in objects:
            evidence = json.dumps([
                {"source_id": source_id, "source_title": "",
                 "element_id": element_id, "element_type": "paragraph",
                 "location_label": "p1", "quoted_span": name,
                 "confidence": 1.0}
            ])
            db.execute(
                "INSERT INTO knowledge_objects (id,notebook_id,object_type,"
                "status,owner,payload,evidence,source_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (object_id, notebook.id, "concept", "approved", "",
                 json.dumps({"name": name}), evidence, source_id, now, now),
            )
        for object_id in ("obj-a", "obj-b"):
            db.execute(
                "INSERT INTO concept_clusters (id,notebook_id,canonical_id,"
                "member_object_id,canonical_name,object_type,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (f"cl-{object_id}", notebook.id, "K-moe", object_id,
                 "Mixture-of-Experts (MoE)", "concept", now),
            )
    return notebook


def _observe(repo, notebook_id, *, question, intent_queries=None, effort=None,
              reflect_sequence=None, element_search_cap=None):
    """跑一次 run，返回可比对的 (trace, counts, sizes) 观测。

    `reflect_sequence`/`element_search_cap` 仅供 B8 quality P2-1 的两个新场景
    使用，省略时逐字复现之前的行为（`_StubLLM` 默认永远直接作答，settings
    与 `repo.settings` 是同一个对象）。`element_search_cap` 通过 `model_copy`
    构造一份**独立**的 settings 副本，绝不直接改写共享的 `repo.settings`——
    那会把这一处覆盖悄悄漏进同一个 `repo` 上跑的其它场景。
    """
    from app.core.ask_retrieval_policy import ask_retrieval_limits
    from app.services.reasoning_retrieval import ReasoningRetriever

    counts: collections.Counter = collections.Counter()
    bind_all_embedding_clients(repo, _CountingEmbedder(counts))
    bind_chat_client(
        repo, "reasoning_agent",
        _StubLLM(counts, reflect_sequence=reflect_sequence))

    settings = repo.settings
    if element_search_cap is not None:
        settings = settings.model_copy(
            update={"reasoning_max_element_searches": element_search_cap})

    retriever = ReasoningRetriever.from_repository(repo, settings)
    retriever.retrieval = _CountingRetrievalPort(retriever.retrieval, counts)
    for name in ("search", "ppr_retrieve", "exact_lookup", "search_elements",
                 "neighbors", "get", "follow_chain"):
        bound = getattr(retriever, name)

        def _make(call_name, target):
            def _wrapper(*args, **kwargs):
                counts[call_name] += 1
                return target(*args, **kwargs)
            return _wrapper

        setattr(retriever, name, _make(name, bound))

    result = retriever.run(
        notebook_id,
        question,
        intent_queries=list(intent_queries or []),
        limits=ask_retrieval_limits(effort) if effort else None,
    )
    trace = []
    for step in result.trace:
        detail = step.detail or {}
        entry = {
            "step_type": step.step_type,
            "summary": step.summary,
            "detail_keys": sorted(detail.keys()),
        }
        whitelist = _DETAIL_VALUE_WHITELIST.get(step.step_type)
        if whitelist:
            entry["detail_values"] = {
                key: detail[key] for key in whitelist if key in detail
            }
        trace.append(entry)
    sizes = {
        "top_hits": len(result.top_hits),
        "chunks": len(result.chunks),
        "elements": len(result.elements),
        "attempted": [
            {key: row[key] for key in sorted(row)} for row in result.attempted
        ],
    }
    return {"trace": trace, "counts": dict(sorted(counts.items())), "sizes": sizes}


# 四个代表性形态：普通问题 / 带引号短语 + 标识符 / 确认方向超首轮上限 /
# reflect 连续检索直到预算耗尽。
SCENARIOS = {
    "plain": {"question": "DeepSeek 与 GLM 的架构对比"},
    "quoted_identifier": {
        "question": '用 set_db 做 "static timing analysis" 时要注意什么',
    },
    "directions_over_first_round_cap": {
        "question": "综述这个库",
        # overview 档的首轮宽度是 2，6 个已确认方向 → 4 个进补种账目，
        # 而 overview 的 max_reasoning_steps=4 ⇒ 补种预算 4//2=2，
        # 于是 2 条补种执行、2 条落进终态未覆盖披露。
        "effort": "overview",
        "intent_queries": [
            "MoE 架构",
            "训练数据",
            "推理成本",
            "上下文长度",
            "对齐方法",
            "开源许可",
        ],
    },
    "budget_exhaustion": {
        # B8 quality P2-1：钉住 `_first_round_coverage_pass` → reflect 循环
        # 之间 `state.steps` 的交接。与上面同一份 6 方向 / overview 档：补种
        # 阶段先消耗 2 步（state.steps=2），reflect 循环拿到的预算只剩
        # max_steps(4) - 2 = 2 —— 桩连续两次 add_subquery（确定性、不要求
        # 命中新数据）正好把它花完，循环因步数耗尽而结束，不产生显式
        # "answer" 决定。若 `state.steps = steps` 那行赋值被删掉，reflect
        # 循环会误以为预算还剩 4，多跑 2 轮 add_subquery——轨迹步数与
        # sizes.attempted 都会变，从而变红。
        "question": "综述这个库",
        "effort": "overview",
        "intent_queries": [
            "MoE 架构",
            "训练数据",
            "推理成本",
            "上下文长度",
            "对齐方法",
            "开源许可",
        ],
        "reflect_sequence": [
            {"next_action": "add_subquery",
             "new_sub_query": {"query": "预算耗尽探针 1"}},
            {"next_action": "add_subquery",
             "new_sub_query": {"query": "预算耗尽探针 2"}},
        ],
    },
}


def _collect(repo):
    notebook = _seed(repo)
    observed = {
        name: _observe(repo, notebook.id, **kwargs)
        for name, kwargs in sorted(SCENARIOS.items())
    }
    # 空证据兜底是首轮的第六个阶段，只有在 KG/PPR/精确查找三条确定性通道都
    # 空手而归时才跑，所以它需要一本自己的空库 —— 上面那本一定有候选。
    empty = repo.create_notebook(NotebookCreate(name="empty"))
    observed["empty_evidence_fallback"] = _observe(
        repo, empty.id, question="这个库里有什么"
    )
    # B8 quality P2-1：钉住 `_first_round_empty_fallback` → reflect 循环之间
    # `state.elements_searches` 的交接。复用同一本空库：首轮的空证据兜底会先
    # 把 elements_searches 记成 1（本项特意把单来源检索上限降到 1，让 reflect
    # 的**第一次** search_elements 决定立即撞上限）——命中的正是它从兜底继承
    # 来的那个 1，不是循环自己新累积出来的。若 `state.elements_searches =
    # elements_searches` 那行赋值被删掉，交接会丢回默认值 0，第一次
    # search_elements 就不会撞上限（记成 "fallback" 步而不是 "skip" 步），
    # 从而变红。
    observed["element_search_cap_handoff"] = _observe(
        repo, empty.id, question="这个库里有什么",
        reflect_sequence=[
            {"next_action": "search_elements", "elements_query": "库内容"},
            {"next_action": "answer", "sufficient": True},
        ],
        element_search_cap=1,
    )
    return observed


def test_first_round_trace_and_request_counts_match_the_golden(repo):
    observed = _collect(repo)
    if os.environ.get("REGEN_FIRST_ROUND_GOLDEN") == "1":
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(
            json.dumps(observed, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        pytest.skip("golden regenerated")
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert set(observed) == set(golden)
    for name in sorted(golden):
        assert observed[name]["trace"] == golden[name]["trace"], name
        assert observed[name]["counts"] == golden[name]["counts"], name
        assert observed[name]["sizes"] == golden[name]["sizes"], name


def test_golden_is_not_vacuous():
    """黄金件必须真的覆盖首轮的每一类步，否则它证明不了任何东西。"""
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    kinds = {
        step["step_type"]
        for scenario in golden.values()
        for step in scenario["trace"]
    }
    # plan / 初检索 / PPR seed / 精确查找 seed / 空证据兜底 / 补种披露 / 合成候选
    assert {
        "plan", "retrieve", "ppr", "exact_lookup", "fallback", "skip", "answer",
    } <= kinds
    coverage = golden["directions_over_first_round_cap"]["trace"]
    assert any(
        step["step_type"] == "retrieve"
        and step["summary"].startswith("补充已确认方向")
        for step in coverage
    )
    assert any(
        step["step_type"] == "skip" and "未能执行" in step["summary"]
        for step in coverage
    )
    assert any(
        step["step_type"] == "exact_lookup"
        for step in golden["quoted_identifier"]["trace"]
    )
