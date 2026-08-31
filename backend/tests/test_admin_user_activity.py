"""list_user_activity — admin 只读用户活动流(P1 仓储层)。

三类(ask_jobs / sources / reports)各自 keyset 分页、在 Python 内按
(created_at DESC, id DESC) 归并，见
docs/superpowers/specs/2026-08-04-user-activity-log-view-design_zh.md §4.1。
"""
import json

import pytest
from app.core.activity_time import parse_activity_instant
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository

from tests.activity_parity_cases import (
    ACTIVITY_ROWS,
    BOUND_PARITY_CASES,
    LOCAL_DAY_EXPECTED,
    LOCAL_DAY_ROWS,
    LOCAL_DAY_SINCE,
    LOCAL_DAY_UNTIL,
    MALFORMED_BOUNDS,
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    return SQLiteRepository(Settings())


def test_user_activity_retention_setting_is_bounded(monkeypatch):
    monkeypatch.setenv("USER_ACTIVITY_RETENTION_DAYS", "30")
    assert Settings().user_activity_retention_days == 30
    for invalid in ("0", "3651"):
        monkeypatch.setenv("USER_ACTIVITY_RETENTION_DAYS", invalid)
        with pytest.raises(ValueError):
            Settings()


def _insert_user(db, user_id: str, username: str) -> None:
    db.execute(
        "INSERT INTO users (id,email,display_name,role,status,username,created_at,updated_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (user_id, f"{user_id}@x", user_id.upper(), "user", "active", username,
         "2026-08-01T00:00:00", "2026-08-01T00:00:00"),
    )


def _insert_notebook(db, nid: str, owner: str, status: str = "ready") -> None:
    db.execute(
        "INSERT INTO notebooks (id,name,created_by,status,created_at,updated_at)"
        " VALUES (?,?,?,?,?,?)",
        (nid, f"NB-{nid}", owner, status, "2026-08-01T00:00:00", "2026-08-01T00:00:00"),
    )


def _insert_member(db, notebook_id: str, user_id: str) -> None:
    db.execute(
        "INSERT INTO notebook_members (notebook_id,user_id,role,added_at) "
        "VALUES (?,?,?,?)",
        (notebook_id, user_id, "viewer", "2026-08-01T00:00:00"),
    )


def _insert_ask(db, job_id, notebook_id, created_by, created_at, *,
                 question="q?", mode="chunk", status="completed", asked_at="",
                 answer_id="", error="") -> None:
    db.execute(
        "INSERT INTO ask_jobs "
        "(id,notebook_id,created_by,mode,question,status,asked_at,answer_id,error,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (job_id, notebook_id, created_by, mode, question, status, asked_at,
         answer_id, error, created_at, created_at),
    )


def _insert_source(db, source_id, notebook_id, created_at, *,
                    title="Doc", source_type="pdf", status="parsed",
                    parse_status="parsed", file_name="doc.pdf",
                    error_message="") -> None:
    db.execute(
        "INSERT INTO sources "
        "(id,notebook_id,title,source_type,status,parse_status,file_name,"
        "error_message,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (source_id, notebook_id, title, source_type, status, parse_status,
         file_name, error_message, created_at, created_at),
    )


def _insert_paper_meta(db, source_id, notebook_id, paper_title, *, is_paper=1) -> None:
    db.execute(
        "INSERT INTO source_paper_meta "
        "(source_id,notebook_id,is_paper,paper_title,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (source_id, notebook_id, is_paper, paper_title,
         "2026-08-01T00:00:00", "2026-08-01T00:00:00"),
    )


def _insert_extraction_run(db, run_id, notebook_id, source_id, error_message,
                            created_at="2026-08-01T00:00:00") -> None:
    db.execute(
        "INSERT INTO extraction_runs "
        "(id,notebook_id,source_id,run_type,status,error_message,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (run_id, notebook_id, source_id, "kg", "completed", error_message,
         created_at, created_at),
    )


def _insert_report(db, report_id, notebook_id, created_by, created_at, *,
                    question="report?", depth=2, status="done",
                    generation_started_at=None) -> None:
    understanding = (
        json.dumps({"_generation_started_at": generation_started_at})
        if generation_started_at is not None else "{}"
    )
    db.execute(
        "INSERT INTO reports "
        "(id,notebook_id,question,depth,status,created_by,understanding_json,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (report_id, notebook_id, question, depth, status, created_by,
         understanding, created_at, created_at),
    )


def test_merges_three_types_in_time_descending_order(repo):
    with repo._write() as db:
        _insert_user(db, "u1", "a00000001")
        _insert_notebook(db, "n1", "u1")
        _insert_ask(db, "ask-1", "n1", "u1", "2026-08-01T10:00:00")
        _insert_source(db, "src-1", "n1", "2026-08-01T11:00:00")
        _insert_report(db, "rep-1", "n1", "u1", "2026-08-01T12:00:00")
        _insert_ask(db, "ask-2", "n1", "u1", "2026-08-01T09:00:00")

    result = repo.list_user_activity("u1", limit=50)
    assert [item["id"] for item in result["items"]] == [
        "rep-1", "src-1", "ask-1", "ask-2",
    ]
    assert [item["type"] for item in result["items"]] == [
        "report", "source", "ask", "ask",
    ]
    assert result["has_more"] is False
    assert result["next_cursor"] is None


