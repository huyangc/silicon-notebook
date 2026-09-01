"""Batch 3·W1 PR-3 Phase B: phase 3 (rows) + phase 4 (files) — SQLite lane.

Covers (see ``tests/postgres/test_notebook_delete_jobization_pg.py`` for the
PostgreSQL-only additions this file cannot exercise: real EXPLAIN plans for
the form-two ctid path, real advisory-lock contention with scale build):

  1. Registry guards (``notebook_delete_tables``): every closure/closure-
     external table is covered exactly once, the four archive-input tables
     and the two D-class tables never appear, cursor keys are unique.
  2. Phase 3 end to end: A-class direct tables, form-two tables (including
     closure-external, no-PK tables), and all five B-class chains (knowhow,
     indexing_pipeline_stages, memory_items, and the two read-only-parent
     chains source_elements/ask_trace_steps) all converge to zero rows.
  3. Phase 3 idempotent resume: a crash mid-chain (simulated by truncating
     the job row's `phase` back to 'quiesce' with a stale cursor) picks up
     and finishes correctly rather than re-doing already-deleted work badly.
  4. Phase 4: source files + asset directory deletion via the
     ``notebook_delete_files`` side table, resumable via its own cursor.
  5. Form-two's runner-level stall/termination logic in isolation (a fake
     store double drives the exact "rowcount < batch size but rows remain"
     shape the design's own worked example describes) — see this file's
     ``test_form_two_never_terminates_early_on_a_sub_batch_rowcount`` for
     the mutation this guards (§1.5's "定稿后处理" #1).
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.services import notebook_delete_tables as ndt
from app.services.sqlite_repository import SQLiteRepository


NOW = "2026-09-01T00:00:00"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings(_env_file=None))


def _seed_user_and_notebook(repo, notebook_id="nb1", owner="u1", status="ready"):
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO users (id,email,display_name,role,status,username,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (owner, f"{owner}@x", owner.upper(), "user", "active", owner, NOW, NOW),
        )
        db.execute(
            "INSERT INTO notebooks (id,name,created_by,status,created_at,"
            "updated_at) VALUES (?,?,?,?,?,?)",
            (notebook_id, f"NB-{notebook_id}", owner, status, NOW, NOW),
        )


def _seed_rich_notebook(repo, notebook_id, owner, source_id, n_extra=0):
    """One source with an element+embedding, a knowledge object, a memory
    item with all three children, a knowhow table with one full column/row/
    cell chain, an ask job with a trace step, a conversation, and one row
    each in a couple of form-two (including no-PK closure-external) tables
    — enough to exercise every unit in ``PHASE_3_PLAN``."""
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,file_path,source_type,"
            "status,parse_status,uploaded_by,created_at,updated_at) VALUES "
            "(?,?,?,?,?,?,?,?,?,?)",
            (source_id, notebook_id, "S", "", "pdf", "ready", "parsed", owner, NOW, NOW),
        )
        db.execute(
            "INSERT INTO source_elements (id,source_id,element_type,"
            "location_label,text,metadata,created_at) VALUES (?,?,?,?,?,?,?)",
            (f"{source_id}-el", source_id, "paragraph", "p1", "hello", "{}", NOW),
        )
        db.execute(
            "INSERT INTO element_embeddings (element_id,source_id,notebook_id,"
            "vector,created_at) VALUES (?,?,?,?,?)",
            (f"{source_id}-el", source_id, notebook_id, "[]", NOW),
        )
        db.execute(
            "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,"
            "owner,payload,evidence,source_id,created_at,updated_at) VALUES "
            "(?,?,?,?,?,?,?,?,?,?)",
            (f"{source_id}-ko", notebook_id, "concept", "extracted", "system",
             "{}", "{}", source_id, NOW, NOW),
        )
        db.execute(
            "INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,"
            "created_at) VALUES (?,?,?,?)",
            (f"{source_id}-ko", notebook_id, "[]", NOW),
        )
        db.execute(
            "INSERT INTO memory_items (id,notebook_id,created_by,origin,"
            "status,promotion_state,title,content_md,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"{source_id}-mi", notebook_id, owner, "ask_answer", "candidate",
             "none", "T", "C", NOW, NOW),
        )
        db.execute(
            "INSERT INTO memory_embeddings (memory_id,model,dimension,vector,"
            "updated_at) VALUES (?,?,?,?,?)",
            (f"{source_id}-mi", "m", 1, "[]", NOW),
        )
        db.execute(
            "INSERT INTO memory_provenance (id,memory_id,origin,payload_json,"
            "created_at) VALUES (?,?,?,?,?)",
            (f"{source_id}-mp", f"{source_id}-mi", "ask_answer", "{}", NOW),
        )
        db.execute(
            "INSERT INTO memory_revisions (id,memory_id,revision,title,"
            "content_md,tags_json,status,promotion_state,changed_by,"
            "change_reason,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f"{source_id}-mrev", f"{source_id}-mi", 1, "T", "C", "[]",
             "candidate", "none", owner, "", NOW),
        )
        db.execute(
            "INSERT INTO knowhow_tables (id,notebook_id,title,description,"
            "mutation_seq,created_by,created_at,updated_at) VALUES "
            "(?,?,?,?,?,?,?,?)",
            (f"{source_id}-kt", notebook_id, "KT", "", 0, owner, NOW, NOW),
        )
        db.execute(
            "INSERT INTO knowhow_columns (id,table_id,name,role,position) "
            "VALUES (?,?,?,?,?)",
            (f"{source_id}-kc", f"{source_id}-kt", "Col", "value", 0),
        )
        db.execute(
            "INSERT INTO knowhow_rows (id,table_id,position,projection_status,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (f"{source_id}-kr", f"{source_id}-kt", 0, "none", NOW, NOW),
        )
        db.execute(
            "INSERT INTO knowhow_cells (id,row_id,column_id,content_md,"
            "updated_at) VALUES (?,?,?,?,?)",
            (f"{source_id}-kx", f"{source_id}-kr", f"{source_id}-kc", "v", NOW),
        )
        db.execute(
            "INSERT INTO knowhow_cell_code (id,row_id,column_id,code_text,"
            "language,updated_by,cell_content_hash,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (f"{source_id}-kxc", f"{source_id}-kr", f"{source_id}-kc",
             "1+1", "python", owner, "hash1", NOW, NOW),
        )
        db.execute(
            "INSERT INTO knowhow_changes (id,table_id,seq,kind,actor,origin,"
            "payload_json,fingerprint,note,created_at) VALUES "
            "(?,?,?,?,?,?,?,?,?,?)",
            (f"{source_id}-kch", f"{source_id}-kt", 0, "cell_edit", owner,
             "user", "{}", "fp", "", NOW),
        )
        db.execute(
            "INSERT INTO knowhow_milestones (id,table_id,seq,name,note,"
            "created_by,created_at) VALUES (?,?,?,?,?,?,?)",
            (f"{source_id}-kms", f"{source_id}-kt", 0, "M1", "", owner, NOW),
        )
        db.execute(
            "INSERT INTO kg_build_jobs (id,notebook_id,created_by,mode,"
            "status,stage,total_sources,completed_sources,failed_sources,"
            "error_code,error_message,created_at,updated_at,finished_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"{source_id}-kgj", notebook_id, owner, "incremental",
             "succeeded", "done", 1, 1, 0, "", "", NOW, NOW, NOW),
        )
        db.execute(
            "INSERT INTO indexing_pipeline_stages (job_id,notebook_id,"
            "pipeline_id,pipeline_version,pipeline_generation,"
            "source_snapshot,created_at,updated_at) VALUES "
            "(?,?,?,?,?,?,?,?)",
            (f"{source_id}-kgj", notebook_id, "builtin", "1", "g1", "[]",
             NOW, NOW),
        )
        db.execute(
            "INSERT INTO indexing_pipeline_stage_sources (job_id,source_id,"
            "status,payload,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (f"{source_id}-kgj", source_id, "completed", "{}", NOW, NOW),
        )
        db.execute(
            "INSERT INTO ask_jobs (id,notebook_id,created_by,mode,status,"
            "question,created_at,updated_at,asked_at) VALUES "
            "(?,?,?,?,?,?,?,?,?)",
            (f"{source_id}-aj", notebook_id, owner, "chunk", "done", "why", NOW, NOW, NOW),
        )
        db.execute(
            "INSERT INTO ask_trace_steps (job_id,seq,step_json,created_at) "
            "VALUES (?,?,?,?)",
            (f"{source_id}-aj", 0, "{}", NOW),
        )
        db.execute(
            "INSERT INTO conversations (id,notebook_id,title,created_by,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (f"{source_id}-cv", notebook_id, "C", owner, NOW, NOW),
        )
        db.execute(
            "INSERT INTO community_members (canonical_id,notebook_id,level,"
            "community_id,canonical_name,centrality) VALUES (?,?,?,?,?,?)",
            (f"{source_id}-cm", notebook_id, 0, f"{source_id}-comm", "C", 0.0),
        )
        db.execute(
            "INSERT INTO knowledge_object_sources (object_id,source_id,"
            "notebook_id) VALUES (?,?,?)",
            (f"{source_id}-ko", source_id, notebook_id),
        )
        db.execute(
            "INSERT INTO agent_notebook_profile (notebook_id,owner_id,label,"
            "value,evidence_json,history_json,revision,updated_by,"
            "updated_origin,created_at,updated_at) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?)",
            (notebook_id, owner, f"{source_id}-L", "V", "{}", "[]", 1, owner,
             "manual", NOW, NOW),
        )


# ---------------------------------------------------------------------------
# 1. Registry guards
# ---------------------------------------------------------------------------


def test_phase_3_plan_covers_every_closure_table_exactly_once():
    names = ndt.phase_3_table_names()
    assert len(names) == ndt.EXPECTED_PHASE_3_TABLE_COUNT


def test_phase_3_plan_never_touches_archive_input_or_d_class_tables():
    """archive-input (phase 5's own job) 与 D 类 (从不清理) 都绝不该出现在
    phase 3 的表序里——这正是「D 类误入表序」变异钉的反向静态断言。``answers``
    是实现期发现的第五个归档读依赖（design 文档正文未点名，见
    ``notebook_delete_tables`` 模块docstring）：相位 5 的 ask 投影经
    `LEFT JOIN answers` 读它，相位 3 若先删它会让归档静默退化——
    ``tests/test_admin_questions.py::test_admin_questions_combines_ask_and_
    report_with_filters`` 就是抓住这个回归的既有用例。"""
    names = ndt.phase_3_table_names()
    forbidden = {
        "ask_jobs", "sources", "reports", "source_paper_meta",  # archive-input
        "answers",  # 5th, undocumented archive-read dependency (see above)
        "object_schemas", "retained_user_activity",  # D-class
        "notebooks", "notebook_delete_jobs", "notebook_delete_files",
    }
    assert not (names & forbidden), names & forbidden


def test_cursor_keys_are_unique():
    assert len(ndt.CURSOR_KEYS) == len(set(ndt.CURSOR_KEYS))


# ---------------------------------------------------------------------------
# 2. Phase 3 end to end
# ---------------------------------------------------------------------------

_CHECK_TABLES = (
    ("sources", "notebook_id"),
    ("source_elements", None),  # checked via join below
    ("element_embeddings", "notebook_id"),
    ("knowledge_objects", "notebook_id"),
    ("knowledge_embeddings", "notebook_id"),
    ("memory_items", "notebook_id"),
    ("memory_embeddings", None),
    ("memory_provenance", None),
    ("memory_revisions", None),
    ("knowhow_tables", "notebook_id"),
    ("knowhow_columns", None),
    ("knowhow_rows", None),
    ("knowhow_cells", None),
    ("knowhow_cell_code", None),
    ("knowhow_changes", None),
    ("knowhow_milestones", None),
    ("kg_build_jobs", "notebook_id"),
    ("indexing_pipeline_stages", "notebook_id"),
    ("indexing_pipeline_stage_sources", None),
    ("ask_jobs", "notebook_id"),
    ("ask_trace_steps", None),
    ("conversations", "notebook_id"),
    ("community_members", "notebook_id"),
    ("knowledge_object_sources", "notebook_id"),
    ("agent_notebook_profile", "notebook_id"),
)


def test_phase3_and_phase4_converge_to_zero_across_every_unit(repo):
    _seed_user_and_notebook(repo)
    _seed_rich_notebook(repo, "nb1", "u1", "s1")

    job = repo._runtime.notebook_delete_jobs.request("nb1", "u1")
    repo._runtime.notebook_delete.run(job["id"])

    with repo._runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) c FROM notebooks WHERE id='nb1'"
        ).fetchone()["c"] == 0
        for table, filter_column in _CHECK_TABLES:
            if filter_column:
                sql = f"SELECT COUNT(*) c FROM {table} WHERE {filter_column}='nb1'"
            elif table in ("source_elements",):
                sql = "SELECT COUNT(*) c FROM source_elements WHERE source_id='s1'"
            elif table in ("memory_embeddings", "memory_provenance", "memory_revisions"):
                sql = f"SELECT COUNT(*) c FROM {table} WHERE memory_id='s1-mi'"
            elif table in (
                "knowhow_columns", "knowhow_rows", "knowhow_changes",
                "knowhow_milestones",
            ):
                sql = f"SELECT COUNT(*) c FROM {table} WHERE table_id='s1-kt'"
            elif table in ("knowhow_cells", "knowhow_cell_code"):
                sql = f"SELECT COUNT(*) c FROM {table} WHERE row_id='s1-kr'"
            elif table == "ask_trace_steps":
                sql = "SELECT COUNT(*) c FROM ask_trace_steps WHERE job_id='s1-aj'"
            elif table == "indexing_pipeline_stage_sources":
                sql = (
                    "SELECT COUNT(*) c FROM indexing_pipeline_stage_sources "
                    "WHERE job_id='s1-kgj'"
                )
            else:  # pragma: no cover - exhaustiveness guard
                raise AssertionError(table)
            assert db.execute(sql).fetchone()["c"] == 0, table
        # notebook_delete_jobs/files cleaned up by finalize (phase 5).
        assert db.execute(
            "SELECT COUNT(*) c FROM notebook_delete_jobs"
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) c FROM notebook_delete_files"
        ).fetchone()["c"] == 0


def test_phase3_never_deletes_answers_before_phase5_reads_it_for_archival(repo):
    """回归钉（实现期发现，design 文档正文未点名——见 notebook_delete_tables
    模块 docstring）：``answers`` 不是四张归档输入表之一，但相位 5 的 ask
    投影经 ``LEFT JOIN answers`` 读它取更完整的问题文本。若相位 3 把它当成
    普通 A 类表提前删掉，归档会静默退化成 ``ask_jobs.question`` 这个更短的
    版本——`tests/test_admin_questions.py` 的既有用例正是靠这条路径抓到了
    这个回归。这里直接钉住归档字段本身，不依赖那个更大的集成测试。"""
    _seed_user_and_notebook(repo)
    full_question = "如何降低噪声？这是历史已完成提问的尾部检索词"
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO answers (id,notebook_id,question,payload,created_at) "
            "VALUES (?,?,?,?,?)",
            ("answer-1", "nb1", full_question, "{}", NOW),
        )
        db.execute(
            "INSERT INTO ask_jobs (id,notebook_id,conversation_id,created_by,"
            "mode,question,status,answer_id,created_at,updated_at) VALUES "
            "(?,?,?,?,?,?,?,?,?,?)",
            ("ask-1", "nb1", "", "u1", "chunk", "如何降低噪声？", "completed",
             "answer-1", NOW, NOW),
        )

    job = repo._runtime.notebook_delete_jobs.request("nb1", "u1")
    repo._runtime.notebook_delete.run(job["id"])

    with repo._runtime.database.connect() as db:
        row = db.execute(
            "SELECT question FROM retained_user_activity WHERE record_id='ask-1'"
        ).fetchone()
    assert row["question"] == full_question


def test_phase4_deletes_the_source_file_and_its_directory(repo, tmp_path):
    _seed_user_and_notebook(repo)
    notebook_dir = tmp_path / "s" / "notebooks" / "nb1"
    notebook_dir.mkdir(parents=True)
    file_path = notebook_dir / "doc.pdf"
    file_path.write_text("x")
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,file_path,source_type,"
            "status,parse_status,uploaded_by,created_at,updated_at) VALUES "
            "(?,?,?,?,?,?,?,?,?,?)",
            ("s1", "nb1", "S", str(file_path), "pdf", "ready", "parsed", "u1", NOW, NOW),
        )

    job = repo._runtime.notebook_delete_jobs.request("nb1", "u1")
    repo._runtime.notebook_delete.run(job["id"])

    assert not file_path.exists()
    assert not notebook_dir.exists()


def test_phase4_deletes_the_asset_directory(repo, tmp_path):
    _seed_user_and_notebook(repo)
    asset_dir = tmp_path / "s" / "assets" / "nb1"
    asset_dir.mkdir(parents=True)
    (asset_dir / "img.png").write_bytes(b"x")

    job = repo._runtime.notebook_delete_jobs.request("nb1", "u1")
    repo._runtime.notebook_delete.run(job["id"])

    assert not asset_dir.exists()


# ---------------------------------------------------------------------------
# 3. Resume mid-phase-3
# ---------------------------------------------------------------------------


def test_phase3_resumes_mid_chain_after_a_simulated_crash(repo):
    """把 job 手工拨回 phase='quiesce'、cursor 指向 knowhow 链中途,模拟进程在
    该 unit 内崩溃后的重跑——必须收敛到零残留,不因为「已经不是第一次跑这个
    unit」而出错或重复计数。"""
    _seed_user_and_notebook(repo)
    _seed_rich_notebook(repo, "nb1", "u1", "s1")

    job = repo._runtime.notebook_delete_jobs.request("nb1", "u1")
    job_id = job["id"]
    # Simulate: phases 0-2 already completed, phase 3 stopped partway
    # through the knowhow_rows chain (P1-D: rows/cells split out ahead of
    # knowhow_tables) with a cursor before this notebook's only knowhow row
    # (empty resume is always safe/idempotent — the chain's own page read
    # just re-finds the same row). Raw SQL, not advance_phase/mark_running
    # — the row is still 'queued' here; run() below does its own CAS+lease
    # exactly like a fresh dispatch would, it just resumes from this
    # pre-seeded phase/cursor instead of 'mark'.
    with repo._runtime.database.write() as db:
        db.execute(
            "UPDATE notebook_delete_jobs SET phase='quiesce',"
            "cursor_table='chain:knowhow_rows',cursor_key='' WHERE id=?",
            (job_id,),
        )

    repo._runtime.notebook_delete.run(job_id)

    with repo._runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) c FROM notebooks WHERE id='nb1'"
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) c FROM knowhow_tables WHERE notebook_id='nb1'"
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) c FROM knowledge_objects WHERE notebook_id='nb1'"
        ).fetchone()["c"] == 0


def test_phase3_rerun_on_an_already_drained_unit_deletes_nothing_extra(repo):
    """幂等重放:同一批(这里放大到整个 unit)跑两次,第二次必须删 0 行且不报错
    ——直接调用 store 层原语钉住这条不变量,不依赖 runner 的高层调度。"""
    _seed_user_and_notebook(repo)
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO agent_notebook_profile (notebook_id,owner_id,label,"
            "value,evidence_json,history_json,revision,updated_by,"
            "updated_origin,created_at,updated_at) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?)",
            ("nb1", "u1", "L", "V", "{}", "[]", 1, "u1", "manual", NOW, NOW),
        )
    store = repo._runtime.notebook_delete_jobs
    first = store.delete_direct_batch_form_two("agent_notebook_profile", "notebook_id", "nb1", 500)
    second = store.delete_direct_batch_form_two("agent_notebook_profile", "notebook_id", "nb1", 500)
    assert first == 1
    assert second == 0


# ---------------------------------------------------------------------------
# 4. Form-two runner-level stall logic (fake double, no real DB race needed)
# ---------------------------------------------------------------------------


class _FakeClaim:
    """Always-held claim double — these tests exercise the form-two
    stall/termination logic in isolation, not claim handling (see
    ``test_notebook_delete_rows_and_files_pg.py``/the artifact-root tests
    above for that)."""

    def verify_held(self) -> bool:
        return True

    def release(self) -> None:
        return None


class _FakeDeleteJobsForFormTwo:
    """Drives ``_run_form_two`` through the exact "batch rowcount smaller
    than the limit while rows remain" shape a concurrent UPDATE produces
    under READ COMMITTED (design doc §1.5's own worked example) — a
    deterministic stand-in for a real race that would otherwise need a
    statement-level pause to reproduce reliably."""

    def __init__(self, batch_counts):
        self._batch_counts = list(batch_counts)
        self._calls = 0
        self.running = True

    def ownership_snapshot(self, job_id):
        return {
            "status": "running" if self.running else "failed",
            "lease_token": "lease-1",
            "notebook_status": "deleting",
        }

    def delete_direct_batch_form_two(self, table, filter_column, filter_value, limit):
        count = self._batch_counts[self._calls] if self._calls < len(self._batch_counts) else 0
        self._calls += 1
        return count

    def table_has_rows(self, table, filter_column, filter_value):
        return self._calls < len(self._batch_counts)

    def advance_phase(self, *args, **kwargs):
        return True

    def mark_queued(self, *args, **kwargs):
        return True


def test_form_two_never_terminates_early_on_a_sub_batch_rowcount():
    """批 1 返回 3(< 假想的 limit=5,模拟并发 UPDATE 让一行漏网)但后面还有更多
    行——正确实现必须继续循环直到 rowcount==0,总调用 3 次全部清空。若把终止
    条件误改成 `count < limit`,批 1 的 3 就会被当成「已经删完」提前退出,
    只调用 1 次。"""
    from app.services.notebook_delete import NotebookDeleteJobRunner

    runner = NotebookDeleteJobRunner.__new__(NotebookDeleteJobRunner)
    fake = _FakeDeleteJobsForFormTwo([3, 2, 0])
    runner.delete_jobs = fake

    result = runner._run_form_two(
        "job-1", "nb1", "lease-1", _FakeClaim(), "community_members",
        "notebook_id", "nb1", residual=False,
    )
    assert result is True
    assert fake._calls == 3  # kept going past the sub-limit rowcount


class _FakeArtifactStore:
    def __init__(self, base):
        self._base = base

    def scale_dir(self, notebook_id):
        return self._base / "kg_index" / notebook_id

    def viz_dir(self, notebook_id):
        return self._base / "kg_viz" / notebook_id

    def source_partition_dir(self, notebook_id):
        return self._base / "kg_index_partitions" / notebook_id


class _FakeLockHandle:
    """#643 不变量①：真 lock 句柄用真的 pg_advisory 会话验真；这里替身只
    控制 ``verify_held`` 的返回序列,不牵涉真实数据库。"""

    def __init__(self, verify_results):
        self._verify_results = list(verify_results)
        self.calls = 0
        self.released = False

    def verify_held(self):
        result = self._verify_results[self.calls] if self.calls < len(self._verify_results) else True
        self.calls += 1
        return result

    def release(self):
        self.released = True


def test_phase4_stops_deleting_artifact_roots_the_moment_verify_held_fails(repo, tmp_path):
    """#643 不变量①的变异钉：每根删除前必须复验持锁。这里让 ``verify_held``
    在第二个 sibling 前返回 False,断言(a)第一个已删、(b)第二个原样保留、
    (c)作业置回 waiting、(d)绝不抛异常——就地停手,不是报错中止。"""
    _seed_user_and_notebook(repo)
    base = tmp_path / "art"
    (base / "kg_index" / "nb1").mkdir(parents=True)
    (base / "kg_index" / "nb1.old").mkdir(parents=True)

    job = repo._runtime.notebook_delete_jobs.request("nb1", "u1")
    lease_token = repo._runtime.notebook_delete_jobs.mark_running(
        job["id"], stale_cutoff_seconds=300,
    )
    runner = repo._runtime.notebook_delete
    runner._scale_artifact_store = _FakeArtifactStore(base)

    # §T-3b's deletion order is .tmp-* -> .tmp -> .old -> live (last), so with
    # only .old and the live dir present here, .old goes FIRST.
    handle = _FakeLockHandle([True, False])
    result = runner._delete_artifact_roots(
        job["id"], "nb1", lease_token, handle, residual=False,
    )

    assert result is False
    assert not (base / "kg_index" / "nb1.old").exists()  # first sibling: deleted
    assert (base / "kg_index" / "nb1").exists()  # live: untouched, verify_held failed first
    row = repo._runtime.notebook_delete_jobs.get(job["id"])
    # P2-c: claim-unavailable/lost cases park 'queued', not 'waiting' --
    # 'waiting' is reserved for phase 2's quiesce alone.
    assert row["status"] == "queued"


def test_phase4_deletes_every_sibling_when_the_lock_stays_held(repo, tmp_path):
    """反向对照：verify_held 全程为真时,两个 sibling 都必须被删干净——防止
    「去掉 verify_held」的变异被一条只测负例的用例放过。"""
    _seed_user_and_notebook(repo)
    base = tmp_path / "art"
    (base / "kg_index" / "nb1").mkdir(parents=True)
    (base / "kg_index" / "nb1.old").mkdir(parents=True)

    job = repo._runtime.notebook_delete_jobs.request("nb1", "u1")
    lease_token = repo._runtime.notebook_delete_jobs.mark_running(
        job["id"], stale_cutoff_seconds=300,
    )
    runner = repo._runtime.notebook_delete
    runner._scale_artifact_store = _FakeArtifactStore(base)

    handle = _FakeLockHandle([True, True])
    result = runner._delete_artifact_roots(
        job["id"], "nb1", lease_token, handle, residual=False,
    )

    assert result is True
    assert not (base / "kg_index" / "nb1").exists()
    assert not (base / "kg_index" / "nb1.old").exists()
    assert handle.calls == 2


def test_form_two_raises_after_three_stalled_rounds_with_rows_still_present():
    from app.services.notebook_delete import NotebookDeleteJobRunner

    runner = NotebookDeleteJobRunner.__new__(NotebookDeleteJobRunner)

    class _AlwaysStalled(_FakeDeleteJobsForFormTwo):
        def table_has_rows(self, table, filter_column, filter_value):
            return True  # rows never actually clear -- pathological stall

    fake = _AlwaysStalled([0, 0, 0, 0, 0])
    runner.delete_jobs = fake

    with pytest.raises(RuntimeError, match="rowcount==0"):
        runner._run_form_two(
            "job-1", "nb1", "lease-1", _FakeClaim(), "community_members",
            "notebook_id", "nb1", residual=False,
        )
