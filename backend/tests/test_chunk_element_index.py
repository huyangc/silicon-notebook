"""批 5:element→chunk 持久反查索引(chunk_elements)。

三段合同,分别对应三组用例:

1. **迁移**——全新库建到 v46、已部署 v45 库补建(不改旧迁移);
2. **写路径前向维护**——``replace_source_chunks`` 与 knowhow 投影的
   ``insert_rows``/``delete_by_ids`` 在同一写事务内维护反查行,重解析后旧行
   随 chunk 的 FK 级联消失;
3. **离线回填 + 读路径分叉**——分页续跑、代次漂移 fail-closed、完成 flip 标记、
   幂等重跑;未回填的 notebook 逐字走旧全量缓存,回填后点查结果与旧路径语义一致。
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.ports import ChunkWrite
from app.repositories.sqlite.migrations import SCHEMA_VERSION
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings(_env_file=None))


def _seed_source(repo, notebook_id: str, source_id: str = "src-1") -> str:
    now = "2026-08-10T00:00:00+00:00"
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
            (source_id, notebook_id, "t", "md", "ready", now, now),
        )
    return source_id


def _reverse_rows(repo) -> list[tuple[str, str, str]]:
    with repo._connect() as db:
        return [
            (r["notebook_id"], r["element_id"], r["chunk_id"])
            for r in db.execute(
                "SELECT notebook_id,element_id,chunk_id FROM chunk_elements "
                "ORDER BY notebook_id,element_id,chunk_id"
            ).fetchall()
        ]


# --------------------------------------------------------------- 1. migration


def test_fresh_database_creates_the_reverse_index_at_current_version(repo, tmp_path):
    with repo._connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        objects = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE name IN "
                "('chunk_elements','chunk_element_backfills',"
                "'idx_chunk_elements_chunk','idx_chunk_element_backfills_status')"
            ).fetchall()
        }
        assert objects == {
            "chunk_elements",
            "chunk_element_backfills",
            "idx_chunk_elements_chunk",
            "idx_chunk_element_backfills_status",
        }
        columns = {c[1] for c in db.execute("PRAGMA table_info(unified_kg_state)")}
        assert "chunk_elements_indexed" in columns


def test_deployed_v45_database_gains_the_reverse_index_on_open(repo, tmp_path):
    """已部署库补建:回滚到 v45 再开一次,v46 必须补上它自己那几件。"""
    database = tmp_path / "t.db"
    repo.close_local()
    with sqlite3.connect(database) as rollback:
        rollback.execute("DROP TABLE chunk_element_backfills")
        rollback.execute("DROP TABLE chunk_elements")
        rollback.execute(
            "ALTER TABLE unified_kg_state DROP COLUMN chunk_elements_indexed"
        )
        rollback.execute("PRAGMA user_version = 45")

    reopened = SQLiteRepository(Settings(_env_file=None))
    try:
        with reopened._connect() as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            assert db.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name IN "
                "('chunk_elements','chunk_element_backfills')"
            ).fetchone()[0] == 2
            columns = {c[1] for c in db.execute("PRAGMA table_info(unified_kg_state)")}
            assert "chunk_elements_indexed" in columns
    finally:
        reopened.close_local()


# ------------------------------------------------------------- 2. write paths


def test_replace_source_chunks_maintains_reverse_rows(repo):
    notebook = repo.create_notebook(NotebookCreate(name="w"))
    source_id = _seed_source(repo, notebook.id)
    repo._runtime.chunk_store.replace_source_chunks(
        source_id,
        notebook.id,
        [
            ChunkWrite(id="c1", text="a", section_path="", element_ids=["e1", "e2"]),
            ChunkWrite(id="c2", text="b", section_path="", element_ids=["e2"]),
        ],
        created_at="2026-08-10T00:00:00+00:00",
    )

    assert _reverse_rows(repo) == [
        (notebook.id, "e1", "c1"),
        (notebook.id, "e2", "c1"),
        (notebook.id, "e2", "c2"),
    ]


def test_reparse_replaces_reverse_rows_via_the_chunk_cascade(repo):
    """重解析:旧行随 chunk 的 ON DELETE CASCADE 消失,新行在。

    这也是 ``PRAGMA foreign_keys = ON``(SqliteDatabase 每条连接都设)这条前提的
    回归护栏——关掉它这条就红。"""
    notebook = repo.create_notebook(NotebookCreate(name="w2"))
    source_id = _seed_source(repo, notebook.id)
    repo._runtime.chunk_store.replace_source_chunks(
        source_id,
        notebook.id,
        [ChunkWrite(id="c1", text="a", section_path="", element_ids=["old"])],
        created_at="2026-08-10T00:00:00+00:00",
    )
    assert _reverse_rows(repo) == [(notebook.id, "old", "c1")]

    repo._runtime.chunk_store.replace_source_chunks(
        source_id,
        notebook.id,
        [ChunkWrite(id="c9", text="a", section_path="", element_ids=["new"])],
        created_at="2026-08-10T00:01:00+00:00",
    )
    assert _reverse_rows(repo) == [(notebook.id, "new", "c9")]


def test_duplicate_element_ids_in_one_chunk_do_not_break_the_batch(repo):
    notebook = repo.create_notebook(NotebookCreate(name="w3"))
    source_id = _seed_source(repo, notebook.id)
    repo._runtime.chunk_store.replace_source_chunks(
        source_id,
        notebook.id,
        [ChunkWrite(id="c1", text="a", section_path="", element_ids=["e", "e"])],
        created_at="2026-08-10T00:00:00+00:00",
    )
    assert _reverse_rows(repo) == [(notebook.id, "e", "c1")]


def test_projection_insert_and_delete_keep_reverse_rows_in_sync(repo):
    """knowhow 投影的精确写(delete_by_ids + insert_rows)同样前向维护。"""
    notebook = repo.create_notebook(NotebookCreate(name="w4"))
    source_id = _seed_source(repo, notebook.id, "src-kh")
    with repo._write() as db:
        repo._runtime.chunk_store.insert_rows(
            db,
            notebook.id,
            source_id,
            [ChunkWrite(id="k1", text="a", section_path="", element_ids=["ke1"])],
            created_at="2026-08-10T00:00:00+00:00",
        )
    assert _reverse_rows(repo) == [(notebook.id, "ke1", "k1")]

    with repo._write() as db:
        repo._runtime.chunk_store.delete_by_ids(db, ["k1"])
    assert _reverse_rows(repo) == []


# ------------------------------------------------ 3. backfill and read fork


def _seed_chunks(repo, notebook_id: str, source_id: str, spec) -> None:
    now = "2026-08-10T00:00:00+00:00"
    with repo._write() as db:
        for chunk_id, element_ids in spec:
            db.execute(
                "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,"
                "element_ids,created_at) VALUES (?,?,?,?,?,?,?)",
                (chunk_id, notebook_id, source_id, chunk_id, "",
                 json.dumps(list(element_ids)), now),
            )


def _drain(maintenance, notebook_id: str, *, batch_size: int = 2000) -> dict:
    """Run pages until the cursor is exhausted, exactly like the offline CLI.

    A full page never terminates the run on its own: completion is "the cursor
    came back empty", so the last page is always followed by one empty page.
    Tests must drain rather than assume one call finishes a small notebook."""
    progress = maintenance.resume_chunk_element_backfill_batch(
        notebook_id, batch_size=batch_size
    )
    while progress["status"] == "running":
        progress = maintenance.resume_chunk_element_backfill_batch(
            notebook_id, batch_size=batch_size
        )
    return progress


def test_backfill_projects_history_and_flips_the_marker(repo):
    notebook = repo.create_notebook(NotebookCreate(name="b"))
    source_id = _seed_source(repo, notebook.id)
    _seed_chunks(repo, notebook.id, source_id,
                 [("c1", ["e1"]), ("c2", ["e1", "e2"]), ("c3", [])])
    maintenance = repo.maintenance

    assert maintenance.chunk_elements_indexed(notebook.id) is False
    begun = maintenance.begin_chunk_element_backfill(notebook.id)
    assert begun["status"] == "running"
    assert begun["total_chunks"] == 3

    progress = _drain(maintenance, notebook.id)
    assert progress["status"] == "complete"
    assert progress["rows_written"] == 3
    assert maintenance.chunk_elements_indexed(notebook.id) is True
    assert _reverse_rows(repo) == [
        (notebook.id, "e1", "c1"),
        (notebook.id, "e1", "c2"),
        (notebook.id, "e2", "c2"),
    ]


def test_backfill_resumes_from_the_committed_cursor(repo):
    notebook = repo.create_notebook(NotebookCreate(name="b2"))
    source_id = _seed_source(repo, notebook.id)
    _seed_chunks(repo, notebook.id, source_id,
                 [("c1", ["e1"]), ("c2", ["e2"]), ("c3", ["e3"]), ("c4", ["e4"])])
    maintenance = repo.maintenance
    maintenance.begin_chunk_element_backfill(notebook.id)

    first = maintenance.resume_chunk_element_backfill_batch(notebook.id, batch_size=2)
    assert first["status"] == "running"
    assert first["chunks_scanned"] == 2

    # A restart re-enters through begin(): it must resume, not restart.
    resumed = maintenance.begin_chunk_element_backfill(notebook.id)
    assert resumed["status"] == "running"
    assert resumed["resumed"] is True
    assert resumed["chunks_scanned"] == 2

    second = _drain(maintenance, notebook.id, batch_size=2)
    assert second["status"] == "complete"
    assert second["chunks_scanned"] == 4
    assert len(_reverse_rows(repo)) == 4


def test_backfill_fails_closed_when_the_kg_generation_moves(repo):
    notebook = repo.create_notebook(NotebookCreate(name="b3"))
    source_id = _seed_source(repo, notebook.id)
    _seed_chunks(repo, notebook.id, source_id,
                 [("c1", ["e1"]), ("c2", ["e2"]), ("c3", ["e3"])])
    maintenance = repo.maintenance
    maintenance.begin_chunk_element_backfill(notebook.id)
    maintenance.resume_chunk_element_backfill_batch(notebook.id, batch_size=1)

    with repo._write() as db:
        db.execute(
            "INSERT INTO unified_kg_state (notebook_id,dirty,kg_mutation_seq,updated_at) "
            "VALUES (?,0,5,?) ON CONFLICT(notebook_id) DO UPDATE SET "
            "kg_mutation_seq=kg_mutation_seq+1",
            (notebook.id, "2026-08-10T00:02:00+00:00"),
        )

    failed = maintenance.resume_chunk_element_backfill_batch(notebook.id)
    assert failed["status"] == "failed"
    assert failed["failure_code"] == "kg_generation_changed"
    assert maintenance.chunk_elements_indexed(notebook.id) is False

    # The next run starts a fresh rebuild against the new generation.
    restarted = maintenance.begin_chunk_element_backfill(notebook.id)
    assert restarted["status"] == "running"
    assert restarted["chunks_scanned"] == 0
    assert _reverse_rows(repo) == []


def test_new_chunks_during_backfill_do_not_complete_it_early(repo):
    """回填期间新写入的 chunk 不得提前把回填判成完成。

    ``total_chunks`` 在 begin() 冻结，而写路径仍在插入新 chunk；新 chunk 的 id
    可能排在游标**之前**，于是它会被后续页顺带数进 ``chunks_scanned``。若终止
    判据是 `scanned >= total_chunks`，标记（以及读路径）就会在游标之后还有历史
    chunk 没投影时翻真——那些 chunk 从此静默查不到，且没有自愈路径。终止判据
    因此只能是「游标走空」。

    fixture：``c1..c4`` 让 total 冻结为 4，随后插入排序最靠前的 ``a-new``，使
    实际待投影行数变成 5。按旧判据第二页 ``scanned`` 就到 4 并判 complete，而
    ``c4`` 尚未投影。
    """
    notebook = repo.create_notebook(NotebookCreate(name="b8"))
    source_id = _seed_source(repo, notebook.id)
    _seed_chunks(repo, notebook.id, source_id,
                 [("c1", ["e1"]), ("c2", ["e2"]), ("c3", ["e3"]), ("c4", ["e4"])])
    maintenance = repo.maintenance
    begun = maintenance.begin_chunk_element_backfill(notebook.id)
    assert begun["total_chunks"] == 4

    # A concurrent write lands a chunk that sorts BEFORE the cursor's current
    # position, so a later page scans it and inflates `chunks_scanned`.
    _seed_chunks(repo, notebook.id, source_id, [("a-new", ["e-new"])])

    first = maintenance.resume_chunk_element_backfill_batch(notebook.id, batch_size=2)
    assert first["chunks_scanned"] == 2          # a-new, c1
    second = maintenance.resume_chunk_element_backfill_batch(notebook.id, batch_size=2)
    # Old gate: scanned == 4 == total_chunks -> "complete" with c4 unprojected.
    assert second["chunks_scanned"] == 4         # c2, c3
    assert second["status"] == "running", "总数是冻结值，不能当终止判据"
    assert maintenance.chunk_elements_indexed(notebook.id) is False

    final = _drain(maintenance, notebook.id, batch_size=2)
    assert final["status"] == "complete"
    assert final["chunks_scanned"] == 5
    assert maintenance.chunk_elements_indexed(notebook.id) is True
    assert _reverse_rows(repo) == [
        (notebook.id, "e-new", "a-new"),
        (notebook.id, "e1", "c1"),
        (notebook.id, "e2", "c2"),
        (notebook.id, "e3", "c3"),
        (notebook.id, "e4", "c4"),
    ]


def test_completed_backfill_is_idempotent_on_rerun(repo):
    notebook = repo.create_notebook(NotebookCreate(name="b4"))
    source_id = _seed_source(repo, notebook.id)
    _seed_chunks(repo, notebook.id, source_id, [("c1", ["e1"]), ("c2", ["e1"])])
    maintenance = repo.maintenance
    maintenance.begin_chunk_element_backfill(notebook.id)
    _drain(maintenance, notebook.id)
    before = _reverse_rows(repo)

    again = maintenance.begin_chunk_element_backfill(notebook.id)
    assert again["status"] == "complete"
    assert again["already_complete"] is True
    assert _reverse_rows(repo) == before


def test_empty_notebook_completes_immediately(repo):
    notebook = repo.create_notebook(NotebookCreate(name="b5"))
    begun = repo.maintenance.begin_chunk_element_backfill(notebook.id)
    assert begun["status"] == "complete"
    assert begun["total_chunks"] == 0
    assert repo.maintenance.chunk_elements_indexed(notebook.id) is True


def test_clear_resets_rows_and_marker(repo):
    notebook = repo.create_notebook(NotebookCreate(name="b6"))
    source_id = _seed_source(repo, notebook.id)
    _seed_chunks(repo, notebook.id, source_id, [("c1", ["e1"])])
    maintenance = repo.maintenance
    maintenance.begin_chunk_element_backfill(notebook.id)
    _drain(maintenance, notebook.id)

    assert maintenance.clear_chunk_element_index(notebook.id) == 1
    assert _reverse_rows(repo) == []
    assert maintenance.chunk_elements_indexed(notebook.id) is False


def test_mark_failed_records_only_a_stable_code(repo):
    notebook = repo.create_notebook(NotebookCreate(name="b7"))
    source_id = _seed_source(repo, notebook.id)
    _seed_chunks(repo, notebook.id, source_id, [("c1", ["e1"])])
    maintenance = repo.maintenance
    maintenance.begin_chunk_element_backfill(notebook.id)
    maintenance.mark_chunk_element_backfill_failed(notebook.id, "boom: /tmp/secret")

    with repo._connect() as db:
        row = db.execute(
            "SELECT status,failure_code FROM chunk_element_backfills WHERE notebook_id=?",
            (notebook.id,),
        ).fetchone()
    assert row["status"] == "failed"
    assert row["failure_code"] == "chunk_element_backfill_failed"


# ----------------------------------------------------------- read-path fork


def _legacy_map(repo, notebook_id: str, element_ids):
    """The pre-batch-5 whole-notebook scan, as the equivalence baseline."""
    out: dict[str, list] = {}
    # Drained inside the connection scope: id_element_rows streams keyset
    # pages on PostgreSQL (batch-3 W4 T-W4-3.1).
    with repo._connect() as db:
        for row in repo._runtime.chunk_store.id_element_rows(db, notebook_id):
            for element_id in json.loads(row["element_ids"] or "[]"):
                out.setdefault(element_id, []).append(row["id"])
    return {e: out.get(e, []) for e in element_ids}


def test_unbackfilled_notebook_keeps_the_legacy_whole_scan(repo, monkeypatch):
    notebook = repo.create_notebook(NotebookCreate(name="r1"))
    source_id = _seed_source(repo, notebook.id)
    _seed_chunks(repo, notebook.id, source_id, [("c1", ["e1"]), ("c2", ["e1"])])

    calls: list[str] = []
    original = repo.retrieval.graph._elem_chunk_map
    monkeypatch.setattr(
        repo.retrieval.graph,
        "_elem_chunk_map",
        lambda nb: (calls.append(nb), original(nb))[1],
    )
    with repo._connect() as db:
        scoped = repo.retrieval.graph._elem_chunks_scoped(db, notebook.id, ["e1"])

    assert calls == [notebook.id]
    assert scoped["e1"] == ["c1", "c2"]


def test_backfilled_notebook_uses_the_point_lookup_and_agrees_with_legacy(
    repo, monkeypatch
):
    notebook = repo.create_notebook(NotebookCreate(name="r2"))
    source_id = _seed_source(repo, notebook.id)
    # Deliberately shuffled ids so "ORDER BY rowid" and "ORDER BY id" differ:
    # a point lookup that forgot its ORDER BY would come back in id order.
    _seed_chunks(
        repo,
        notebook.id,
        source_id,
        [("zz", ["e1"]), ("mm", ["e1", "e2"]), ("aa", ["e1"])],
    )
    baseline = _legacy_map(repo, notebook.id, ["e1", "e2"])

    maintenance = repo.maintenance
    maintenance.begin_chunk_element_backfill(notebook.id)
    _drain(maintenance, notebook.id)

    def _fail(_notebook_id):  # pragma: no cover - the assertion is that it never runs
        raise AssertionError("backfilled notebooks must not full-scan chunks")

    monkeypatch.setattr(repo.retrieval.graph, "_elem_chunk_map", _fail)
    with repo._connect() as db:
        scoped = repo.retrieval.graph._elem_chunks_scoped(db, notebook.id, ["e1", "e2"])

    assert scoped == baseline
    # Insertion order, not id order — the deterministic-order contract.
    assert scoped["e1"] == ["zz", "mm", "aa"]


def test_point_lookup_is_scoped_to_the_notebook(repo):
    first = repo.create_notebook(NotebookCreate(name="r3a"))
    second = repo.create_notebook(NotebookCreate(name="r3b"))
    _seed_chunks(repo, first.id, _seed_source(repo, first.id, "src-a"),
                 [("c1", ["shared"])])
    _seed_chunks(repo, second.id, _seed_source(repo, second.id, "src-b"),
                 [("c2", ["shared"])])
    for notebook_id in (first.id, second.id):
        repo.maintenance.begin_chunk_element_backfill(notebook_id)
        _drain(repo.maintenance, notebook_id)

    with repo._connect() as db:
        assert repo._runtime.chunk_store.chunks_for_element_ids(db, first.id, ["shared"]) and [
            row["chunk_id"]
            for row in repo._runtime.chunk_store.chunks_for_element_ids(db, first.id, ["shared"])
        ] == ["c1"]
        assert [
            row["chunk_id"]
            for row in repo._runtime.chunk_store.chunks_for_element_ids(db, second.id, ["shared"])
        ] == ["c2"]


def test_kg_source_chunks_agrees_across_the_fork(repo):
    """同一 fixture 下,回填前后 ``_kg_source_chunks`` 的输出逐位一致。"""
    notebook = repo.create_notebook(NotebookCreate(name="r4"))
    source_id = _seed_source(repo, notebook.id)
    _seed_chunks(
        repo,
        notebook.id,
        source_id,
        [("zz", ["e1"]), ("mm", ["e2"]), ("aa", ["e1"])],
    )
    now = "2026-08-10T00:00:00+00:00"
    evidence = json.dumps(
        [
            {"source_id": source_id, "source_title": "", "element_id": "e1",
             "element_type": "paragraph", "location_label": "p",
             "quoted_span": "x", "confidence": 1.0},
            {"source_id": source_id, "source_title": "", "element_id": "e2",
             "element_type": "paragraph", "location_label": "p",
             "quoted_span": "y", "confidence": 1.0},
        ]
    )
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,"
            "payload,evidence,source_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("o1", notebook.id, "concept", "approved", "",
             json.dumps({"name": "X"}), evidence, source_id, now, now),
        )

    legacy = [c.chunk_id for c in repo.retrieval.graph._kg_source_chunks(notebook.id, ["o1"])]

    repo.maintenance.begin_chunk_element_backfill(notebook.id)
    _drain(repo.maintenance, notebook.id)
    indexed = [c.chunk_id for c in repo.retrieval.graph._kg_source_chunks(notebook.id, ["o1"])]

    assert legacy == ["zz", "aa", "mm"]
    assert indexed == legacy


# ----------------------------------------------------- write-transaction guard


def _replace_source_chunks_ast(module_path: str):
    """``replace_source_chunks``'s AST node in one adapter.

    ``module_path`` is resolved against ``backend/``, NOT the process cwd: the
    G1 lane runs pytest from the repository root, where a cwd-relative
    ``app/...`` path does not exist — the guard would fail for the wrong
    reason (or, worse, a future ``try/except`` around it would pass for the
    wrong reason). Same idiom as ``test_kg_analysis_service.py``.
    """
    import ast
    import pathlib

    backend_root = pathlib.Path(__file__).resolve().parents[1]
    source = (backend_root / module_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    store = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ChunkStore"
    )
    return next(
        node
        for node in store.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "replace_source_chunks"
    )


def _calls_inside_write_block(function) -> set[str]:
    """Names called anywhere INSIDE a ``with ....write()`` block's body."""
    import ast

    found: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.With):
            continue
        opens_write = any(
            isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Attribute)
            and item.context_expr.func.attr == "write"
            for item in node.items
        )
        if not opens_write:
            continue
        for child in node.body:
            for inner in ast.walk(child):
                if isinstance(inner, ast.Call) and isinstance(
                    inner.func, ast.Attribute
                ):
                    found.add(inner.func.attr)
    return found


@pytest.mark.parametrize(
    "module_path",
    [
        "app/repositories/sqlite/chunk_store.py",
        "app/repositories/postgres/chunk_store.py",
    ],
)
def test_reverse_rows_are_written_inside_the_chunk_write_transaction(module_path):
    """反查行必须在**同一个写事务**里维护,两后端各扫一遍。

    纯行内容断言证明不了这条:把维护挪到事务外,行照样最终正确,但崩在两个事务
    之间就会留下「chunk 已提交、反查行未提交」的库——而读路径的标记还说它已回填。
    判据是 ``_insert_chunk_element_rows`` 落在 ``with ...write()`` 块**体内**;
    挪到块外照样报红。
    """
    function = _replace_source_chunks_ast(module_path)
    assert "_insert_chunk_element_rows" in _calls_inside_write_block(function)