def test_activity_type_filter_applies_before_limit_and_paginates_all_questions(repo):
    """提问概览不能先取混合页再过滤：较新的来源/报告不应挤掉问题。"""
    with repo._write() as db:
        _insert_user(db, "u1", "a00000001")
        _insert_user(db, "u2", "b00000002")
        _insert_notebook(db, "n1", "u1")
        _insert_notebook(db, "n2", "u2")
        _insert_member(db, "n2", "u1")
        _insert_source(db, "src-new", "n1", "2026-08-01T13:00:00")
        _insert_report(db, "rep-new", "n1", "u1", "2026-08-01T12:00:00")
        _insert_ask(
            db, "ask-2", "n1", "u1", "2026-08-01T11:00:00",
            question="第二个关注点",
        )
        _insert_ask(
            db, "ask-1", "n2", "u1", "2026-08-01T10:00:00",
            question="第一个关注点",
        )

    first = repo.list_user_activity("u1", activity_type="ask", limit=1)
    assert [item["id"] for item in first["items"]] == ["ask-2"]
    assert first["has_more"] is True
    assert first["next_cursor"] is not None

    second = repo.list_user_activity(
        "u1",
        activity_type="ask",
        before_ts=first["next_cursor"]["ts"],
        before_id=first["next_cursor"]["id"],
        limit=1,
    )
    assert [item["id"] for item in second["items"]] == ["ask-1"]
    assert second["has_more"] is False


def test_question_overview_enforces_live_read_access_unless_admin_audit(repo):
    with repo._write() as db:
        _insert_user(db, "u1", "a00000001")
        _insert_user(db, "u2", "b00000002")
        _insert_notebook(db, "n1", "u1")
        _insert_notebook(db, "n2", "u2")
        _insert_ask(db, "ask-mine", "n1", "u1", "2026-08-01T11:00:00")
        _insert_ask(db, "ask-revoked", "n2", "u1", "2026-08-01T10:00:00")

    self_view = repo.list_user_activity("u1", activity_type="ask")
    assert [item["id"] for item in self_view["items"]] == ["ask-mine"]

    admin_view = repo.list_user_activity(
        "u1", activity_type="ask", include_inaccessible_questions=True
    )
    assert [item["id"] for item in admin_view["items"]] == [
        "ask-mine", "ask-revoked",
    ]

    with repo._write() as db:
        _insert_member(db, "n2", "u1")
    shared_view = repo.list_user_activity("u1", activity_type="ask")
    assert [item["id"] for item in shared_view["items"]] == [
        "ask-mine", "ask-revoked",
    ]

    with repo._write() as db:
        db.execute(
            "DELETE FROM notebook_members WHERE notebook_id=? AND user_id=?",
            ("n2", "u1"),
        )
    revoked_view = repo.list_user_activity("u1", activity_type="ask")
    assert [item["id"] for item in revoked_view["items"]] == ["ask-mine"]


def test_creator_wide_question_overview_uses_activity_keyset_index(repo):
    """The unscoped ask-only path must not regress to a global scan + temp sort."""
    with repo._connect() as db:
        plan = db.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT id FROM ask_jobs WHERE created_by = ? "
            "ORDER BY COALESCE(julianday(created_at), "
            "julianday('0001-01-01T00:00:00+00:00')) DESC, id DESC LIMIT ?",
            ("u1", 51),
        ).fetchall()

    details = "\n".join(str(row["detail"]) for row in plan)
    assert "idx_ask_jobs_creator_activity" in details
    assert "USE TEMP B-TREE" not in details


def test_ask_field_shape_and_ordering_uses_created_at_not_asked_at(repo):
    with repo._write() as db:
        _insert_user(db, "u1", "a00000001")
        _insert_notebook(db, "n1", "u1")
        # asked_at (browser submit time) intentionally out of step with
        # created_at — sort must follow created_at, asked_at is a pass-through
        # display field, not a sort key.
        _insert_ask(
            db, "ask-1", "n1", "u1", "2026-08-01T10:00:00",
            question="first?", mode="reasoning", status="running",
            asked_at="2026-08-01T09:55:00", answer_id="", error="",
        )
        _insert_ask(
            db, "ask-2", "n1", "u1", "2026-08-01T11:00:00",
            question="second?", mode="chunk", status="failed",
            asked_at="", answer_id="ans-2", error="boom",
        )

    result = repo.list_user_activity("u1", limit=50)
    items = result["items"]
    assert [item["id"] for item in items] == ["ask-2", "ask-1"]
    first = items[0]
    assert first["type"] == "ask"
    assert first["notebook_id"] == "n1"
    assert first["question"] == "second?"
    assert first["mode"] == "chunk"
    assert first["status"] == "failed"
    assert first["answer_id"] == "ans-2"
    assert first["error"] == "boom"
    assert first["asked_at"] == ""
    second = items[1]
    assert second["asked_at"] == "2026-08-01T09:55:00"


def test_source_hides_memory_and_knowhow_and_carries_paper_meta(repo):
    with repo._write() as db:
        _insert_user(db, "u1", "a00000001")
        _insert_notebook(db, "n1", "u1")
        _insert_source(db, "src-visible", "n1", "2026-08-01T10:00:00",
                        title="Paper A", source_type="pdf")
        _insert_paper_meta(db, "src-visible", "n1", "The Real Paper Title")
        _insert_source(db, "src-plain", "n1", "2026-08-01T09:00:00",
                        title="Plain Doc", source_type="md")
        # Hidden synthetic sources — must never surface in the activity feed.
        _insert_source(db, "src-memory", "n1", "2026-08-01T12:00:00",
                        title="Memory projection", source_type="memory")
        _insert_source(db, "src-knowhow", "n1", "2026-08-01T13:00:00",
                        title="Knowhow projection", source_type="knowhow")

    result = repo.list_user_activity("u1", limit=50)
    ids = [item["id"] for item in result["items"]]
    assert "src-memory" not in ids
    assert "src-knowhow" not in ids
    assert ids == ["src-visible", "src-plain"]

    by_id = {item["id"]: item for item in result["items"]}
    visible = by_id["src-visible"]
    assert visible["is_paper"] is True
    assert visible["paper_title"] == "The Real Paper Title"
    assert visible["title"] == "Paper A"
    assert visible["source_type"] == "pdf"

    plain = by_id["src-plain"]
    # LEFT JOIN miss must not error and must default sanely.
    assert plain["is_paper"] is False
    assert plain["paper_title"] == ""


