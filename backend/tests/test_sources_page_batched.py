"""C5: _source_from_row's 3 per-row lookups (source_elements COUNT,
extraction_runs latest error_message, knowledge_objects EXISTS) reworked to 3
TOTAL per page via _sources_from_rows — was 3*N round-trips (N+1) on
list_sources / list_sources_page. Output-equality oracle vs the old per-row
implementation, plus a query-count spy.
"""
import json

import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository, _now
from app.models.schemas import NotebookCreate
from tests.kg_extracted_parity_cases import (
    KG_EXTRACTED_CASE_IDS,
    KG_EXTRACTED_CASES,
    kg_case_run_id,
    kg_case_source_id,
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def _seed_source(repo, nb_id, src_id, title, created_at):
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,"
            "file_size,file_hash,summary,doc_type,parse_status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (src_id, nb_id, title, "document", f"{src_id}.md", f"/tmp/{src_id}.md",
             0, f"h-{src_id}", "", "", "extracted", created_at, now))


def _add_elements(repo, src_id, n):
    now = _now()
    with repo._write() as db:
        for i in range(n):
            db.execute(
                "INSERT INTO source_elements (id,source_id,element_type,location_label,"
                "text,metadata,created_at) VALUES (?,?,?,?,?,?,?)",
                (f"el-{src_id}-{i:03d}", src_id, "paragraph", f"p{i}", f"text {i}", "{}", now))


def _add_kg_object(repo, nb_id, src_id):
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,"
            "evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"ko-{src_id}", nb_id, "concept", "approved", "", json.dumps({"name": "x"}),
             "[]", src_id, now, now))


