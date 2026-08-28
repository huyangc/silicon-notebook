"""Store-level coverage for ``AgentObservationStorePort`` (Agentic Memory P3,
T2).

Deliberately built directly on
``app.repositories.sqlite.agent_observation_store.AgentObservationStore``
plus a bare migrated ``SqliteDatabase`` — not through the full
``SQLiteRepository``/``app.services.repository_runtime`` composition,
mirroring ``test_agent_profile_store.py``'s own rationale: this file proves
the STORE primitive in isolation. T2 wires no consumer to it yet (no MCP
tool, no consolidation job, no route), so there is nothing else to exercise
it through.

``owner_id``/``agent_profile_id`` carry no foreign key (see
``_migration_55``'s docstring), so these tests use bare strings like
``"user-a"``/``"agent-1"`` without seeding real ``users``/``agent_profiles``
rows for them — only ``notebooks.created_by`` needs a real user to satisfy
that table's own FK.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.core.config import Settings
from app.repositories.ports import AGENT_CALL_RING_MAX, AGENT_OBSERVATION_RING_MAX
from app.repositories.sqlite.agent_observation_store import AgentObservationStore
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.migrations import SqliteMigrator
from tests.agent_observation_parity_cases import (
    AGENT_OBSERVATION_TIE_BREAK_IDS,
    AGENT_OBSERVATION_TIE_BREAK_SURVIVORS,
)

NOW = "2026-08-20T00:00:00+00:00"
NOTEBOOK_ID = "nb-1"
OTHER_NOTEBOOK_ID = "nb-2"


class _Clock:
    """A monotonically advancing ISO clock — one call, one second later.

    Used instead of a fixed timestamp wherever a test cares about eviction
    ORDER, so the ordering assertion exercises ``created_at`` rather than
    coincidentally passing off the ``id`` tie-break alone.
    """

    def __init__(self, start: str = NOW) -> None:
        self._base = datetime.fromisoformat(start)
        self._n = 0

    def __call__(self) -> str:
        value = (self._base + timedelta(seconds=self._n)).isoformat()
        self._n += 1
        return value


@dataclass
class Harness:
    database: SqliteDatabase
    store: AgentObservationStore
    clock: _Clock


def _seed_notebooks(database: SqliteDatabase) -> None:
    with database.write() as db:
        db.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,"
            "updated_at) VALUES (?,?,?,?,?,?,?)",
            ("user-owner", "owner@example.test", "Owner", "admin", "active", NOW, NOW),
        )
        for notebook_id in (NOTEBOOK_ID, OTHER_NOTEBOOK_ID):
            db.execute(
                "INSERT INTO notebooks(id,name,purpose,primary_domain,status,"
                "created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (notebook_id, "NB", "", "engineering", "ready", "user-owner", NOW, NOW),
            )


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'test.db'}")
    database = SqliteDatabase(settings, tmp_path)
    migrated = SqliteMigrator(database, settings).migrate()
    assert migrated, "fresh database must actually run the migration ladder"
    _seed_notebooks(database)

    counter = {"n": 0}

    def new_id(prefix: str) -> str:
        counter["n"] += 1
        return f"{prefix}-{counter['n']:06d}"

    clock = _Clock()
    return Harness(
        database=database,
        store=AgentObservationStore(database, new_id=new_id, now=clock),
        clock=clock,
    )


# -------------------------------------------------------------- idempotency
def test_append_observation_same_key_returns_the_same_row_and_does_not_duplicate(
    harness: Harness,
):
    first_id, first_dup = harness.store.append_observation(
        NOTEBOOK_ID, "user-a", "agent-1",
        text="noticed X while working here", client_request_id="req-1",
    )
    assert first_dup is False

    second_id, second_dup = harness.store.append_observation(
        NOTEBOOK_ID, "user-a", "agent-1",
        text="a different text — must be ignored", client_request_id="req-1",
    )
    assert second_id == first_id
    assert second_dup is True

    rows = harness.store.list_observations(NOTEBOOK_ID, "user-a", limit=10)
    assert len(rows) == 1
    assert rows[0]["text"] == "noticed X while working here"


def test_append_observation_idempotency_key_includes_agent_profile_id(
    harness: Harness,
):
    """⚠ Mutation-tested contract point: two DIFFERENT Agents that happen to
    mint the same client-side ``client_request_id`` must each get their own
    row. If the idempotency key ever drops ``agent_profile_id`` (from either
    the pre-insert SELECT or the ``IntegrityError`` re-read), this collapses
    to one row and this test goes red — see the T2 report for the manual
    mutation run that confirmed it."""
    id_a, dup_a = harness.store.append_observation(
        NOTEBOOK_ID, "user-a", "agent-1",
        text="from agent 1", client_request_id="shared-req",
    )
    id_b, dup_b = harness.store.append_observation(
        NOTEBOOK_ID, "user-a", "agent-2",
        text="from agent 2", client_request_id="shared-req",
    )
    assert dup_a is False
    assert dup_b is False
    assert id_a != id_b

    rows = harness.store.list_observations(NOTEBOOK_ID, "user-a", limit=10)
    assert {row["agent_profile_id"] for row in rows} == {"agent-1", "agent-2"}
    assert len(rows) == 2


# ------------------------------------------------------------------ eviction
def test_append_observation_evicts_down_to_the_ring_max_keeping_the_newest(
    harness: Harness,
):
    total = AGENT_OBSERVATION_RING_MAX + 5
    inserted_ids: list[str] = []
    for i in range(total):
        observation_id, deduplicated = harness.store.append_observation(
            NOTEBOOK_ID, "user-a", "agent-1",
            text=f"note {i}", client_request_id=f"req-{i}",
        )
        assert deduplicated is False
        inserted_ids.append(observation_id)

    rows = harness.store.list_observations(NOTEBOOK_ID, "user-a", limit=total)
    assert len(rows) == AGENT_OBSERVATION_RING_MAX

    kept_ids = {row["id"] for row in rows}
    assert kept_ids == set(inserted_ids[-AGENT_OBSERVATION_RING_MAX:])
    # The ring keeps the group at exactly the bound at the database level too
    # — not just what a bounded read happens to return.
    with harness.database.connect() as db:
        total_rows = db.execute(
            "SELECT COUNT(*) AS n FROM agent_observations "
            "WHERE notebook_id=? AND owner_id=?",
            (NOTEBOOK_ID, "user-a"),
        ).fetchone()["n"]
    assert total_rows == AGENT_OBSERVATION_RING_MAX


def test_ring_eviction_is_per_owner_scope(harness: Harness):
    """A's ring filling up must never evict B's rows in the same notebook."""
    for i in range(AGENT_OBSERVATION_RING_MAX + 5):
        harness.store.append_observation(
            NOTEBOOK_ID, "user-a", "agent-1",
            text=f"a-note {i}", client_request_id=f"a-req-{i}",
        )
    harness.store.append_observation(
        NOTEBOOK_ID, "user-b", "agent-1",
        text="b-note", client_request_id="b-req-1",
    )
    b_rows = harness.store.list_observations(NOTEBOOK_ID, "user-b", limit=10)
    assert len(b_rows) == 1
    assert b_rows[0]["text"] == "b-note"


# ------------------------------------------------------------------ isolation
def test_recent_observations_is_scoped_by_owner_id(harness: Harness):
    harness.store.append_observation(
        NOTEBOOK_ID, "user-a", "agent-1", text="a-only", client_request_id="r1",
    )
    harness.store.append_observation(
        NOTEBOOK_ID, "user-b", "agent-1", text="b-only", client_request_id="r1",
    )
    a_rows = harness.store.recent_observations(NOTEBOOK_ID, "user-a", limit=10)
    b_rows = harness.store.recent_observations(NOTEBOOK_ID, "user-b", limit=10)
    assert [row["text"] for row in a_rows] == ["a-only"]
    assert [row["text"] for row in b_rows] == ["b-only"]
    # And the four-field projection never carries owner_id at all.
    assert set(a_rows[0]) == {"id", "agent_profile_id", "text", "created_at"}


def test_list_observations_matches_recent_observations(harness: Harness):
    for i in range(3):
        harness.store.append_observation(
            NOTEBOOK_ID, "user-a", "agent-1",
            text=f"note {i}", client_request_id=f"req-{i}",
        )
    assert harness.store.list_observations(
        NOTEBOOK_ID, "user-a", limit=10
    ) == harness.store.recent_observations(NOTEBOOK_ID, "user-a", limit=10)


# ---------------------------------------------------------------------- clear
def test_clear_observations_by_agent_profile_only_removes_that_agent(
    harness: Harness,
):
    harness.store.append_observation(
        NOTEBOOK_ID, "user-a", "agent-1", text="keep-target", client_request_id="r1",
    )
    harness.store.append_observation(
        NOTEBOOK_ID, "user-a", "agent-2", text="other-agent", client_request_id="r1",
    )
    removed = harness.store.clear_observations(
        NOTEBOOK_ID, "user-a", agent_profile_id="agent-1"
    )
    assert removed == 1
    remaining = harness.store.recent_observations(NOTEBOOK_ID, "user-a", limit=10)
    assert [row["agent_profile_id"] for row in remaining] == ["agent-2"]


def test_clear_observations_without_agent_profile_removes_everything_in_scope(
    harness: Harness,
):
    harness.store.append_observation(
        NOTEBOOK_ID, "user-a", "agent-1", text="a", client_request_id="r1",
    )
    harness.store.append_observation(
        NOTEBOOK_ID, "user-a", "agent-2", text="b", client_request_id="r2",
    )
    harness.store.append_observation(
        NOTEBOOK_ID, "user-b", "agent-1", text="other-owner", client_request_id="r1",
    )
    removed = harness.store.clear_observations(NOTEBOOK_ID, "user-a")
    assert removed == 2
    assert harness.store.recent_observations(NOTEBOOK_ID, "user-a", limit=10) == []
    # user-b's scope is untouched.
    assert len(harness.store.recent_observations(NOTEBOOK_ID, "user-b", limit=10)) == 1


# -------------------------------------------------------------------- cascade
def test_notebook_delete_cascades_observation_rows(harness: Harness):
    harness.store.append_observation(
        NOTEBOOK_ID, "user-a", "agent-1", text="a", client_request_id="r1",
    )
    harness.store.append_observation(
        OTHER_NOTEBOOK_ID, "user-a", "agent-1", text="b", client_request_id="r1",
    )
    with harness.database.write() as db:
        db.execute("DELETE FROM notebooks WHERE id=?", (NOTEBOOK_ID,))
    with harness.database.connect() as db:
        remaining = db.execute(
            "SELECT notebook_id FROM agent_observations"
        ).fetchall()
    assert [row["notebook_id"] for row in remaining] == [OTHER_NOTEBOOK_ID]


# ------------------------------------------------------------------ ordering
def test_eviction_tie_breaks_on_id_when_created_at_is_identical(harness: Harness):
    """T2 修复轮回归——真正的 tie-break 测试。EVERY row shares the exact
    same ``created_at`` (a fixed clock, not ``_Clock``'s one-second-per-call
    advance), which isolates the ``id DESC`` comparison from ``created_at``
    entirely: the existing ``test_append_observation_evicts_down_to_the_
    ring_max_keeping_the_newest`` above never actually exercises the id
    tie-break, because its ``_Clock`` gives every row a unique timestamp.

    The id list (``tests.agent_observation_parity_cases``) is shuffled
    relative to insertion order specifically so "keep the last N inserted"
    and "keep the top N ids by descending sort" produce DIFFERENT surviving
    sets — see that module's docstring.
    """
    id_iter = iter(AGENT_OBSERVATION_TIE_BREAK_IDS)

    def fixed_new_id(prefix: str) -> str:
        return next(id_iter)

    store = AgentObservationStore(harness.database, new_id=fixed_new_id, now=lambda: NOW)
    for i, expected_id in enumerate(AGENT_OBSERVATION_TIE_BREAK_IDS):
        observation_id, deduplicated = store.append_observation(
            NOTEBOOK_ID, "user-tie", "agent-1",
            text=f"note {i}", client_request_id=f"req-{i}",
        )
        assert observation_id == expected_id
        assert deduplicated is False

    rows = store.list_observations(
        NOTEBOOK_ID, "user-tie", limit=len(AGENT_OBSERVATION_TIE_BREAK_IDS)
    )
    assert {row["id"] for row in rows} == AGENT_OBSERVATION_TIE_BREAK_SURVIVORS


def test_recent_observations_orders_by_absolute_instant_not_text(harness: Harness):
    """T2 修复轮回归——两行 ``created_at`` 的 TEXT 序与它们代表的绝对时刻
    相反(不同 UTC offset),镜像 ``conversations``'
    ``CONVERSATION_ANSWERS_ORDER_DESC`` 先例的同一条教训: a naive
    ``ORDER BY created_at DESC`` text comparison ranks the row whose text
    happens to sort higher first, even when it is chronologically OLDER.

    OLD: ``"2026-08-20T10:00:00+00:00"`` — absolute UTC 10:00.
    NEW: ``"2026-08-20T09:00:00-02:00"`` — absolute UTC 11:00 (genuinely
    LATER than OLD), but its text is LEXICOGRAPHICALLY SMALLER than OLD's
    (``"09"`` < ``"10"`` at the first differing character) — text-only
    ordering would rank OLD as "most recent"; the fix must not.
    """
    ids = iter(["obs-old", "obs-new"])

    def fixed_new_id(prefix: str) -> str:
        return next(ids)

    timestamps = iter(
        [
            "2026-08-20T10:00:00+00:00",  # OLD, absolute 10:00
            "2026-08-20T09:00:00-02:00",  # NEW, absolute 11:00
        ]
    )

    def fixed_clock() -> str:
        return next(timestamps)

    store = AgentObservationStore(harness.database, new_id=fixed_new_id, now=fixed_clock)
    old_id, _ = store.append_observation(
        NOTEBOOK_ID, "user-offset", "agent-1", text="old", client_request_id="r-old",
    )
    new_id, _ = store.append_observation(
        NOTEBOOK_ID, "user-offset", "agent-1", text="new", client_request_id="r-new",
    )
    assert (old_id, new_id) == ("obs-old", "obs-new")

    rows = store.list_observations(NOTEBOOK_ID, "user-offset", limit=10)
    assert [row["id"] for row in rows] == ["obs-new", "obs-old"]


def test_ring_eviction_is_per_notebook_scope(harness: Harness):
    """B 的规格②(cross-notebook isolation)——同一个 OWNER 跨两本笔记本时,
    一本笔记本灌满环形绝不能连带淘汰另一本笔记本的行:淘汰 SQL 同时按
    ``notebook_id`` 与 ``owner_id`` 两列限定作用域,不是只按 owner。"""
    other_id, other_dup = harness.store.append_observation(
        OTHER_NOTEBOOK_ID, "user-a", "agent-1",
        text="other-notebook-note", client_request_id="other-req",
    )
    assert other_dup is False

    for i in range(AGENT_OBSERVATION_RING_MAX + 5):
        harness.store.append_observation(
            NOTEBOOK_ID, "user-a", "agent-1",
            text=f"note {i}", client_request_id=f"req-{i}",
        )

    other_rows = harness.store.recent_observations(OTHER_NOTEBOOK_ID, "user-a", limit=10)
    assert [row["id"] for row in other_rows] == [other_id]


# --------------------------------------------------------------- transaction
def test_append_observation_opens_exactly_one_write_transaction(
    harness: Harness, monkeypatch: pytest.MonkeyPatch,
):
    """接缝断言——一次 ``append_observation`` 必须恰好开 1 个
    ``SqliteDatabase.write()`` 事务(挡「淘汰挪到写事务之外」这类移动变异,
    该变异会打破幂等读、INSERT 与淘汰 DELETE 同原子提交的契约,却不会让
    任何既有断言变红,因为它们只看最终数据库状态)。"""
    calls = {"n": 0}
    original_write = harness.database.write

    import contextlib

    @contextlib.contextmanager
    def counting_write(*args, **kwargs):
        calls["n"] += 1
        with original_write(*args, **kwargs) as conn:
            yield conn

    monkeypatch.setattr(harness.database, "write", counting_write)

    harness.store.append_observation(
        NOTEBOOK_ID, "user-a", "agent-1", text="x", client_request_id="req-tx",
    )
    assert calls["n"] == 1


# ---------------------------------------------------------------- validation
def test_append_observation_rejects_empty_client_request_id(harness: Harness):
    """空/缺失 client_request_id 绝不能到达 store:它是唯一会让同一个
    (notebook_id, owner_id, agent_profile_id) 元组下所有「无 id」写入折叠进
    同一个部分唯一索引槽位的值(索引谓词是 ``client_request_id IS NOT
    NULL``,空字符串不是 NULL,照样参与比较并冲突)。调用方(T3 的
    add_observation MCP 工具)必须在到达这里之前就拒收,这里是响亮的兜底。"""
    with pytest.raises(ValueError):
        harness.store.append_observation(
            NOTEBOOK_ID, "user-a", "agent-1", text="x", client_request_id="",
        )


def test_idempotency_window_is_bounded_by_ring_retention(harness):
    """codex #535 R4 P2(登记为有界合同,非缺陷):幂等只在行仍被环形保留时
    成立——RING_MAX 条更新的观察把某 request_id 的行淘汰后,重试同一 id 会
    写出一条**新**行(deduplicated=False)。tool 描述与 docs 同句登记;要
    永久幂等就得再开一张 key 表,对以秒计的重试合同不值一次迁移。"""
    store = harness.store
    first_id, first_dup = store.append_observation(
        "nb-1", "user-a", "agent-1", text="old", client_request_id="req-old",
    )
    assert first_dup is False
    from app.repositories.ports import AGENT_CALL_RING_MAX, AGENT_OBSERVATION_RING_MAX

    for index in range(AGENT_OBSERVATION_RING_MAX):
        store.append_observation(
            "nb-1", "user-a", "agent-1",
            text=f"newer {index}", client_request_id=f"req-newer-{index}",
        )
    retry_id, retry_dup = store.append_observation(
        "nb-1", "user-a", "agent-1", text="old again", client_request_id="req-old",
    )
    assert retry_dup is False
    assert retry_id != first_id


def test_member_removal_clears_that_members_observations(harness):
    """codex #535 R6 P2:成员移出走覆盖层同一条空白起点契约——remove_member
    同批清空该成员本库观察行,别人的行与别库的行不动。"""
    from app.services.notebook_sharing import NotebookSharingService

    store = harness.store
    store.append_observation("nb-1", "user-a", "agent-1",
                             text="mine", client_request_id="m-1")
    store.append_observation("nb-1", "user-b", "agent-1",
                             text="theirs", client_request_id="t-1")
    store.append_observation("nb-2", "user-a", "agent-1",
                             text="other nb", client_request_id="o-1")

    class _SharingStore:
        def remove_member(self, notebook_id, user_id):
            return None

    service = NotebookSharingService.__new__(NotebookSharingService)
    service._store = _SharingStore()
    service._profiles = None
    service._observations = store
    service.remove_member("nb-1", "user-a")

    assert store.list_observations("nb-1", "user-a", limit=10) == []
    assert len(store.list_observations("nb-1", "user-b", limit=10)) == 1
    assert len(store.list_observations("nb-2", "user-a", limit=10)) == 1


def test_same_instant_different_offset_spellings_tie_break_on_id(harness):
    """codex #535 R7 P2:同一绝对时刻的 +02:00/+01:00 拼写在 julianday 上
    并列——存活行必须由 id 决定(与 PG 的 timestamptz+id 一致),不得按文本
    拼写。"""
    # 同一绝对时刻的两种拼写——用脚本时钟直造,绕开单调 _Clock
    spellings = iter(["2026-08-20T12:00:00+02:00", "2026-08-20T11:00:00+01:00"])
    counter = {"n": 0}

    def tie_new_id(prefix: str) -> str:
        counter["n"] += 1
        return f"{prefix}-tie-{counter['n']:06d}"

    store = AgentObservationStore(
        harness.database, new_id=tie_new_id, now=lambda: next(spellings)
    )
    store.append_observation("nb-1", "user-a", "agent-1",
                             text="plus2", client_request_id="tie-a")
    store.append_observation("nb-1", "user-a", "agent-1",
                             text="plus1", client_request_id="tie-b")
    rows = store.recent_observations("nb-1", "user-a", limit=10)
    ids = [row["id"] for row in rows]
    assert ids == sorted(ids, reverse=True), (
        "并列时刻必须按 id 降序,不得按 created_at 文本拼写"
    )


# ------------------------------------------------ 调用记账(kind='call')
#
# 这一组钉的全部是「两种行互不侵占」——加 kind 列的**唯一**理由。每一条都对应
# 一个具体的、加列之前不存在的失败形态,不是形状复述。


def test_append_call_writes_a_call_row_readable_only_through_list_calls(harness):
    harness.store.append_call(NOTEBOOK_ID, "user-a", "agent-1", capability="ask:execute")

    calls = harness.store.list_calls(NOTEBOOK_ID, "user-a", limit=10)
    assert [row["capability"] for row in calls] == ["ask:execute"]
    # 调用行**绝不**以 `text` 的名义示人:那个键在这张表里意味着「Agent 写下的
    # 话」,把一个能力档名字塞进去就是让渲染方把系统记的账当成 Agent 说的话。
    assert "text" not in calls[0]
    assert set(calls[0]) == {"id", "agent_profile_id", "capability", "created_at"}

    # 两条读路径都看不见它。前者是巡固喂给模型的那一条——这才是重点。
    assert harness.store.recent_observations(NOTEBOOK_ID, "user-a", limit=10) == []
    assert harness.store.list_observations(NOTEBOOK_ID, "user-a", limit=10) == []


def test_call_rows_never_reach_the_consolidation_read(harness):
    """``recent_observations`` 是巡固的取样读,调用记账绝不能出现在里面——
    否则系统自己记的账会以「外部 Agent 写下的话」的身份进模型 prompt。"""
    harness.store.append_observation(
        NOTEBOOK_ID, "user-a", "agent-1",
        text="真的是 Agent 写下的", client_request_id="req-1",
    )
    for _ in range(5):
        harness.store.append_call(
            NOTEBOOK_ID, "user-a", "agent-1", capability="knowledge:read"
        )

    rows = harness.store.recent_observations(NOTEBOOK_ID, "user-a", limit=50)
    assert [row["text"] for row in rows] == ["真的是 Agent 写下的"]


def test_call_flood_never_evicts_written_notes(harness):
    """环形淘汰按 kind 分组的**因**:调用记账每次工具调用写一行,共用一个环
    时,一次密集检索就能把用户攒了很久的短句全部挤掉。"""
    harness.store.append_observation(
        NOTEBOOK_ID, "user-a", "agent-1",
        text="攒了很久的一句", client_request_id="req-keep",
    )
    for index in range(AGENT_OBSERVATION_RING_MAX + 50):
        harness.store.append_call(
            NOTEBOOK_ID, "user-a", "agent-1", capability=f"scope:{index}"
        )

    notes = harness.store.list_observations(NOTEBOOK_ID, "user-a", limit=50)
    assert [row["text"] for row in notes] == ["攒了很久的一句"]


def test_note_flood_never_evicts_call_rows(harness):
    """反方向同样成立:两个环各自独立,不是「短句优先」这种偏袒。"""
    harness.store.append_call(
        NOTEBOOK_ID, "user-a", "agent-1", capability="ask:execute"
    )
    for index in range(AGENT_OBSERVATION_RING_MAX + 50):
        harness.store.append_observation(
            NOTEBOOK_ID, "user-a", "agent-1",
            text=f"note {index}", client_request_id=f"req-{index}",
        )

    calls = harness.store.list_calls(NOTEBOOK_ID, "user-a", limit=50)
    assert [row["capability"] for row in calls] == ["ask:execute"]


def test_call_ring_evicts_within_its_own_kind(harness):
    for index in range(AGENT_CALL_RING_MAX + 5):
        harness.store.append_call(
            NOTEBOOK_ID, "user-a", "agent-1", capability=f"scope:{index}"
        )

    calls = harness.store.list_calls(
        NOTEBOOK_ID, "user-a", limit=AGENT_CALL_RING_MAX + 100
    )
    assert len(calls) == AGENT_CALL_RING_MAX
    # 最新的那批活下来,最早的 5 条被淘汰。
    assert calls[0]["capability"] == f"scope:{AGENT_CALL_RING_MAX + 4}"
    assert {row["capability"] for row in calls} == {
        f"scope:{index}" for index in range(5, AGENT_CALL_RING_MAX + 5)
    }


def test_call_rows_carry_no_client_request_id(harness):
    """调用行走 NULL 停车位:两次一模一样的调用**就是两次调用**,不该被幂等
    索引折成一行;写空串而不是 NULL 会让它们真的撞在一起(空串不是 NULL,会
    参与那个部分唯一索引)。"""
    harness.store.append_call(NOTEBOOK_ID, "user-a", "agent-1", capability="ask:execute")
    harness.store.append_call(NOTEBOOK_ID, "user-a", "agent-1", capability="ask:execute")

    with harness.database.connect() as connection:
        rows = connection.execute(
            "SELECT client_request_id FROM agent_observations WHERE kind='call'"
        ).fetchall()
    assert len(rows) == 2
    assert all(row["client_request_id"] is None for row in rows)


def test_clear_narrows_by_kind_and_still_clears_both_by_default(harness):
    def seed() -> None:
        harness.store.append_observation(
            NOTEBOOK_ID, "user-a", "agent-1",
            text="written", client_request_id=f"req-{harness.clock()}",
        )
        harness.store.append_call(
            NOTEBOOK_ID, "user-a", "agent-1", capability="ask:execute"
        )

    seed()
    removed = harness.store.clear_observations(NOTEBOOK_ID, "user-a", kind="call")
    assert removed == 1
    assert harness.store.list_calls(NOTEBOOK_ID, "user-a", limit=10) == []
    assert len(harness.store.list_observations(NOTEBOOK_ID, "user-a", limit=10)) == 1

    seed()
    # 缺省仍然清两种——成员被移出共享笔记本时走的就是这一条,漏掉调用记账
    # 就等于在他失去访问权的那一刻把行留在库里。
    removed_all = harness.store.clear_observations(NOTEBOOK_ID, "user-a")
    assert removed_all == 3
    assert harness.store.list_calls(NOTEBOOK_ID, "user-a", limit=10) == []
    assert harness.store.list_observations(NOTEBOOK_ID, "user-a", limit=10) == []


def test_clear_by_kind_and_agent_together_leaves_other_agents_calls(harness):
    harness.store.append_call(NOTEBOOK_ID, "user-a", "agent-1", capability="ask:execute")
    harness.store.append_call(
        NOTEBOOK_ID, "user-a", "agent-2", capability="knowledge:read"
    )

    removed = harness.store.clear_observations(
        NOTEBOOK_ID, "user-a", agent_profile_id="agent-1", kind="call"
    )
    assert removed == 1
    remaining = harness.store.list_calls(NOTEBOOK_ID, "user-a", limit=10)
    assert [row["agent_profile_id"] for row in remaining] == ["agent-2"]


def test_call_ledger_is_per_owner_and_per_notebook(harness):
    harness.store.append_call(NOTEBOOK_ID, "user-a", "agent-1", capability="ask:execute")

    assert harness.store.list_calls(NOTEBOOK_ID, "user-b", limit=10) == []
    assert harness.store.list_calls(OTHER_NOTEBOOK_ID, "user-a", limit=10) == []