def test_sources_scoped_to_owned_notebooks_only(repo):
    """sources 没有 created_by 列，只能靠该用户自己的笔记本 id 限定范围——与
    list_user_notebooks 完全同口径。"""
    with repo._write() as db:
        _insert_user(db, "u1", "a00000001")
        _insert_user(db, "u2", "b00000002")
        _insert_notebook(db, "n1", "u1")
        _insert_notebook(db, "n2", "u2")
        _insert_source(db, "src-mine", "n1", "2026-08-01T10:00:00")
        _insert_source(db, "src-other", "n2", "2026-08-01T11:00:00")

    result = repo.list_user_activity("u1", limit=50)
    ids = [item["id"] for item in result["items"]]
    assert ids == ["src-mine"]


def test_ask_and_reports_are_owner_only_like_sources(repo):
    """三类**统一**按该用户自有的笔记本收窄(owner-only)。

    共享库里该用户自己提的问 / 生成的报告不进这份清单:设计稿 §5 与 `docs/product-and-api.md`
    红线的口径是「用量合计包含共享库提交，但展开清单刻意沿用 owner-only」，而活动
    流就是展开清单。此前 ask/report 只按 created_by 过滤，于是同一次会话里出现两种
    口径——不带 notebook_id 时吐出别人库里的行，客户端按那个 notebook_id 收窄却又
    因归属校验短路成空。
    """
    with repo._write() as db:
        _insert_user(db, "u1", "a00000001")
        _insert_user(db, "u2", "b00000002")
        _insert_notebook(db, "n1", "u1")
        _insert_notebook(db, "n2", "u2")  # owned by someone else
        _insert_ask(db, "ask-shared", "n2", "u1", "2026-08-01T10:00:00")
        _insert_report(db, "rep-shared", "n2", "u1", "2026-08-01T11:00:00")
        # Same user's own-notebook rows — these must still be there, otherwise
        # the assertion below would pass on a trivially broken implementation.
        _insert_ask(db, "ask-mine", "n1", "u1", "2026-08-01T10:30:00")
        _insert_report(db, "rep-mine", "n1", "u1", "2026-08-01T11:30:00")

    result = repo.list_user_activity("u1", limit=50)
    ids = {item["id"] for item in result["items"]}
    assert ids == {"ask-mine", "rep-mine"}

    # Narrowing to a notebook the caller does not own stays empty — same answer
    # as the unscoped call now gives for those rows, not a second opinion.
    scoped = repo.list_user_activity("u1", notebook_id="n2", limit=50)
    assert scoped == {"items": [], "has_more": False, "next_cursor": None}


def test_other_users_notebooks_asks_and_reports_are_excluded(repo):
    with repo._write() as db:
        _insert_user(db, "u1", "a00000001")
        _insert_user(db, "u2", "b00000002")
        _insert_notebook(db, "n1", "u1")
        _insert_notebook(db, "n2", "u2")
        _insert_ask(db, "ask-u1", "n1", "u1", "2026-08-01T10:00:00")
        _insert_ask(db, "ask-u2", "n2", "u2", "2026-08-01T10:00:00")
        _insert_report(db, "rep-u1", "n1", "u1", "2026-08-01T11:00:00")
        _insert_report(db, "rep-u2", "n2", "u2", "2026-08-01T11:00:00")
        _insert_source(db, "src-u1", "n1", "2026-08-01T12:00:00")
        _insert_source(db, "src-u2", "n2", "2026-08-01T12:00:00")

    result = repo.list_user_activity("u1", limit=50)
    ids = {item["id"] for item in result["items"]}
    assert ids == {"ask-u1", "rep-u1", "src-u1"}


def test_explicit_notebook_id_not_owned_returns_empty_without_error(repo):
    with repo._write() as db:
        _insert_user(db, "u1", "a00000001")
        _insert_user(db, "u2", "b00000002")
        _insert_notebook(db, "n1", "u1")
        _insert_notebook(db, "n2", "u2")
        _insert_ask(db, "ask-u2", "n2", "u2", "2026-08-01T10:00:00")

    result = repo.list_user_activity("u1", notebook_id="n2", limit=50)
    assert result == {"items": [], "has_more": False, "next_cursor": None}

    result_missing = repo.list_user_activity("u1", notebook_id="does-not-exist", limit=50)
    assert result_missing == {"items": [], "has_more": False, "next_cursor": None}


