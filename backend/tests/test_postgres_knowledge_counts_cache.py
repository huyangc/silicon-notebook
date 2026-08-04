"""PostgreSQL 开路计数缓存的 seq-gating 单测(codex 第4轮 P2)。

纯 Python + fake db,无需 postgres 服务器——故放在主测试根、不进 backend/tests/postgres/
(那目录整体在无服务器时 skip)。验证 checkup H6 的 visible 计数按 kg_mutation_seq memo:
同 seq 不重查、seq 变重查、invalidate 后重查(安全阀)。
"""
from __future__ import annotations

from app.repositories.postgres import knowledge_counts_cache as kcc


class _Cursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeDB:
    """记录 pending 查询被真正执行了几次(seq 读不计入)。"""

    def __init__(self, seq: int):
        self.seq = seq
        self.query_calls = 0
        self.pending_sql: list[str] = []

    def execute(self, sql, params):
        if "kg_mutation_seq" in sql:
            return _Cursor({"kg_mutation_seq": self.seq})
        self.query_calls += 1  # pending 相关子查询
        self.pending_sql.append(sql)
        return _Cursor({"c": 42})


def test_postgres_h6_visible_count_is_seq_gated_memo():
    kcc.invalidate()  # 清进程缓存,免跨测试污染
    db = _FakeDB(seq=5)
    assert kcc.visible_pending_source_count(db, "nb-x") == 42
    assert kcc.visible_pending_source_count(db, "nb-x") == 42
    assert db.query_calls == 1  # 同 kg_mutation_seq → 只跑一次冷查询(seq-gated)

    db.seq = 6  # kg_mutation_seq bump(数据变)
    assert kcc.visible_pending_source_count(db, "nb-x") == 42
    assert db.query_calls == 2  # seq 变 → 重查

    kcc.invalidate("nb-x")  # 安全阀
    assert kcc.visible_pending_source_count(db, "nb-x") == 42
    assert db.query_calls == 3  # invalidate 后重查


def test_postgres_pending_and_visible_are_independent_memos():
    """全集 pending 与 visible 各自独立 memo(共享 seq gate),互不串缓存。"""
    kcc.invalidate()
    db = _FakeDB(seq=1)
    kcc.pending_source_count(db, "nb-y")
    kcc.visible_pending_source_count(db, "nb-y")
    assert db.query_calls == 2  # 两个独立计数各查一次
    kcc.pending_source_count(db, "nb-y")
    kcc.visible_pending_source_count(db, "nb-y")
    assert db.query_calls == 2  # 同 seq → 都命中缓存


def test_pending_query_is_source_driven_and_reads_latest_run_once():
    """Regression: never evaluate latest-run state once per knowledge object."""
    db = _FakeDB(seq=1)

    assert kcc._pending_query(db, "nb-shape", visible_only=True) == 42

    sql = db.pending_sql[-1]
    assert sql.count("LATERAL") == 3
    assert sql.count("FROM extraction_runs er") == 1
    assert sql.count("FROM knowledge_objects k") == 1
    assert "ORDER BY er.created_at DESC,er.ordinal DESC LIMIT 1" in sql
    assert "source_kg.found IS NULL" in sql
    assert "s.source_type NOT IN ('memory','knowhow')" in sql
    assert "NOT EXISTS(SELECT 1 FROM knowledge_objects" not in sql


def test_physical_pending_query_keeps_hidden_sources():
    db = _FakeDB(seq=1)

    assert kcc._pending_query(db, "nb-shape", visible_only=False) == 42

    assert "s.source_type NOT IN" not in db.pending_sql[-1]
