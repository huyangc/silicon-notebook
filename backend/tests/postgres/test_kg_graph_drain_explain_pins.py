"""EXPLAIN pins for the K1 additions to ``delete_notebook_kg``'s drain
(debt-fix 2026-10-12): the conflict-candidate and merge-review steps' backlog
probes and final-pass DELETEs.

The statements are CAPTURED from the real store methods (a recording wrapper
that runs reads and records, without running, every DELETE) and then
EXPLAINed, so the pin is on the text that actually runs. Judged with seqscan
off, as the sibling pins are: the test bed has thousands of rows and no
production-scale cost separation, but a statement rewritten into a shape with
no index path shows up as a Seq Scan.
"""
from __future__ import annotations

import re

import pytest

from app.repositories.postgres.migrator import PostgresMigrator

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_kg_graph_drain_explain"),
]

_NOW = "2026-01-01T00:00:00+00:00"
_TABLES = (
    "kg_conflict_candidates", "merge_review_jobs", "knowledge_objects",
    "knowledge_relations",
)


def _seed(postgres_database) -> None:
    with postgres_database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        db.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
            "username,password_hash,password_salt,password_iterations) "
            "VALUES ('u-a','a@example.test','a','user','active',%s,%s,'a','','',0)",
            (_NOW, _NOW),
        )
        for nb in ("nb", "nb-other"):
            db.execute(
                "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
                "created_at,updated_at,tier) "
                "VALUES (%s,'N','','','ready','u-a',%s,%s,'personal')",
                (nb, _NOW, _NOW),
            )
            db.execute(
                "INSERT INTO kg_conflict_candidates (id,notebook_id,kind,left_ref,"
                "right_ref,status,created_at,updated_at) SELECT %s||'-kcc-'||g,%s,"
                "'node','ko-'||g,'ko-'||(g+1),'pending',%s,%s "
                "FROM generate_series(1,5000) g",
                (nb, nb, _NOW, _NOW),
            )
            db.execute(
                "INSERT INTO merge_review_jobs (notebook_id,status,total,done,"
                "updated_at,error) VALUES (%s,'done',1,1,%s,'')",
                (nb, _NOW),
            )
        # Another notebook's graph: the conflict step's reference checks have
        # real id sets to probe (an empty table would make any plan look fine).
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,created_at,updated_at) "
            "VALUES ('src-o','nb-other','t','upload',%s,%s)", (_NOW, _NOW),
        )
        db.execute(
            "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,payload,"
            "source_id,created_at,updated_at) SELECT 'ko-o-'||g,'nb-other','concept',"
            "'approved','{}'::jsonb,'src-o',%s,%s FROM generate_series(0,29999) g",
            (_NOW, _NOW),
        )
        db.execute(
            "INSERT INTO knowledge_relations(id,notebook_id,source_id,source_object_id,"
            "target_object_id,edge_type,created_at) SELECT 'kr-o-'||g,'nb-other','src-o',"
            "'ko-o-'||g,'ko-o-'||((g+1)%%30000),'depends_on',%s "
            "FROM generate_series(0,29999) g",
            (_NOW,),
        )
        for table in _TABLES:
            db.execute(f"ANALYZE {table}")


class _Recording:
    """Records everything, runs reads only (an EXPLAIN needs only the text
    and parameters)."""

    def __init__(self, inner, log):
        self._inner, self._log = inner, log

    def execute(self, statement, params=()):
        text = statement if isinstance(statement, str) else statement.as_string(
            self._inner
        )
        self._log.append((" ".join(text.split()), tuple(params or ())))
        if text.lstrip().upper().startswith("SELECT"):
            return self._inner.execute(statement, params)
        return _NoRows()

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _NoRows:
    rowcount = 0

    def fetchone(self):
        return None

    def fetchall(self):
        return []