def test_composite_cursor_ties_across_types_no_skip_or_duplicate(repo):
    """并列 created_at 下的复合游标必须靠 id 做字典序 tie-break,且要在三类混合
    归并里保持全局一致——这是最容易写错的一条(排序键只比 ts 会跳行)。"""
    same_ts = "2026-08-01T10:00:00"
    with repo._write() as db:
        _insert_user(db, "u1", "a00000001")
        _insert_notebook(db, "n1", "u1")
        for ask_id in ("ask-1", "ask-2"):
            _insert_ask(db, ask_id, "n1", "u1", same_ts)
        for src_id in ("src-1", "src-2"):
            _insert_source(db, src_id, "n1", same_ts)
        for rep_id in ("rep-1", "rep-2"):
            _insert_report(db, rep_id, "n1", "u1", same_ts)

    # Full order, descending by (created_at, id): id string order dominates
    # since every created_at ties — 's' > 'r' > 'a' in ASCII.
    expected_order = ["src-2", "src-1", "rep-2", "rep-1", "ask-2", "ask-1"]

    seen: list[str] = []
    cursor_ts = None
    cursor_id = None
    pages = 0
    while True:
        pages += 1
        assert pages <= 10, "pagination did not terminate"
        page = repo.list_user_activity(
            "u1", before_ts=cursor_ts, before_id=cursor_id, limit=2,
        )
        seen.extend(item["id"] for item in page["items"])
        if not page["has_more"]:
            assert page["next_cursor"] is None
            break
        assert page["next_cursor"] is not None
        cursor_ts = page["next_cursor"]["ts"]
        cursor_id = page["next_cursor"]["id"]

    assert seen == expected_order
    assert len(seen) == len(set(seen)), "duplicate item across pages"


def test_has_more_and_next_cursor_single_page(repo):
    with repo._write() as db:
        _insert_user(db, "u1", "a00000001")
        _insert_notebook(db, "n1", "u1")
        _insert_ask(db, "ask-1", "n1", "u1", "2026-08-01T10:00:00")
        _insert_ask(db, "ask-2", "n1", "u1", "2026-08-01T09:00:00")

    result = repo.list_user_activity("u1", limit=2)
    assert result["has_more"] is False
    assert result["next_cursor"] is None
    assert len(result["items"]) == 2

    result_small = repo.list_user_activity("u1", limit=1)
    assert result_small["has_more"] is True
    assert result_small["next_cursor"] == {"ts": "2026-08-01T10:00:00", "id": "ask-1"}
    assert len(result_small["items"]) == 1


def test_limit_clamped_to_bounds(repo):
    with repo._write() as db:
        _insert_user(db, "u1", "a00000001")
        _insert_notebook(db, "n1", "u1")
        for i in range(3):
            _insert_ask(db, f"ask-{i}", "n1", "u1", f"2026-08-01T1{i}:00:00")

    # limit below 1 clamps to 1.
    tiny = repo.list_user_activity("u1", limit=0)
    assert len(tiny["items"]) == 1

    # limit above 200 with fewer rows than the cap: no error, no truncation
    # below the actual row count.
    big = repo.list_user_activity("u1", limit=10_000)
    assert len(big["items"]) == 3
    assert big["has_more"] is False


def test_limit_upper_bound_actually_caps_the_page(repo):
    """上限必须有**多于 200 行**的数据才证明得了。

    只有 3 行时 `min(200, limit)` 删掉照样绿——那条断言证明的是「没炸」，不是
    「封了顶」。这里插 250 行，页长必须正好 200。
    """
    with repo._write() as db:
        _insert_user(db, "u1", "a00000001")
        _insert_notebook(db, "n1", "u1")
        for i in range(250):
            _insert_ask(
                db, f"ask-{i:03d}", "n1", "u1",
                f"2026-08-01T{i // 60:02d}:{i % 60:02d}:00",
            )

    capped = repo.list_user_activity("u1", limit=10_000)
    assert len(capped["items"]) == 200
    assert capped["has_more"] is True
    assert capped["next_cursor"] is not None


def test_empty_scope_for_unknown_user(repo):
    with repo._write() as db:
        _insert_user(db, "u1", "a00000001")
        _insert_notebook(db, "n1", "u1")
        _insert_ask(db, "ask-1", "n1", "u1", "2026-08-01T10:00:00")

    assert repo.list_user_activity("nobody", limit=50) == {
        "items": [], "has_more": False, "next_cursor": None,
    }


def test_source_extraction_and_parse_quality_warnings(repo):
    with repo._write() as db:
        _insert_user(db, "u1", "a00000001")
        _insert_notebook(db, "n1", "u1")
        # Degraded KG extraction — windows_failed marker on the latest run.
        _insert_source(db, "src-degraded", "n1", "2026-08-01T10:00:00")
        _insert_extraction_run(db, "run-1", "n1", "src-degraded",
                                "windows_failed=2/5")
        # Clean extraction — no warning even though a run row exists.
        _insert_source(db, "src-clean", "n1", "2026-08-01T09:00:00")
        _insert_extraction_run(db, "run-2", "n1", "src-clean", "")
        # No extraction_runs row at all.
        _insert_source(db, "src-none", "n1", "2026-08-01T08:00:00")
        # MinerU-degraded-to-local-PyMuPDF4LLM fallback marker on error_message.
        _insert_source(db, "src-fallback", "n1", "2026-08-01T07:00:00",
                        error_message="[pdf-python-fallback] mineru retries exhausted")

    result = repo.list_user_activity("u1", limit=50)
    by_id = {item["id"]: item for item in result["items"]}

    degraded = by_id["src-degraded"]
    assert degraded["extraction_warning"] == (
        "部分内容因网络问题未完成分析（2/5 段失败），建议重新上传或重试。"
    )
    assert degraded["parse_quality_warning"] is False

    clean = by_id["src-clean"]
    assert clean["extraction_warning"] is None
    assert clean["parse_quality_warning"] is False

    none_run = by_id["src-none"]
    assert none_run["extraction_warning"] is None

    fallback = by_id["src-fallback"]
    assert fallback["parse_quality_warning"] is True
    assert fallback["extraction_warning"] is None


