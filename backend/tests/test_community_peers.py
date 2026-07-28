"""Task 5 — community_peers 原语的自给自足测试。

刻意**不** import 真实 SQLiteRepository。改为在临时 sqlite 里直接建
community_members / concept_clusters / notebooks 三表并插最小数据，配一个只暴露
community_peers 真正消费的两个接口的假 repo（Task 13 起持久化读走
repo._runtime.unified_kg —— 这里给假 repo 组一个真 UnifiedKgStore，其 database
座是共享同一条连接的最小包装）：
  · _runtime.unified_kg → UnifiedKgStore(共享连接)，社区/簇/焦点读接口
  · event_log.emit      → 把事件收集进 list 供断言

覆盖：
  1) 正常出兄弟、按 (keyword_score×centrality) 排序、排除焦点自身；
  2) 焦点名解析不到 → [] + emit community_unavailable{reason:focal_unresolved}；
  3) 社区表空（焦点有 canonical 但无社区行）→ [] + emit
     community_unavailable{reason:not_built}。
"""
from __future__ import annotations

import sqlite3

import pytest

from app.repositories.sqlite.unified_kg_store import UnifiedKgStore
from app.services.communities import (
    CommunityQueryService, _norm,
)


# --------------------------------------------------------------------------- #
# 最小假 repo：一个持久 sqlite 连接 + 事件收集器。
# sqlite3.Connection 本身就是上下文管理器（进入/退出=事务提交/回滚，且不关闭连接），
# 所以 community_peers 里的 `with repo._connect() as db:` 能正常工作。
# --------------------------------------------------------------------------- #
class _EventLog:
    def __init__(self) -> None:
        self.events: list = []

    def emit(self, event) -> None:  # noqa: ANN001
        self.events.append(event)


class _FakeDatabase:
    """UnifiedKgStore 的最小 database 座：connect() 返回共享连接。
    sqlite3.Connection 自身即上下文管理器（进入/退出=事务提交/回滚，不关连接）。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def connect(self) -> sqlite3.Connection:
        return self._conn


class _FakeRuntime:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.unified_kg = UnifiedKgStore(_FakeDatabase(conn))


class _FakeRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._runtime = _FakeRuntime(conn)
        self.event_log = _EventLog()
        self.community_queries = CommunityQueryService(
            notebooks=object(),
            unified_kg=self._runtime.unified_kg,
            event_log=self.event_log,
        )


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE notebooks (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            tier TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            updated_at TEXT,
            created_by TEXT
        );
        CREATE TABLE notebook_bases (
            notebook_id TEXT NOT NULL,
            base_notebook_id TEXT NOT NULL,
            created_at TEXT,
            created_by TEXT
        );
        CREATE TABLE concept_clusters (
            notebook_id TEXT NOT NULL,
            canonical_id TEXT NOT NULL,
            canonical_name TEXT NOT NULL
        );
        CREATE TABLE community_members (
            canonical_id TEXT NOT NULL,
            notebook_id TEXT NOT NULL,
            level INTEGER NOT NULL DEFAULT 0,
            community_id TEXT NOT NULL,
            canonical_name TEXT NOT NULL DEFAULT '',
            centrality REAL NOT NULL DEFAULT 0
        );
        """
    )
    return conn


def _seed_base_community(conn: sqlite3.Connection) -> None:
    """base 库 nb-base：焦点 DeepSeek-V4 与 Qwen-X / GPT-Y 同社区 cm-1；
    另有一个别的社区 cm-2 的成员 Mixtral-Z 不应被带出。"""
    conn.execute(
        "INSERT INTO notebooks (id, tier, updated_at) VALUES (?,?,?)",
        ("nb-base", "base", "2026-07-07T00:00:00"),
    )
    clusters = [
        ("nb-base", "can-deepseek", "DeepSeek-V4"),
        ("nb-base", "can-qwen", "Qwen-X"),
        ("nb-base", "can-gpt", "GPT-Y"),
        ("nb-base", "can-mixtral", "Mixtral-Z"),
    ]
    conn.executemany(
        "INSERT INTO concept_clusters (notebook_id, canonical_id, canonical_name) VALUES (?,?,?)",
        clusters,
    )
    # cm-1: focal + 两个兄弟。centrality 故意让 GPT-Y 更高，用来验证
    # keyword_score(query) 能把词法相关的 Qwen-X 顶到前面（query 里含 "qwen"）。
    members = [
        ("can-deepseek", "nb-base", 0, "cm-1", "DeepSeek-V4", 9.0),
        ("can-qwen", "nb-base", 0, "cm-1", "Qwen-X", 1.0),
        ("can-gpt", "nb-base", 0, "cm-1", "GPT-Y", 5.0),
        # 另一个社区，不该出现在 focal 的兄弟里
        ("can-mixtral", "nb-base", 0, "cm-2", "Mixtral-Z", 8.0),
    ]
    conn.executemany(
        "INSERT INTO community_members "
        "(canonical_id, notebook_id, level, community_id, canonical_name, centrality) "
        "VALUES (?,?,?,?,?,?)",
        members,
    )
    conn.commit()


@pytest.fixture
def repo_with_communities() -> _FakeRepo:
    conn = _make_db()
    _seed_base_community(conn)
    return _FakeRepo(conn)


@pytest.fixture
def repo_no_communities() -> _FakeRepo:
    """有 concept_clusters（焦点能解析到 canonical）但 community_members 空 →
    模拟"社区未建"。"""
    conn = _make_db()
    conn.execute(
        "INSERT INTO notebooks (id, tier, updated_at) VALUES (?,?,?)",
        ("nb-base", "base", "2026-07-07T00:00:00"),
    )
    conn.execute(
        "INSERT INTO concept_clusters (notebook_id, canonical_id, canonical_name) VALUES (?,?,?)",
        ("nb-base", "can-deepseek", "DeepSeek-V4"),
    )
    conn.commit()
    return _FakeRepo(conn)


# --------------------------------------------------------------------------- #
# _norm / mounted_base_ids 基础
# --------------------------------------------------------------------------- #
def test_norm_collapses_ws_and_lowercases():
    assert _norm("  DeepSeek   V4  ") == "deepseek v4"
    assert _norm("") == ""
    assert _norm(None) == ""  # type: ignore[arg-type]
