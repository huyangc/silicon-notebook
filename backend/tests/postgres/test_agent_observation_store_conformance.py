"""PostgreSQL conformance for ``AgentObservationStorePort`` (Agentic Memory
P3, T2).

Scope is deliberately narrow, mirroring ``test_agent_profile_store_
conformance.py``'s own rationale: the SQLite side already has full
behavioural coverage in ``tests/test_agent_observation_store.py`` (identical
fixtures, identical assertions — this store is a behavioural mirror by
design). This file only proves the things that are genuinely
backend-specific:

- the idempotency check is expressed as ``INSERT ... ON CONFLICT (...) WHERE
  client_request_id IS NOT NULL DO NOTHING`` rather than SQLite's
  ``begin_immediate`` + a pre-insert ``SELECT`` — a losing concurrent writer
  must land on the SAME row a winning one just created, not raise;
- eviction orders by ``id COLLATE "C" DESC`` to match SQLite's default binary
  collation on a non-C-collated database, and the SAME rows must survive on
  both backends given the SAME inputs;
- ``created_at`` round-trips through a real ``timestamptz`` column and must
  still come back as ISO text through ``project_observation_row``.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

import pytest

from app.repositories.ports import AGENT_CALL_RING_MAX, AGENT_OBSERVATION_RING_MAX
from app.repositories.postgres.agent_observation_store import AgentObservationStore
from app.services.repository_runtime import RepositoryCompatibilitySeams
from tests.agent_observation_parity_cases import (
    AGENT_OBSERVATION_TIE_BREAK_IDS,
    AGENT_OBSERVATION_TIE_BREAK_SURVIVORS,
)

NOW = "2026-08-20T00:00:00+00:00"
NOTEBOOK_ID = "nb-agent-observation"
OTHER_NOTEBOOK_ID = "nb-agent-observation-2"

pytestmark = pytest.mark.postgres_integration


class _Clock:
    """A monotonically advancing ISO timestamp — mirrors the SQLite test's
    own ``_Clock``, one call, one second later, so eviction-ordering
    assertions exercise ``created_at`` rather than the ``id`` tie-break
    alone."""

    def __init__(self, start: str = NOW) -> None:
        from datetime import datetime

        self._base = datetime.fromisoformat(start)
        self._n = 0

    def __call__(self) -> str:
        from datetime import timedelta

        value = (self._base + timedelta(seconds=self._n)).isoformat()
        self._n += 1
        return value


def _seams(clock) -> RepositoryCompatibilitySeams:
    lock = threading.Lock()
    counter: dict[str, int] = {}

    def new_id(prefix: str) -> str:
        with lock:
            counter[prefix] = counter.get(prefix, 0) + 1
            return f"{prefix}-obs-{counter[prefix]:06d}"

    return RepositoryCompatibilitySeams(
        new_id=new_id,
        now=clock,
        copy_chunk_size=lambda: 100,
        remap_json_ids=lambda value, _mapping: value,
        in_chunk_size=lambda: 100,
    )


def _seed(database, *, notebook_ids) -> None:
    mark = "%s"
    with database.write() as connection:
        connection.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
            "username,password_hash,password_salt,password_iterations) "
            f"VALUES ({','.join([mark] * 11)})",
            (
                "user-owner", "owner@example.test", "Owner", "admin", "active",
                NOW, NOW, "u00654322", "", "", 0,
            ),
        )
        for notebook_id in notebook_ids:
            connection.execute(
                "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
                "created_at,updated_at,tier) "
                f"VALUES ({','.join([mark] * 9)})",
                (
                    notebook_id, "NB", "", "engineering", "ready", "user-owner",
                    NOW, NOW, "personal",
                ),
            )


@dataclass
class AgentObservationHarness:
    database: object
    store: AgentObservationStore
    clock: _Clock


@pytest.fixture
def agent_observation_harness(request) -> AgentObservationHarness:
    clock = _Clock()
    seams = _seams(clock)
    database = request.getfixturevalue("postgres_database")
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(database).migrate() == 46
    _seed(database, notebook_ids=(NOTEBOOK_ID, OTHER_NOTEBOOK_ID))
    yield AgentObservationHarness(
        database=database,
        store=AgentObservationStore(database, new_id=seams.new_id, now=seams.now),
        clock=clock,
    )


def test_append_observation_round_trips_created_at_as_iso_text(
    agent_observation_harness,
):
    harness = agent_observation_harness
    observation_id, deduplicated = harness.store.append_observation(
        NOTEBOOK_ID, "user-a", "agent-1",
        text="jsonb round trip? no — plain text column", client_request_id="req-1",
    )
    assert deduplicated is False
    rows = harness.store.recent_observations(NOTEBOOK_ID, "user-a", limit=10)
    assert len(rows) == 1
    assert rows[0]["id"] == observation_id
    assert rows[0]["created_at"] == NOW
    assert set(rows[0]) == {"id", "agent_profile_id", "text", "created_at"}


def test_append_observation_on_conflict_do_nothing_lands_on_the_winning_row(
    agent_observation_harness,
):
    harness = agent_observation_harness
    first_id, first_dup = harness.store.append_observation(
        NOTEBOOK_ID, "user-a", "agent-1",
        text="first", client_request_id="same-request",
    )
    assert first_dup is False

    second_id, second_dup = harness.store.append_observation(
        NOTEBOOK_ID, "user-a", "agent-1",
        text="second — must be ignored", client_request_id="same-request",
    )
    assert second_id == first_id
    assert second_dup is True

    with harness.database.connect() as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS n FROM agent_observations "
            "WHERE notebook_id=%s AND owner_id=%s",
            (NOTEBOOK_ID, "user-a"),
        ).fetchone()
    assert row["n"] == 1


def test_idempotency_key_includes_agent_profile_id(agent_observation_harness):
    harness = agent_observation_harness
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


def test_eviction_tie_breaks_on_id_when_created_at_is_identical(
    agent_observation_harness,
):
    """T2 修复轮回归——取代原先名不副实的
    ``test_eviction_orders_by_created_at_desc_id_collate_c_desc_same_as_
    sqlite``:那条测试用的 ``_Clock`` 每次调用前进一秒,created_at 永不相同,
    ``id COLLATE "C" DESC`` 比较从未真正被触发过——「最后插入的 N 行」与
    「按 id 降序的前 N 行」在那组输入下逐位相同,一次意外的巧合通过。

    这里改为固定时钟(每一行的 created_at 完全相同),把 id 比较从
    created_at 里完全隔离出来;id 列表(``tests.agent_observation_parity_
    cases``)相对插入顺序被打乱,专门让「保留最后插入的 N 行」与「保留 id
    降序最大的 N 行」在这份输入下给出不同结果——与 SQLite 镜像共用同一份
    id 列表与同一份期望存活集合,证明两个后端在并列排序下选出**逐位相同**
    的存活行,而不是各自独立决定。"""
    harness = agent_observation_harness
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

    with harness.database.connect() as connection:
        total_rows = connection.execute(
            "SELECT COUNT(*) AS n FROM agent_observations "
            "WHERE notebook_id=%s AND owner_id=%s",
            (NOTEBOOK_ID, "user-tie"),
        ).fetchone()["n"]
    assert total_rows == AGENT_OBSERVATION_RING_MAX


def test_recent_observations_is_scoped_by_owner_id(agent_observation_harness):
    harness = agent_observation_harness
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


def test_clear_observations_by_agent_profile_only_removes_that_agent(
    agent_observation_harness,
):
    harness = agent_observation_harness
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
    agent_observation_harness,
):
    harness = agent_observation_harness
    harness.store.append_observation(
        NOTEBOOK_ID, "user-a", "agent-1", text="a", client_request_id="r1",
    )
    harness.store.append_observation(
        NOTEBOOK_ID, "user-a", "agent-2", text="b", client_request_id="r2",
    )
    removed = harness.store.clear_observations(NOTEBOOK_ID, "user-a")
    assert removed == 2
    assert harness.store.recent_observations(NOTEBOOK_ID, "user-a", limit=10) == []


def test_append_observation_rejects_empty_client_request_id(
    agent_observation_harness,
):
    """镜像 SQLite 侧同名测试——空 client_request_id 绝不能到达 store,
    它是唯一会让同一个 (notebook_id, owner_id, agent_profile_id) 元组下所有
    「无 id」写入折叠进同一个部分唯一索引槽位的值(索引谓词是
    ``client_request_id IS NOT NULL``,空字符串不是 NULL,照样参与
    ``ON CONFLICT`` 冲突推断)。"""
    harness = agent_observation_harness
    with pytest.raises(ValueError):
        harness.store.append_observation(
            NOTEBOOK_ID, "user-a", "agent-1", text="x", client_request_id="",
        )


def test_notebook_delete_cascades_observation_rows(agent_observation_harness):
    harness = agent_observation_harness
    harness.store.append_observation(
        NOTEBOOK_ID, "user-a", "agent-1", text="a", client_request_id="r1",
    )
    harness.store.append_observation(
        OTHER_NOTEBOOK_ID, "user-a", "agent-1", text="b", client_request_id="r1",
    )
    with harness.database.write() as connection:
        connection.execute("DELETE FROM notebooks WHERE id=%s", (NOTEBOOK_ID,))
    with harness.database.connect() as connection:
        remaining = connection.execute(
            "SELECT notebook_id FROM agent_observations"
        ).fetchall()
    assert [row["notebook_id"] for row in remaining] == [OTHER_NOTEBOOK_ID]


def test_concurrent_appends_at_the_ring_limit_never_exceed_the_cap(
    agent_observation_harness,
):
    """codex #535 R1 P2:满环时两个并发事务各按提交前快照算保留名单,会删同
    一条最旧行、双双提交后组里留 RING_MAX+1 行。per-(notebook, owner) 的
    advisory 事务锁把「插入+淘汰」串行化,组行数恒 ≤ RING_MAX。"""
    import threading

    harness = agent_observation_harness
    store = harness.store
    for index in range(AGENT_OBSERVATION_RING_MAX):
        store.append_observation(
            NOTEBOOK_ID, "user-a", "agent-1",
            text=f"seed {index}", client_request_id=f"ring-seed-{index}",
        )
    barrier = threading.Barrier(2)
    errors: list = []

    def _append(tag: str) -> None:
        try:
            barrier.wait(timeout=10)
            store.append_observation(
                NOTEBOOK_ID, "user-a", "agent-1",
                text=f"burst {tag}", client_request_id=f"ring-burst-{tag}",
            )
        except Exception as exc:  # pragma: no cover - 失败时上浮断言
            errors.append(exc)

    threads = [threading.Thread(target=_append, args=(t,)) for t in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors
    with harness.database.connect() as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS n FROM agent_observations "
            "WHERE notebook_id=%s AND owner_id=%s",
            (NOTEBOOK_ID, "user-a"),
        ).fetchone()
    assert int(row["n"]) == AGENT_OBSERVATION_RING_MAX


# ---------------------------------------------- 调用记账(kind='call')的对等
#
# 与 SQLite 侧同名用例逐条对应。跨后端必须**同样**成立的是那两条互不侵占的
# 性质:环形淘汰按 kind 分组,巡固读取钉死 kind='note'。


def test_append_call_is_invisible_to_both_observation_reads(
    agent_observation_harness,
):
    harness = agent_observation_harness
    call_id = harness.store.append_call(
        NOTEBOOK_ID, "user-a", "agent-1", capability="ask:execute"
    )

    calls = harness.store.list_calls(NOTEBOOK_ID, "user-a", limit=10)
    assert [row["id"] for row in calls] == [call_id]
    assert set(calls[0]) == {"id", "agent_profile_id", "capability", "created_at"}
    assert calls[0]["capability"] == "ask:execute"
    assert calls[0]["created_at"] == NOW

    assert harness.store.recent_observations(NOTEBOOK_ID, "user-a", limit=10) == []
    assert harness.store.list_observations(NOTEBOOK_ID, "user-a", limit=10) == []


def test_call_rows_park_on_null_client_request_id(agent_observation_harness):
    """调用行不参与那条部分唯一索引:两次同样的调用就是两次调用。写空串而不是
    NULL 会让它们真的撞上(空串不是 NULL)。"""
    harness = agent_observation_harness
    harness.store.append_call(NOTEBOOK_ID, "user-a", "agent-1", capability="ask:execute")
    harness.store.append_call(NOTEBOOK_ID, "user-a", "agent-1", capability="ask:execute")

    with harness.database.connect() as connection:
        rows = connection.execute(
            "SELECT client_request_id FROM agent_observations "
            "WHERE notebook_id=%s AND owner_id=%s AND kind='call'",
            (NOTEBOOK_ID, "user-a"),
        ).fetchall()
    assert len(rows) == 2
    assert all(row["client_request_id"] is None for row in rows)


def test_ring_eviction_is_per_kind(agent_observation_harness):
    harness = agent_observation_harness
    harness.store.append_observation(
        NOTEBOOK_ID, "user-a", "agent-1",
        text="攒了很久的一句", client_request_id="keep-me",
    )
    for index in range(AGENT_CALL_RING_MAX + 5):
        harness.store.append_call(
            NOTEBOOK_ID, "user-a", "agent-1", capability=f"scope:{index}"
        )

    notes = harness.store.list_observations(NOTEBOOK_ID, "user-a", limit=50)
    assert [row["text"] for row in notes] == ["攒了很久的一句"]

    calls = harness.store.list_calls(
        NOTEBOOK_ID, "user-a", limit=AGENT_CALL_RING_MAX + 100
    )
    assert len(calls) == AGENT_CALL_RING_MAX


def test_clear_narrows_by_kind(agent_observation_harness):
    harness = agent_observation_harness
    harness.store.append_observation(
        NOTEBOOK_ID, "user-a", "agent-1",
        text="written", client_request_id="req-1",
    )
    harness.store.append_call(NOTEBOOK_ID, "user-a", "agent-1", capability="ask:execute")

    assert harness.store.clear_observations(NOTEBOOK_ID, "user-a", kind="call") == 1
    assert harness.store.list_calls(NOTEBOOK_ID, "user-a", limit=10) == []
    assert len(harness.store.list_observations(NOTEBOOK_ID, "user-a", limit=10)) == 1

    # 缺省仍然清两种(成员移除路径走的就是这一条)。
    harness.store.append_call(NOTEBOOK_ID, "user-a", "agent-1", capability="ask:execute")
    assert harness.store.clear_observations(NOTEBOOK_ID, "user-a") == 2


def test_notebook_delete_cascades_call_rows(agent_observation_harness):
    harness = agent_observation_harness
    harness.store.append_call(NOTEBOOK_ID, "user-a", "agent-1", capability="ask:execute")
    with harness.database.write() as connection:
        connection.execute("DELETE FROM notebooks WHERE id=%s", (NOTEBOOK_ID,))
    assert harness.store.list_calls(NOTEBOOK_ID, "user-a", limit=10) == []