def test_source_paper_meta_status_four_states(repo):
    with repo._write() as db:
        _insert_user(db, "u1", "a00000001")
        _insert_notebook(db, "n1", "u1")
        # has_meta: source_paper_meta row with is_paper=1.
        _insert_source(db, "src-has-meta", "n1", "2026-08-01T10:00:00")
        _insert_paper_meta(db, "src-has-meta", "n1", "A Paper Title", is_paper=1)
        # not_paper: source_paper_meta row exists but is_paper=0 (marker row).
        _insert_source(db, "src-not-paper", "n1", "2026-08-01T09:00:00")
        _insert_paper_meta(db, "src-not-paper", "n1", None, is_paper=0)
        # missing: parsed, eligible doc_type, no meta row yet.
        _insert_source(db, "src-missing", "n1", "2026-08-01T08:00:00",
                        parse_status="parsed")
        # None: not applicable — still queued (not parsed yet).
        _insert_source(db, "src-not-applicable", "n1", "2026-08-01T07:00:00",
                        parse_status="queued")

    result = repo.list_user_activity("u1", limit=50)
    by_id = {item["id"]: item for item in result["items"]}
    assert by_id["src-has-meta"]["paper_meta_status"] == "has_meta"
    assert by_id["src-not-paper"]["paper_meta_status"] == "not_paper"
    assert by_id["src-missing"]["paper_meta_status"] == "missing"
    assert by_id["src-not-applicable"]["paper_meta_status"] is None


def test_report_generation_started_at_present_and_missing(repo):
    with repo._write() as db:
        _insert_user(db, "u1", "a00000001")
        _insert_notebook(db, "n1", "u1")
        _insert_report(db, "rep-with-start", "n1", "u1", "2026-08-01T10:00:00",
                        generation_started_at="2026-08-01T10:05:00")
        _insert_report(db, "rep-without-start", "n1", "u1", "2026-08-01T09:00:00")

    result = repo.list_user_activity("u1", limit=50)
    by_id = {item["id"]: item for item in result["items"]}
    assert by_id["rep-with-start"]["generation_started_at"] == "2026-08-01T10:05:00"
    # Old report missing the timestamp must return an empty string — never a
    # fabricated value and never a fallback to created_at/updated_at.
    assert by_id["rep-without-start"]["generation_started_at"] == ""


def test_since_and_until_half_open_interval(repo):
    with repo._write() as db:
        _insert_user(db, "u1", "a00000001")
        _insert_notebook(db, "n1", "u1")
        _insert_ask(db, "ask-before", "n1", "u1", "2026-08-01T09:59:59")
        _insert_ask(db, "ask-at-since", "n1", "u1", "2026-08-01T10:00:00")
        _insert_ask(db, "ask-inside", "n1", "u1", "2026-08-01T15:00:00")
        _insert_ask(db, "ask-at-until", "n1", "u1", "2026-08-02T00:00:00")
        _insert_ask(db, "ask-after", "n1", "u1", "2026-08-02T00:00:01")

    # since alone.
    since_only = repo.list_user_activity(
        "u1", since="2026-08-01T10:00:00", limit=50,
    )
    ids = {item["id"] for item in since_only["items"]}
    assert ids == {"ask-at-since", "ask-inside", "ask-at-until", "ask-after"}

    # until alone.
    until_only = repo.list_user_activity(
        "u1", until="2026-08-02T00:00:00", limit=50,
    )
    ids = {item["id"] for item in until_only["items"]}
    assert ids == {"ask-before", "ask-at-since", "ask-inside"}

    # since + until together — half-open [since, until): the "since" boundary
    # row is INCLUDED, the "until" boundary row is EXCLUDED. This is the
    # easiest edge to get backwards.
    day = repo.list_user_activity(
        "u1", since="2026-08-01T10:00:00", until="2026-08-02T00:00:00", limit=50,
    )
    ids = {item["id"] for item in day["items"]}
    assert ids == {"ask-at-since", "ask-inside"}
    assert "ask-at-until" not in ids
    assert "ask-before" not in ids
    assert "ask-after" not in ids


def test_since_until_combined_with_cursor_pagination(repo):
    """范围与复合游标同时生效时,翻页仍不跳行不重复。"""
    with repo._write() as db:
        _insert_user(db, "u1", "a00000001")
        _insert_notebook(db, "n1", "u1")
        # Two rows outside the [since, until) window that must never appear.
        _insert_ask(db, "ask-outside-early", "n1", "u1", "2026-08-01T00:00:00")
        _insert_ask(db, "ask-outside-late", "n1", "u1", "2026-08-03T00:00:00")
        for i in range(4):
            _insert_ask(db, f"ask-in-{i}", "n1", "u1", f"2026-08-01T1{i}:00:00")

    since = "2026-08-01T10:00:00"
    until = "2026-08-02T00:00:00"
    seen: list[str] = []
    cursor_ts = None
    cursor_id = None
    pages = 0
    while True:
        pages += 1
        assert pages <= 10
        page = repo.list_user_activity(
            "u1", since=since, until=until,
            before_ts=cursor_ts, before_id=cursor_id, limit=2,
        )
        seen.extend(item["id"] for item in page["items"])
        if not page["has_more"]:
            break
        cursor_ts = page["next_cursor"]["ts"]
        cursor_id = page["next_cursor"]["id"]

    assert seen == ["ask-in-3", "ask-in-2", "ask-in-1", "ask-in-0"]
    assert len(seen) == len(set(seen))
    assert "ask-outside-early" not in seen
    assert "ask-outside-late" not in seen


