"""NotebookSummary.ask_available —— 该库能否在任一可用问答模式下产出有据回答。

前端据此禁用"空库"的问答输入框(codex PR#334 评审:判定所需的隐藏 knowhow chunk、
confirmed memory、base+overlay 配置前端都看不到,故由后端权威计算)。这里钉住每条
证据线索各自都能让 ask_available 为真,尤其 P1-1:无可见来源、零 knowledge_objects
但有可检索 chunk 的 knowhow 表 —— 它可对话,绝不能被判为不可用。
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository

NOW = "2026-07-20T00:00:00"


@pytest.fixture
def repo(tmp_path, monkeypatch) -> SQLiteRepository:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'ask_available.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings(_env_file=None))


def _add_source(db, notebook_id, source_id, source_type):
    db.execute(
        "INSERT INTO sources "
        "(id,notebook_id,title,source_type,status,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (source_id, notebook_id, source_id, source_type, "ready", NOW, NOW),
    )


def _add_chunk(db, notebook_id, source_id, chunk_id):
    db.execute(
        "INSERT INTO chunks (id,notebook_id,source_id,text,created_at) "
        "VALUES (?,?,?,?,?)",
        (chunk_id, notebook_id, source_id, "some retrievable text", NOW),
    )


def _add_kg_object(db, notebook_id, object_id, status="approved"):
    db.execute(
        "INSERT INTO knowledge_objects "
        "(id,notebook_id,object_type,status,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (object_id, notebook_id, "concept", status, NOW, NOW),
    )


def _add_memory(db, notebook_id, user_id, memory_id, status):
    db.execute(
        "INSERT INTO memory_items "
        "(id,notebook_id,created_by,origin,status,title,content_md,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (memory_id, notebook_id, user_id, "external_agent", status,
         "t", "c", NOW, NOW),
    )


def test_empty_notebook_is_not_ask_available(repo):
    """报告的 bug 本体:全空的新库 —— 无来源/无 chunk/无 KG/无参考库/无 memory —— 该禁。"""
    nb = repo.create_notebook(NotebookCreate(name="empty"))
    assert repo.get_notebook(nb.id).ask_available is False


def test_visible_source_with_chunk_is_ask_available(repo):
    nb = repo.create_notebook(NotebookCreate(name="doc"))
    with repo._write() as db:
        _add_source(db, nb.id, "s-doc", "document")
        _add_chunk(db, nb.id, "s-doc", "c-doc")
    assert repo.get_notebook(nb.id).ask_available is True


def test_knowhow_only_chunks_are_ask_available(repo):
    """P1-1 反向护栏:无锚点列的 knowhow 表产出可检索 chunk 但零 knowledge_objects,
    且其源是 source_type='knowhow' 的隐藏合成源(被 visible_source_count 排除)。
    visible_sources=0 且 kg_ready=False,但 chunk 模式能答 —— 必须 ask_available=True。"""
    nb = repo.create_notebook(NotebookCreate(name="knowhow-only"))
    with repo._write() as db:
        _add_source(db, nb.id, "s-knowhow", "knowhow")
        _add_chunk(db, nb.id, "s-knowhow", "c-knowhow")
    summary = repo.get_notebook(nb.id)
    assert summary.counts["sources"] == 0   # 可见来源确实为 0
    assert summary.kg_ready is False        # 确实无 knowledge_objects
    assert summary.ask_available is True     # 但仍可对话


def test_kg_ready_notebook_is_ask_available(repo):
    nb = repo.create_notebook(NotebookCreate(name="kg"))
    with repo._write() as db:
        _add_kg_object(db, nb.id, "ko-1")
    summary = repo.get_notebook(nb.id)
    assert summary.kg_ready is True
    assert summary.ask_available is True


def test_deprecated_only_kg_is_not_ask_available(repo):
    """P2-1 反向护栏:本库只有 deprecated 的 knowledge_objects(无 chunk/参考库/memory)。
    检索排除 deprecated,故没有可用证据——kg_ready 为真(既有全应用口径含 deprecated)但
    ask_available 必须为假,不能只因"建过图"就放行。"""
    nb = repo.create_notebook(NotebookCreate(name="deprecated-kg"))
    with repo._write() as db:
        _add_kg_object(db, nb.id, "ko-dep", status="deprecated")
    summary = repo.get_notebook(nb.id)
    assert summary.kg_ready is True          # 既有口径:建过图
    assert summary.ask_available is False     # 但无可用证据,禁止对话


def test_deprecated_only_base_kg_is_not_ask_available(repo):
    """P2-1 反向护栏(参考库侧):挂载的参考库只有 deprecated KG —— base_kg_available
    为真(含 deprecated)但没有可用证据,ask_available 必须为假。"""
    base = repo.create_notebook(NotebookCreate(name="dep-ref"))
    with repo._write() as db:
        _add_kg_object(db, base.id, "ko-base-dep", status="deprecated")
    repo.mark_notebook_base(base.id)
    nb = repo.create_notebook(NotebookCreate(name="mounts-dep-base"))
    repo.replace_notebook_bases(nb.id, [base.id], "user-local")
    summary = repo.get_notebook(nb.id)
    assert summary.base_kg_available is True   # 既有口径:挂的参考库建过图
    assert summary.ask_available is False       # 但只有 deprecated,无可用证据


def test_mounted_base_with_kg_is_ask_available(repo):
    """本库无自有内容,但挂载了一个有 KG 的参考库 —— 严格模式可借用,故可对话。"""
    base = repo.create_notebook(NotebookCreate(name="ref"))
    with repo._write() as db:
        _add_kg_object(db, base.id, "ko-base")
    repo.mark_notebook_base(base.id)
    nb = repo.create_notebook(NotebookCreate(name="mounts-base"))
    repo.replace_notebook_bases(nb.id, [base.id], "user-local")
    summary = repo.get_notebook(nb.id)
    assert summary.base_kg_available is True
    assert summary.ask_available is True


def test_confirmed_memory_is_ask_available(repo):
    nb = repo.create_notebook(NotebookCreate(name="mem"))
    with repo._write() as db:
        _add_memory(db, nb.id, repo.current_user().id, "m-1", "confirmed")
    assert repo.get_notebook(nb.id).ask_available is True


def test_candidate_only_memory_is_not_ask_available(repo):
    """只有 candidate memory(无来源/KG/参考库)—— candidate 不作证据,该禁。
    钉住 ask_available 用的是 confirmed-only 判定,而非 counts["memories"](含候选)。"""
    nb = repo.create_notebook(NotebookCreate(name="candidate-mem"))
    with repo._write() as db:
        _add_memory(db, nb.id, repo.current_user().id, "m-cand", "candidate")
    summary = repo.get_notebook(nb.id)
    assert summary.counts["memories"] == 1   # 计数看得到候选
    assert summary.ask_available is False     # 但候选不让对话可用


# ---------------------------------------------------------------------------
# local_evidence_available —— ask_available 的**本地那一半**(codex #431 R7 P1)
#
# 合并后的单个布尔分不出「有得可搜」是本地撑起来的还是参考库撑起来的,于是「把参考库
# 全部取消勾选后本地还剩不剩东西」只能退化成拿**可见来源数**回答 —— 而 Knowhow 表、
# 已确认 Memory、本地图谱都不计入那个数。下面逐条钉住这个信号的边界。
# ---------------------------------------------------------------------------


def test_empty_notebook_has_no_local_evidence(repo):
    nb = repo.create_notebook(NotebookCreate(name="empty-local"))
    assert repo.get_notebook(nb.id).local_evidence_available is False


def test_knowhow_only_chunks_are_local_evidence(repo):
    """本条就是被误拒的那个形态:零可见来源,但 knowhow 格子照常可搜。
    可见来源数为 0 → 旧判据说「本地为空」;local_evidence_available 必须说不是。"""
    nb = repo.create_notebook(NotebookCreate(name="knowhow-local"))
    with repo._write() as db:
        _add_source(db, nb.id, "s-knowhow", "knowhow")
        _add_chunk(db, nb.id, "s-knowhow", "c-knowhow")
    summary = repo.get_notebook(nb.id)
    assert summary.counts["sources"] == 0            # 可见来源确实为 0
    assert summary.local_evidence_available is True  # 但本地确实有得可搜


def test_confirmed_memory_is_local_evidence(repo):
    """已确认 Memory 同样没有可见来源 —— 它也在本地那一半里。"""
    nb = repo.create_notebook(NotebookCreate(name="mem-local"))
    with repo._write() as db:
        _add_memory(db, nb.id, repo.current_user().id, "m-1", "confirmed")
    summary = repo.get_notebook(nb.id)
    assert summary.counts["sources"] == 0
    assert summary.local_evidence_available is True


def test_candidate_only_memory_is_not_local_evidence(repo):
    """candidate 不作证据 —— 与 ask_available 同一口径。顺带钉住:
    counts["memories"] 只是**便宜预过滤**(全状态计数),不是判据本身。"""
    nb = repo.create_notebook(NotebookCreate(name="candidate-local"))
    with repo._write() as db:
        _add_memory(db, nb.id, repo.current_user().id, "m-cand", "candidate")
    summary = repo.get_notebook(nb.id)
    assert summary.counts["memories"] == 1
    assert summary.local_evidence_available is False


def test_local_usable_kg_is_local_evidence(repo):
    nb = repo.create_notebook(NotebookCreate(name="kg-local"))
    with repo._write() as db:
        _add_kg_object(db, nb.id, "ko-1")
    assert repo.get_notebook(nb.id).local_evidence_available is True


def test_deprecated_only_local_kg_is_not_local_evidence(repo):
    """本地那一半沿用同一条 USABLE_STATUSES 口径,不因「建过图」就算有证据。"""
    nb = repo.create_notebook(NotebookCreate(name="dep-kg-local"))
    with repo._write() as db:
        _add_kg_object(db, nb.id, "ko-dep", status="deprecated")
    summary = repo.get_notebook(nb.id)
    assert summary.kg_ready is True
    assert summary.local_evidence_available is False


def test_mounted_base_kg_is_not_local_evidence(repo):
    """**反向护栏**:挂载参考库的 KG 让 ask_available 为真,但它是**参考库**证据,
    绝不能算进本地那一半 —— 算进去的话,一个零本地证据的库把参考库全部取消勾选后
    仍会被放行,Ask 白跑一轮零证据、报告还要落一行 + 调一次意图模型。"""
    base = repo.create_notebook(NotebookCreate(name="ref-only"))
    with repo._write() as db:
        _add_kg_object(db, base.id, "ko-base")
    repo.mark_notebook_base(base.id)
    nb = repo.create_notebook(NotebookCreate(name="mounts-ref-only"))
    repo.replace_notebook_bases(nb.id, [base.id], "user-local")
    summary = repo.get_notebook(nb.id)
    assert summary.ask_available is True              # 借参考库可对话
    assert summary.local_evidence_available is False  # 但本地一无所有


# ---------------------------------------------------------------------------
# 效率回归:新信号不得新增数据库往返
#
# 这条路径是「打开笔记本卡 5-6 秒」事故的现场,效率是本仓库的一等约束。四个可用性
# 探针短路求值,顺序有成本理由;拆出本地那一半只允许**复用**它们的结果。
# ---------------------------------------------------------------------------

AVAILABILITY_PROBES = (
    "notebook_has_chunk",
    "notebook_has_usable_kg",
    "notebook_has_usable_base_kg",
    "notebook_has_confirmed_memory",
)


class _CountingQueries:
    """透明代理:记录每次方法调用名后原样委托。"""

    def __init__(self, inner):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "calls", [])

    def __getattr__(self, name):
        attribute = getattr(self._inner, name)
        if not callable(attribute):
            return attribute

        def recorded(*args, **kwargs):
            self.calls.append(name)
            return attribute(*args, **kwargs)

        return recorded

    def probes(self) -> list[str]:
        return [name for name in self.calls if name in AVAILABILITY_PROBES]


def _probe_calls(repo, notebook_id) -> tuple[list[str], list[str]]:
    summaries = repo._runtime.notebook_summaries
    original = summaries.queries
    spy = _CountingQueries(original)
    summaries.queries = spy
    try:
        repo.get_notebook(notebook_id)
    finally:
        summaries.queries = original
    return spy.probes(), spy.calls


def test_chunked_notebook_still_costs_one_availability_probe(repo):
    """绝大多数库(有 chunk)恒 1 次探针 —— 与拆分前逐字一致。"""
    nb = repo.create_notebook(NotebookCreate(name="one-probe"))
    with repo._write() as db:
        _add_source(db, nb.id, "s-doc", "document")
        _add_chunk(db, nb.id, "s-doc", "c-doc")
    probes, _calls = _probe_calls(repo, nb.id)
    assert probes == ["notebook_has_chunk"]


def test_empty_notebook_now_costs_one_fewer_probe(repo):
    """空库:kg_ready/base_kg_available 都为假早已免掉 B/C,而新加的
    counts["memories"] 预过滤(它是本函数**本来就查**的 memory_counts_by_owner_notebook
    的结果,不是新查询)连 D 也免了 —— 从拆分前的 2 次降到 1 次。"""
    nb = repo.create_notebook(NotebookCreate(name="empty-probe"))
    probes, calls = _probe_calls(repo, nb.id)
    assert probes == ["notebook_has_chunk"]
    # 预过滤只复用已在手的那一次分组计数,没有新增第二次 memory 查询。
    assert calls.count("memory_counts_by_owner_notebook") == 1


def test_base_only_notebook_probe_count_is_unchanged(repo):
    """本条是「C 与 D 换序是否要多付一次查询」的对账点。
    换序本身会让 base-only 库多付一次 confirmed-memory 探针(local 的取值由 D 决定,
    c 再真也代替不了);memories 预过滤把它挡掉,于是这个形态仍是 A + C = 2 次,
    与拆分前逐字一致。"""
    base = repo.create_notebook(NotebookCreate(name="ref-probe"))
    with repo._write() as db:
        _add_kg_object(db, base.id, "ko-base")
    repo.mark_notebook_base(base.id)
    nb = repo.create_notebook(NotebookCreate(name="mounts-ref-probe"))
    repo.replace_notebook_bases(nb.id, [base.id], "user-local")
    probes, _calls = _probe_calls(repo, nb.id)
    assert probes == ["notebook_has_chunk", "notebook_has_usable_base_kg"]