def _add_extraction_run(
    repo, nb_id, src_id, run_id, created_at, error_message="", status="completed"
):
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO extraction_runs (id,notebook_id,source_id,run_type,status,"
            "error_message,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, nb_id, src_id, "kg", status, error_message, created_at, now))


def _oracle_sources(repo, notebook_id):
    """Verbatim pre-fix computation: 3 separate per-row lookups via the
    original _source_from_row (still present, single-row path)."""
    with repo._connect() as db:
        rows = db.execute(
            "SELECT * FROM sources WHERE notebook_id = ? ORDER BY created_at ASC",
            (notebook_id,),
        ).fetchall()
        return [repo._source_from_row(db, row) for row in rows]


def test_list_sources_equals_per_row_oracle(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed_source(repo, nb.id, "s1", "Doc One", "2026-01-01T00:00:00")
    _seed_source(repo, nb.id, "s2", "Doc Two", "2026-01-02T00:00:00")
    _seed_source(repo, nb.id, "s3", "Doc Three (no elements/kg/runs)", "2026-01-03T00:00:00")
    _add_elements(repo, "s1", 5)
    _add_elements(repo, "s2", 0)
    _add_kg_object(repo, nb.id, "s1")
    _add_extraction_run(repo, nb.id, "s1", "r1", "2026-01-01T01:00:00", "windows_failed=1/2")
    _add_extraction_run(repo, nb.id, "s1", "r2", "2026-01-01T02:00:00", "")  # latest, clean

    got = repo.list_sources(nb.id)
    oracle = _oracle_sources(repo, nb.id)
    assert [s.model_dump() for s in got] == [s.model_dump() for s in oracle]
    # sanity: s1's latest run (r2, clean) wins -> no warning despite r1 having one
    s1 = next(s for s in got if s.id == "s1")
    assert s1.extraction_warning is None
    assert s1.element_count == 5
    assert s1.kg_extracted is True
    s3 = next(s for s in got if s.id == "s3")
    assert s3.element_count == 0
    assert s3.kg_extracted is False
    assert s3.extraction_warning is None


def test_list_sources_page_equals_per_row_oracle(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    for i in range(15):
        _seed_source(repo, nb.id, f"s{i}", f"Doc {i}", f"2026-01-{i + 1:02d}T00:00:00")
        if i % 3 == 0:
            _add_elements(repo, f"s{i}", i + 1)
        if i % 2 == 0:
            _add_kg_object(repo, nb.id, f"s{i}")

    page = repo.list_sources_page(nb.id, offset=0, limit=10)
    oracle = _oracle_sources(repo, nb.id)[:10]
    assert [s.model_dump() for s in page.items] == [s.model_dump() for s in oracle]
    assert page.total_count == 15


def test_extraction_warning_tie_break_matches_per_row(repo):
    """Two extraction_runs rows for the same source share created_at (tie) —
    the batched loader must pick the SAME winner as a per-row ORDER BY
    created_at DESC LIMIT 1 (both driven by the same index scan order)."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed_source(repo, nb.id, "s1", "Doc", "2026-01-01T00:00:00")
    _add_extraction_run(repo, nb.id, "s1", "r1", "2026-01-01T05:00:00", "windows_failed=1/1")
    _add_extraction_run(repo, nb.id, "s1", "r2", "2026-01-01T05:00:00", "")  # same created_at

    with repo._connect() as db:
        per_row_winner = db.execute(
            "SELECT id FROM extraction_runs WHERE source_id=? "
            "ORDER BY created_at DESC LIMIT 1", ("s1",)).fetchone()["id"]

    got = repo.list_sources(nb.id)
    s1 = next(s for s in got if s.id == "s1")
    with repo._connect() as db:
        oracle_warning = repo._extraction_warning(db, "s1")
    assert s1.extraction_warning == oracle_warning, (
        f"batched tie-break diverged from per-row oracle (per_row_winner={per_row_winner})")


def test_sources_from_rows_empty_list_short_circuits(repo):
    with repo._connect() as db:
        assert repo._sources_from_rows(db, []) == []


# --------------------------------------------------------------------------
# kg_extracted 判定矩阵(与 PG 侧共用 tests/kg_extracted_parity_cases.py)
# --------------------------------------------------------------------------

def _seed_kg_matrix(repo, notebook_id):
    """把整张判定矩阵种进**同一个** notebook:每条用例一条来源。

    刻意同页,因为批量路径的判定是整页一条 SQL——十条用例同页才真正跑到「一页
    多个 id」那条形状;一条用例一个 notebook 只会反复验证单元素的退化情形。"""
    for index, (_label, has_object, runs, _expected) in enumerate(KG_EXTRACTED_CASES):
        source_id = kg_case_source_id(index)
        _seed_source(
            repo, notebook_id, source_id, f"Doc {index}",
            f"2026-03-{index + 1:02d}T00:00:00",
        )
        _add_elements(repo, source_id, 1)
        if has_object:
            _add_kg_object(repo, notebook_id, source_id)
        for rank, status, error_message in runs:
            _add_extraction_run(
                repo, notebook_id, source_id, kg_case_run_id(index, rank),
                f"2026-04-{rank + 1:02d}T00:00:00",
                error_message=error_message, status=status,
            )


def _expected_kg_matrix():
    return {label: expected for label, _obj, _runs, expected in KG_EXTRACTED_CASES}


def test_batched_kg_extracted_matches_the_parity_matrix(repo):
    """批量路径(list_sources / list_sources_page)的每一条判定分支。

    有对象 + 最近一次跑完 + 消息干净 → True;失败窗口、未补齐的 partial 重试、
    非 completed 的最近一次、没有对象行 → False;完全没有抽取记录按 'completed'
    兜底 → True;多条 run 只看最近一次(两个方向)。"""
    nb = repo.create_notebook(NotebookCreate(name="kg matrix"))
    _seed_kg_matrix(repo, nb.id)

    by_id = {item.id: item for item in repo.list_sources(nb.id)}
    got = {
        label: by_id[kg_case_source_id(index)].kg_extracted
        for index, (label, _obj, _runs, _expected) in enumerate(KG_EXTRACTED_CASES)
    }
    assert got == _expected_kg_matrix()

    # 生产上真正的热点是分页那条路径,它与 list_sources 共用同一个批量水合。
    page = repo.list_sources_page(nb.id, offset=0, limit=50)
    paged = {item.id: item.kg_extracted for item in page.items}
    assert paged == {
        kg_case_source_id(index): expected
        for index, (_label, _obj, _runs, expected) in enumerate(KG_EXTRACTED_CASES)
    }


def test_single_row_kg_extracted_matches_the_parity_matrix(repo):
    """单条路径(get_source)必须给出与批量路径逐条相同的答案——两处各有一份取数
    SQL,漂了会让同一份来源在列表和详情里显示成两种状态。"""
    nb = repo.create_notebook(NotebookCreate(name="kg matrix"))
    _seed_kg_matrix(repo, nb.id)

    got = {
        label: repo.get_source(kg_case_source_id(index)).kg_extracted
        for index, (label, _obj, _runs, _expected) in enumerate(KG_EXTRACTED_CASES)
    }
    assert got == _expected_kg_matrix()