# --------------------------------------------- 绝对时刻排序(混合 UTC offset)

# 同一天的三行,**文本序与真实时刻序恰好相反**。seams.now 现在写的是带本机 offset
# 的串,而历史行是裸 naive,所以这几张表里的 created_at 必然是混合格式。
_MIXED_OFFSET_ROWS = (
    # (id, created_at 原文, 绝对时刻)
    ("ask-plus8", "2026-08-04T10:00:00+08:00"),   # 02:00Z —— 文本最大、时刻最早
    ("ask-naive", "2026-08-04T05:00:00"),         # 05:00Z —— 文本居中、时刻最晚
    ("ask-utc", "2026-08-04T03:30:00+00:00"),     # 03:30Z —— 文本最小、时刻居中
)
_MIXED_OFFSET_ORDER = ["ask-naive", "ask-utc", "ask-plus8"]


def test_mixed_utc_offsets_sort_by_absolute_instant_not_text(repo):
    """裸文本比较会把 `+08:00` 的 10:00(=02:00Z)排在 `+00:00` 的 03:30 之前。

    顺序反了不只是显示问题:keyset 游标按这个错序推进,翻页会跳行。
    """
    with repo._write() as db:
        _insert_user(db, "u1", "a00000001")
        _insert_notebook(db, "n1", "u1")
        for ask_id, created_at in _MIXED_OFFSET_ROWS:
            _insert_ask(db, ask_id, "n1", "u1", created_at)

    result = repo.list_user_activity("u1", limit=50)
    assert [item["id"] for item in result["items"]] == _MIXED_OFFSET_ORDER
    # 纯文本降序会给出 plus8 / naive / utc —— 断言必须真的把它排除掉。
    assert [item["id"] for item in result["items"]] != [
        "ask-plus8", "ask-naive", "ask-utc",
    ]


def test_mixed_utc_offsets_paginate_without_skipping(repo):
    """游标 ts 是行**原样**的 created_at(可能是裸 naive、也可能带 offset)。

    下一页的并列判据必须仍能精确命中它自己那一行,否则混合格式下每翻一页都会漏行。
    """
    with repo._write() as db:
        _insert_user(db, "u1", "a00000001")
        _insert_notebook(db, "n1", "u1")
        for ask_id, created_at in _MIXED_OFFSET_ROWS:
            _insert_ask(db, ask_id, "n1", "u1", created_at)

    seen: list[str] = []
    cursor_ts = None
    cursor_id = None
    pages = 0
    while True:
        pages += 1
        assert pages <= 10, "pagination did not terminate"
        page = repo.list_user_activity(
            "u1", before_ts=cursor_ts, before_id=cursor_id, limit=1,
        )
        seen.extend(item["id"] for item in page["items"])
        if not page["has_more"]:
            break
        cursor_ts = page["next_cursor"]["ts"]
        cursor_id = page["next_cursor"]["id"]

    assert seen == _MIXED_OFFSET_ORDER
    assert len(seen) == len(set(seen))


def test_since_until_compare_absolute_instants_across_offsets(repo):
    """范围边界同样按绝对时刻比,不按文本。"""
    with repo._write() as db:
        _insert_user(db, "u1", "a00000001")
        _insert_notebook(db, "n1", "u1")
        for ask_id, created_at in _MIXED_OFFSET_ROWS:
            _insert_ask(db, ask_id, "n1", "u1", created_at)

    # [03:00Z, 04:00Z) 只框住 03:30Z 那一行 —— 文本比较会框成别的集合。
    window = repo.list_user_activity(
        "u1", since="2026-08-04T03:00:00+00:00",
        until="2026-08-04T04:00:00+00:00", limit=50,
    )
    assert [item["id"] for item in window["items"]] == ["ask-utc"]


# ------------------------------------------------- 时间边界:双后端共用用例表

def _seed_parity_rows(repo) -> None:
    with repo._write() as db:
        _insert_user(db, "u1", "a00000001")
        _insert_notebook(db, "n1", "u1")
        for ask_id, created_at in ACTIVITY_ROWS:
            _insert_ask(db, ask_id, "n1", "u1", created_at)


@pytest.mark.parametrize("since,until,expected", BOUND_PARITY_CASES)
def test_bound_spellings_select_the_same_rows(repo, since, until, expected):
    """同一时刻的不同写法必须选出同一批行(PostgreSQL 侧跑同一张表)。"""
    _seed_parity_rows(repo)
    result = repo.list_user_activity("u1", since=since, until=until, limit=50)
    assert {item["id"] for item in result["items"]} == expected


def test_browser_local_day_window_selects_that_local_day(repo):
    """选「浏览器本地日」要真的选中那一天的行(F1)。

    BOUND_PARITY_CASES 全是同一时刻的不同写法,证明不了日边界落在哪。这里用前端
    dayRange() 实际产出的那对带偏移边界,断言窗口两端都对:当地 00:00 在窗口内
    (闭端)、次日当地 00:00 在窗口外(开端)、前一天当地 23:59:59 在窗口外。
    """
    with repo._write() as db:
        _insert_user(db, "u1", "a00000001")
        _insert_notebook(db, "n1", "u1")
        for ask_id, created_at in LOCAL_DAY_ROWS:
            _insert_ask(db, ask_id, "n1", "u1", created_at)

    result = repo.list_user_activity(
        "u1", since=LOCAL_DAY_SINCE, until=LOCAL_DAY_UNTIL, limit=50,
    )
    assert {item["id"] for item in result["items"]} == LOCAL_DAY_EXPECTED