def _plan(connection, statement: str, params: tuple) -> str:
    connection.execute("SET LOCAL enable_seqscan=off")
    # The conflict step's reference checks are four correlated NOT EXISTS
    # under an OR: PostgreSQL may plan each as a *hashed* SubPlan, which reads
    # every knowledge_objects / knowledge_relations id of every notebook —
    # chosen here only because the test bed's tables are tiny. Production-
    # sized id sets do not fit the hash memory budget; a small work_mem
    # reproduces that, so the pin checks the per-row primary-key path.
    connection.execute("SET LOCAL work_mem='64kB'")
    rows = connection.execute(f"EXPLAIN (COSTS OFF) {statement}", params).fetchall()
    return "\n".join(str(row["QUERY PLAN"]) for row in rows)


@pytest.fixture
def captured(postgres_database):
    from app.repositories.postgres.knowledge_store import (
        _GRAPH_DRAIN_STEPS,
        KnowledgeStore,
    )

    assert PostgresMigrator(postgres_database).migrate()
    _seed(postgres_database)
    log: list = []
    with postgres_database.connect() as db:
        rec = _Recording(db, log)
        conflict_step = next(i for i, (t, *_r) in enumerate(_GRAPH_DRAIN_STEPS)
                             if t == "kg_conflict_candidates")
        assert KnowledgeStore.graph_drain_backlog(
            rec, "nb", 10**9, conflict_step) is None
        # merge_review_jobs is only ever probed at threshold 0, by the sweep
        merge_step = next(i for i, (t, *_r) in enumerate(_GRAPH_DRAIN_STEPS)
                          if t == "merge_review_jobs")
        KnowledgeStore.graph_drain_backlog(rec, "nb", 0, merge_step, scope="sweep")
        KnowledgeStore.delete_notebook_graph_rows(rec, "nb", _NOW)
    plans: dict[str, list[tuple[str, str]]] = {}
    with postgres_database.connect() as connection:
        for statement, params in log:
            match = re.match(r"(?:SELECT .*? FROM|DELETE FROM) \"?(\w+)\"?", statement)
            if not match or match.group(1) not in ("kg_conflict_candidates",
                                                   "merge_review_jobs"):
                continue
            plans.setdefault(match.group(1), []).append(
                (statement, _plan(connection, statement, params)))
    return plans


def test_conflict_and_merge_review_steps_are_index_bounded(captured):
    """K1:冲突候选的探针与终局 DELETE 走 (notebook_id, status) 前缀索引;两侧
    引用的存在性检查对对象表、关系表各自是一次以该侧引用为键的主键探测
    (质量评审 P3-2:逐侧断言 ``Index Cond: (id = …left_ref/right_ref)``,
    且计划里没有 ``Materialize`` / ``hashed SubPlan``——整索引扫描或整表
    哈希的形态即使用上主键名也会被拦下)。合并预审行走主键。"""
    import re

    conflict = captured["kg_conflict_candidates"]
    assert len(conflict) == 2, [s for s, _p in conflict]  # probe + final DELETE
    for statement, plan in conflict:
        assert "Seq Scan" not in plan, f"{statement}\n{plan}"
        assert "Materialize" not in plan and "hashed SubPlan" not in plan, plan
        assert "idx_conflict_candidates_nb_status" in plan, plan
        for index, table in (("pk_knowledge_objects", "knowledge_objects"),
                             ("pk_knowledge_relations", "knowledge_relations")):
            for side in ("left_ref", "right_ref"):
                assert re.search(
                    rf"Index (?:Only )?Scan using {index} on {table} \w+\n\s+"
                    rf"Index Cond: \(id = kg_conflict_candidates\.{side}\)",
                    plan,
                ), f"{table} {side} is not a primary-key probe:\n{plan}"
    merge = captured["merge_review_jobs"]
    assert len(merge) == 2, [s for s, _p in merge]  # sweep probe + final DELETE
    for statement, plan in merge:
        assert "Seq Scan" not in plan, f"{statement}\n{plan}"
        assert "pk_merge_review_jobs" in plan, plan