@pytest.mark.parametrize("bad", MALFORMED_BOUNDS)
@pytest.mark.parametrize("field", ["since", "until"])
def test_malformed_range_bound_raises_value_error(repo, bad, field):
    """畸形边界抛 ValueError,不静默忽略——静默会返回比请求更宽的窗口。"""
    _seed_parity_rows(repo)
    with pytest.raises(ValueError):
        repo.list_user_activity("u1", **{field: bad}, limit=50)


def test_cursor_round_trips_for_unparseable_created_at(repo):
    """游标只能发**自己解析得回来**的值(F3)。

    ``ask_jobs.created_at`` 的 DDL 是 ``TEXT NOT NULL DEFAULT ''``,空串是合法的历史
    脏值;``_absolute_instant`` 的 COALESCE 刻意容忍它(排到最末)。但游标曾经原样回传
    那个空串,而空串正是 MALFORMED_BOUNDS 里登记的畸形输入——第一页正常并给出
    ``cursor {'ts': ''}``,第二页在 parse_activity_instant 直接抛 ValueError。

    这条用例走完整的翻页闭环:三行脏值 + limit=2,必须能翻到底、不抛错、不重不漏。
    """
    with repo._write() as db:
        _insert_user(db, "u1", "a00000001")
        _insert_notebook(db, "n1", "u1")
        for ask_id in ("dirty-c", "dirty-b", "dirty-a"):
            _insert_ask(db, ask_id, "n1", "u1", "")

    seen: list[str] = []
    cursor_ts: str | None = None
    cursor_id: str | None = None
    for _ in range(5):  # 有界:三行 + limit=2 最多两页,多出来的轮次证明它真的会停
        page = repo.list_user_activity(
            "u1", before_ts=cursor_ts, before_id=cursor_id, limit=2,
        )
        seen.extend(item["id"] for item in page["items"])
        if not page["has_more"] or page["next_cursor"] is None:
            break
        cursor_ts = page["next_cursor"]["ts"]
        cursor_id = page["next_cursor"]["id"]

    # 三行全部取到,一行不重(游标原样回传空串时这里会先抛 ValueError)。
    assert sorted(seen) == ["dirty-a", "dirty-b", "dirty-c"]
    assert len(seen) == len(set(seen))
    # 并且游标发出的确实是一个能被解析回来的值。
    assert cursor_ts is not None
    parse_activity_instant(cursor_ts, field="before_ts")


@pytest.mark.parametrize("bad", MALFORMED_BOUNDS)
def test_malformed_cursor_raises_value_error(repo, bad):
    """畸形游标同样抛错:静默忽略会让翻页永远停在第一页(客户端死循环)。"""
    _seed_parity_rows(repo)
    with pytest.raises(ValueError):
        repo.list_user_activity("u1", before_ts=bad, before_id="ask-a", limit=2)


# ------------------------------------------------------- 披露面 / 排序键守卫

def test_source_never_exposes_the_raw_error_message(repo):
    """`sources.error_message` 是 str(exc) 原样落库、可能带服务端绝对路径。

    这条流是给 admin 看**别人**的活动,正是 ScopedSourceDetail 要防的场景——原始
    诊断一个字都不许跨出来,安全事实由 parse_failed / parse_quality_warning 承载。
    """
    leaky = "[pdf-python-fallback] FileNotFoundError: /srv/app/storage/notebooks/n1/x.pdf"
    with repo._write() as db:
        _insert_user(db, "u1", "a00000001")
        _insert_notebook(db, "n1", "u1")
        _insert_source(db, "src-failed", "n1", "2026-08-01T10:00:00",
                        parse_status="failed", status="failed",
                        error_message=leaky)
        _insert_source(db, "src-ok", "n1", "2026-08-01T09:00:00")

    result = repo.list_user_activity("u1", limit=50)
    by_id = {item["id"]: item for item in result["items"]}

    failed = by_id["src-failed"]
    assert "error_message" not in failed
    assert leaky not in json.dumps(result, ensure_ascii=False, default=str)
    # 但「解析失败了」这个信号必须留住(与 ScopedSourceDetail.of 同一条派生)。
    assert failed["parse_failed"] is True
    assert failed["parse_quality_warning"] is True
    assert by_id["src-ok"]["parse_failed"] is False


def test_ask_page_boundary_follows_created_at_not_asked_at(repo):
    """ask 的 SQL ORDER BY 必须真的被钉住。

    一页装得下时 Python 会把整个 pool 重排一遍,SQL 的 ORDER BY 写错完全不可见。
    这里让 ask 行数**多于** limit:SQL 只取回前 limit+1 行,选错排序键就会取回
    错误的子集,Python 再怎么重排也补不回来。asked_at 与 created_at 的顺序刻意
    完全相反。
    """
    order = ["a-1", "a-2", "a-3", "a-4", "a-5"]
    with repo._write() as db:
        _insert_user(db, "u1", "a00000001")
        _insert_notebook(db, "n1", "u1")
        for index, ask_id in enumerate(order):
            _insert_ask(
                db, ask_id, "n1", "u1", f"2026-08-01T1{index}:00:00",
                asked_at=f"2026-08-01T2{4 - index}:00:00",
            )

    page = repo.list_user_activity("u1", limit=2)
    assert [item["id"] for item in page["items"]] == ["a-5", "a-4"]
    assert page["has_more"] is True
    # ORDER BY asked_at 会让 SQL 取回 a-1/a-2/a-3,Python 重排后得到 ["a-3","a-2"]。
    assert [item["id"] for item in page["items"]] != ["a-3", "a-2"]

    second = repo.list_user_activity(
        "u1", before_ts=page["next_cursor"]["ts"],
        before_id=page["next_cursor"]["id"], limit=2,
    )
    assert [item["id"] for item in second["items"]] == ["a-3", "a-2"]


def test_deleted_notebook_keeps_only_expiring_activity_metadata(repo):
    """Deleting the aggregate keeps the admin-analysis projection, not content."""
    created_at = "2026-08-30T10:00:00+00:00"
    with repo._write() as db:
        _insert_user(db, "u1", "a00000001")
        _insert_notebook(db, "n1", "u1")
        db.execute(
            "INSERT INTO conversations "
            "(id,notebook_id,title,created_by,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            ("conv-1", "n1", "private conversation", "u1", created_at, created_at),
        )
        _insert_source(
            db, "src-1", "n1", created_at,
            title="", file_name="private.pdf",
        )
        _insert_report(
            db, "rep-1", "n1", "u1", created_at,
            question="Report prompt", depth=4,
        )
        db.execute(
            "UPDATE reports SET sections_json=? WHERE id='rep-1'",
            (json.dumps([{"content": "private report body"}]),),
        )
        db.execute(
            "INSERT INTO answers "
            "(id,notebook_id,question,payload,created_at,conversation_id) "
            "VALUES (?,?,?,?,?,?)",
            (
                "ans-1", "n1", "Private question",
                json.dumps({"answer": "private answer body"}),
                created_at, "conv-1",
            ),
        )
        db.execute(
            "INSERT INTO ask_jobs "
            "(id,notebook_id,conversation_id,created_by,mode,question,status,"
            "asked_at,answer_id,error,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "ask-1", "n1", "conv-1", "u1", "reasoning",
                "Private question", "done", created_at, "ans-1",
                "private server error",
                created_at, created_at,
            ),
        )
        db.execute(
            "INSERT INTO ask_trace_steps(job_id,seq,step_json,created_at) "
            "VALUES (?,?,?,?)",
            (
                "ask-1", 0,
                json.dumps({"detail": "private reasoning trace"}), created_at,
            ),
        )

    repo._runtime.notebook_store.delete_row_and_orphan_embeddings("n1")

    with repo._connect() as db:
        assert db.execute("SELECT 1 FROM notebooks WHERE id='n1'").fetchone() is None
        assert db.execute("SELECT 1 FROM answers WHERE id='ans-1'").fetchone() is None
        assert db.execute(
            "SELECT 1 FROM ask_trace_steps WHERE job_id='ask-1'"
        ).fetchone() is None
        retained = db.execute(
            "SELECT activity_type,record_id,notebook_name,deleted_at,expires_at "
            "FROM retained_user_activity ORDER BY activity_type"
        ).fetchall()
        assert [(row["activity_type"], row["record_id"]) for row in retained] == [
            ("ask", "ask-1"), ("report", "rep-1"), ("source", "src-1"),
        ]
        archive_columns = {
            row["name"]
            for row in db.execute("PRAGMA table_info(retained_user_activity)")
        }
        assert not {
            "answer", "answer_id", "error", "trace", "trace_json", "sections_json",
            "source_content", "citation",
        } & archive_columns
        deleted_at = parse_activity_instant(retained[0]["deleted_at"], field="deleted_at")
        expires_at = parse_activity_instant(retained[0]["expires_at"], field="expires_at")
        assert (expires_at - deleted_at).days == 180

    activity = repo.list_user_activity(
        "u1", include_inaccessible_questions=True, limit=50,
    )
    assert {item["id"] for item in activity["items"]} == {
        "ask-1", "src-1", "rep-1",
    }
    assert all(item["notebook_name"] == "NB-n1" for item in activity["items"])
    source_item = next(item for item in activity["items"] if item["type"] == "source")
    assert source_item["display_title"] == "private.pdf"
    detail = repo.ask_job_detail("ask-1")
    assert detail["trace"] == []
    assert detail["answer_id"] == ""
    assert detail["error"] == ""
    assert detail["notebook_name"] == "NB-n1"

    usage = next(row for row in repo.list_user_usage() if row["id"] == "u1")
    assert usage["notebooks"] == 0
    assert usage["sources"] == 1
    assert usage["conversations"] == 1
    assert usage["questions"] == 1
    assert usage["reports"] == 1
    assert usage["last_active"] == created_at

    # Expiry is a hard read gate even before physical maintenance runs.
    with repo._write() as db:
        db.execute(
            "UPDATE retained_user_activity SET expires_at='2000-01-01T00:00:00+00:00'"
        )
    assert repo.list_user_activity(
        "u1", include_inaccessible_questions=True, limit=50,
    )["items"] == []
    expired_usage = next(row for row in repo.list_user_usage() if row["id"] == "u1")
    assert expired_usage["sources"] == 0
    # Conversations have an existing notebook-independent lifecycle and are
    # therefore unaffected by this retention window.
    assert expired_usage["conversations"] == 1
    assert expired_usage["questions"] == 0
    assert expired_usage["reports"] == 0
    assert expired_usage["last_active"] == created_at
    with pytest.raises(KeyError):
        repo.ask_job_detail("ask-1")

    repo._migrator.recover_interrupted_jobs()
    with repo._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM retained_user_activity").fetchone()[0] == 0
