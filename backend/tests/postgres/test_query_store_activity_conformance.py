"""PostgreSQL twin of ``list_user_activity`` — proves the SQLite/PostgreSQL
implementations agree on fields, sort order, and keyset pagination semantics.

See ``backend/tests/test_admin_user_activity.py`` for the fast SQLite-only
suite and
docs/superpowers/specs/2026-08-04-user-activity-log-view-design_zh.md §4.1
for the design. Gated behind ``TEST_POSTGRES_URL`` via ``pytest.mark.
postgres_integration`` (see backend/tests/postgres/conftest.py).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.repositories.postgres._store_utils import jsonb
from app.repositories.postgres.migrator import PostgresMigrator
from app.repositories.postgres.query_store import QueryStore as PostgresQueryStore

from tests.activity_parity_cases import (
    ACTIVITY_ROWS,
    BOUND_PARITY_CASES,
    LOCAL_DAY_EXPECTED,
    LOCAL_DAY_ROWS,
    LOCAL_DAY_SINCE,
    LOCAL_DAY_UNTIL,
    MALFORMED_BOUNDS,
)


pytestmark = pytest.mark.postgres_integration


NOW = datetime(2026, 8, 1, 10, 0, 0, tzinfo=timezone.utc)


def _insert_user(connection, user_id: str) -> None:
    connection.execute(
        "INSERT INTO users (id,email,display_name,role,created_at,updated_at) "
        "VALUES (%s,%s,%s,'user',%s,%s)",
        (user_id, f"{user_id}@x", user_id.upper(), NOW, NOW),
    )


def _insert_notebook(connection, notebook_id: str, owner: str, status: str = "ready") -> None:
    connection.execute(
        "INSERT INTO notebooks (id,name,created_by,status,created_at,updated_at) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (notebook_id, f"NB-{notebook_id}", owner, status, NOW, NOW),
    )


def _insert_ask(connection, job_id, notebook_id, created_by, created_at, **kw) -> None:
    connection.execute(
        "INSERT INTO ask_jobs "
        "(id,notebook_id,created_by,mode,question,status,asked_at,answer_id,error,"
        "created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            job_id, notebook_id, created_by,
            kw.get("mode", "chunk"), kw.get("question", "q?"),
            kw.get("status", "completed"), kw.get("asked_at", ""),
            kw.get("answer_id", ""), kw.get("error", ""),
            created_at, created_at,
        ),
    )


def _insert_source(connection, source_id, notebook_id, created_at, **kw) -> None:
    connection.execute(
        "INSERT INTO sources "
        "(id,notebook_id,title,source_type,status,parse_status,file_name,"
        "error_message,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            source_id, notebook_id, kw.get("title", "Doc"),
            kw.get("source_type", "pdf"), kw.get("status", "parsed"),
            kw.get("parse_status", "parsed"), kw.get("file_name", "doc.pdf"),
            kw.get("error_message", ""), created_at, created_at,
        ),
    )


def _insert_paper_meta(connection, source_id, notebook_id, paper_title, *, is_paper=1) -> None:
    connection.execute(
        "INSERT INTO source_paper_meta "
        "(source_id,notebook_id,is_paper,paper_title,created_at,updated_at) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (source_id, notebook_id, is_paper, paper_title, NOW, NOW),
    )


def _insert_extraction_run(connection, run_id, notebook_id, source_id, error_message,
                            created_at=NOW) -> None:
    connection.execute(
        "INSERT INTO extraction_runs "
        "(id,notebook_id,source_id,run_type,status,error_message,created_at,updated_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (run_id, notebook_id, source_id, "kg", "completed", error_message,
         created_at, created_at),
    )


def _insert_report(connection, report_id, notebook_id, created_by, created_at, **kw) -> None:
    generation_started_at = kw.get("generation_started_at")
    understanding = (
        jsonb({"_generation_started_at": generation_started_at})
        if generation_started_at is not None else jsonb({})
    )
    connection.execute(
        "INSERT INTO reports "
        "(id,notebook_id,question,depth,status,created_by,understanding_json,"
        "created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            report_id, notebook_id, kw.get("question", "report?"),
            kw.get("depth", 2), kw.get("status", "done"), created_by,
            understanding, created_at, created_at,
        ),
    )


@pytest.fixture
def store(postgres_database, postgres_settings):
    assert PostgresMigrator(postgres_database).migrate() == 38
    return PostgresQueryStore(postgres_database, postgres_settings)


def test_merges_three_types_in_time_descending_order(postgres_database, store):
    with postgres_database.write() as connection:
        _insert_user(connection, "u1")
        _insert_notebook(connection, "n1", "u1")
        _insert_ask(connection, "ask-1", "n1", "u1", datetime(2026, 8, 1, 10, tzinfo=timezone.utc))
        _insert_source(connection, "src-1", "n1", datetime(2026, 8, 1, 11, tzinfo=timezone.utc))
        _insert_report(connection, "rep-1", "n1", "u1", datetime(2026, 8, 1, 12, tzinfo=timezone.utc))
        _insert_ask(connection, "ask-2", "n1", "u1", datetime(2026, 8, 1, 9, tzinfo=timezone.utc))

    result = store.list_user_activity("u1", limit=50)
    assert [item["id"] for item in result["items"]] == ["rep-1", "src-1", "ask-1", "ask-2"]
    assert result["has_more"] is False
    assert result["next_cursor"] is None
    # created_at must come back as an ISO string (not a raw datetime) — same
    # wire shape as the SQLite backend.
    assert isinstance(result["items"][0]["created_at"], str)


def test_source_hides_memory_and_knowhow_and_carries_paper_meta(postgres_database, store):
    with postgres_database.write() as connection:
        _insert_user(connection, "u1")
        _insert_notebook(connection, "n1", "u1")
        _insert_source(connection, "src-visible", "n1",
                        datetime(2026, 8, 1, 10, tzinfo=timezone.utc), title="Paper A")
        _insert_paper_meta(connection, "src-visible", "n1", "The Real Paper Title")
        _insert_source(connection, "src-plain", "n1",
                        datetime(2026, 8, 1, 9, tzinfo=timezone.utc), title="Plain Doc")
        _insert_source(connection, "src-memory", "n1",
                        datetime(2026, 8, 1, 12, tzinfo=timezone.utc), source_type="memory")
        _insert_source(connection, "src-knowhow", "n1",
                        datetime(2026, 8, 1, 13, tzinfo=timezone.utc), source_type="knowhow")

    result = store.list_user_activity("u1", limit=50)
    ids = [item["id"] for item in result["items"]]
    assert "src-memory" not in ids
    assert "src-knowhow" not in ids
    assert ids == ["src-visible", "src-plain"]

    by_id = {item["id"]: item for item in result["items"]}
    assert by_id["src-visible"]["is_paper"] is True
    assert by_id["src-visible"]["paper_title"] == "The Real Paper Title"
    assert by_id["src-plain"]["is_paper"] is False
    assert by_id["src-plain"]["paper_title"] == ""


def test_sources_scoped_to_owned_notebooks_only(postgres_database, store):
    with postgres_database.write() as connection:
        _insert_user(connection, "u1")
        _insert_user(connection, "u2")
        _insert_notebook(connection, "n1", "u1")
        _insert_notebook(connection, "n2", "u2")
        _insert_source(connection, "src-mine", "n1", datetime(2026, 8, 1, 10, tzinfo=timezone.utc))
        _insert_source(connection, "src-other", "n2", datetime(2026, 8, 1, 11, tzinfo=timezone.utc))

    result = store.list_user_activity("u1", limit=50)
    assert [item["id"] for item in result["items"]] == ["src-mine"]


def test_explicit_notebook_id_not_owned_returns_empty(postgres_database, store):
    with postgres_database.write() as connection:
        _insert_user(connection, "u1")
        _insert_user(connection, "u2")
        _insert_notebook(connection, "n1", "u1")
        _insert_notebook(connection, "n2", "u2")
        _insert_ask(connection, "ask-u2", "n2", "u2", NOW)

    empty = {"items": [], "has_more": False, "next_cursor": None}
    assert store.list_user_activity("u1", notebook_id="n2", limit=50) == empty
    assert store.list_user_activity("u1", notebook_id="does-not-exist", limit=50) == empty


def test_composite_cursor_ties_across_types_no_skip_or_duplicate(postgres_database, store):
    with postgres_database.write() as connection:
        _insert_user(connection, "u1")
        _insert_notebook(connection, "n1", "u1")
        for ask_id in ("ask-1", "ask-2"):
            _insert_ask(connection, ask_id, "n1", "u1", NOW)
        for src_id in ("src-1", "src-2"):
            _insert_source(connection, src_id, "n1", NOW)
        for rep_id in ("rep-1", "rep-2"):
            _insert_report(connection, rep_id, "n1", "u1", NOW)

    expected_order = ["src-2", "src-1", "rep-2", "rep-1", "ask-2", "ask-1"]

    seen: list[str] = []
    cursor_ts = None
    cursor_id = None
    pages = 0
    while True:
        pages += 1
        assert pages <= 10, "pagination did not terminate"
        page = store.list_user_activity(
            "u1", before_ts=cursor_ts, before_id=cursor_id, limit=2,
        )
        seen.extend(item["id"] for item in page["items"])
        if not page["has_more"]:
            assert page["next_cursor"] is None
            break
        cursor_ts = page["next_cursor"]["ts"]
        cursor_id = page["next_cursor"]["id"]

    assert seen == expected_order
    assert len(seen) == len(set(seen))


def test_ask_and_reports_are_owner_only_like_sources(postgres_database, store):
    """三类统一 owner-only,与 SQLite 侧同口径(见那边的同名测试)。"""
    with postgres_database.write() as connection:
        _insert_user(connection, "u1")
        _insert_user(connection, "u2")
        _insert_notebook(connection, "n1", "u1")
        _insert_notebook(connection, "n2", "u2")
        _insert_ask(connection, "ask-shared", "n2", "u1", NOW)
        _insert_report(connection, "rep-shared", "n2", "u1", NOW)
        _insert_ask(connection, "ask-mine", "n1", "u1", NOW)
        _insert_report(connection, "rep-mine", "n1", "u1", NOW)

    result = store.list_user_activity("u1", limit=50)
    ids = {item["id"] for item in result["items"]}
    assert ids == {"ask-mine", "rep-mine"}

    scoped = store.list_user_activity("u1", notebook_id="n2", limit=50)
    assert scoped == {"items": [], "has_more": False, "next_cursor": None}


def test_source_extraction_and_parse_quality_warnings(postgres_database, store):
    with postgres_database.write() as connection:
        _insert_user(connection, "u1")
        _insert_notebook(connection, "n1", "u1")
        _insert_source(connection, "src-degraded", "n1",
                        datetime(2026, 8, 1, 10, tzinfo=timezone.utc))
        _insert_extraction_run(connection, "run-1", "n1", "src-degraded",
                                "windows_failed=2/5")
        _insert_source(connection, "src-clean", "n1",
                        datetime(2026, 8, 1, 9, tzinfo=timezone.utc))
        _insert_extraction_run(connection, "run-2", "n1", "src-clean", "")
        _insert_source(connection, "src-none", "n1",
                        datetime(2026, 8, 1, 8, tzinfo=timezone.utc))
        _insert_source(
            connection, "src-fallback", "n1",
            datetime(2026, 8, 1, 7, tzinfo=timezone.utc),
            error_message="[pdf-python-fallback] mineru retries exhausted",
        )

    result = store.list_user_activity("u1", limit=50)
    by_id = {item["id"]: item for item in result["items"]}

    degraded = by_id["src-degraded"]
    assert degraded["extraction_warning"] == (
        "部分内容因网络问题未完成分析（2/5 段失败），建议重新上传或重试。"
    )
    assert degraded["parse_quality_warning"] is False

    clean = by_id["src-clean"]
    assert clean["extraction_warning"] is None
    assert clean["parse_quality_warning"] is False

    assert by_id["src-none"]["extraction_warning"] is None

    fallback = by_id["src-fallback"]
    assert fallback["parse_quality_warning"] is True
    assert fallback["extraction_warning"] is None


def test_source_paper_meta_status_four_states(postgres_database, store):
    with postgres_database.write() as connection:
        _insert_user(connection, "u1")
        _insert_notebook(connection, "n1", "u1")
        _insert_source(connection, "src-has-meta", "n1",
                        datetime(2026, 8, 1, 10, tzinfo=timezone.utc))
        _insert_paper_meta(connection, "src-has-meta", "n1", "A Paper Title", is_paper=1)
        _insert_source(connection, "src-not-paper", "n1",
                        datetime(2026, 8, 1, 9, tzinfo=timezone.utc))
        _insert_paper_meta(connection, "src-not-paper", "n1", None, is_paper=0)
        _insert_source(connection, "src-missing", "n1",
                        datetime(2026, 8, 1, 8, tzinfo=timezone.utc),
                        parse_status="parsed")
        _insert_source(connection, "src-not-applicable", "n1",
                        datetime(2026, 8, 1, 7, tzinfo=timezone.utc),
                        parse_status="queued")

    result = store.list_user_activity("u1", limit=50)
    by_id = {item["id"]: item for item in result["items"]}
    assert by_id["src-has-meta"]["paper_meta_status"] == "has_meta"
    assert by_id["src-not-paper"]["paper_meta_status"] == "not_paper"
    assert by_id["src-missing"]["paper_meta_status"] == "missing"
    assert by_id["src-not-applicable"]["paper_meta_status"] is None


def test_report_generation_started_at_present_and_missing(postgres_database, store):
    with postgres_database.write() as connection:
        _insert_user(connection, "u1")
        _insert_notebook(connection, "n1", "u1")
        _insert_report(
            connection, "rep-with-start", "n1", "u1",
            datetime(2026, 8, 1, 10, tzinfo=timezone.utc),
            generation_started_at="2026-08-01T10:05:00+00:00",
        )
        _insert_report(connection, "rep-without-start", "n1", "u1",
                        datetime(2026, 8, 1, 9, tzinfo=timezone.utc))

    result = store.list_user_activity("u1", limit=50)
    by_id = {item["id"]: item for item in result["items"]}
    assert by_id["rep-with-start"]["generation_started_at"] == "2026-08-01T10:05:00+00:00"
    assert by_id["rep-without-start"]["generation_started_at"] == ""


def test_since_and_until_half_open_interval(postgres_database, store):
    with postgres_database.write() as connection:
        _insert_user(connection, "u1")
        _insert_notebook(connection, "n1", "u1")
        _insert_ask(connection, "ask-before", "n1", "u1",
                    datetime(2026, 8, 1, 9, 59, 59, tzinfo=timezone.utc))
        _insert_ask(connection, "ask-at-since", "n1", "u1",
                    datetime(2026, 8, 1, 10, 0, 0, tzinfo=timezone.utc))
        _insert_ask(connection, "ask-inside", "n1", "u1",
                    datetime(2026, 8, 1, 15, 0, 0, tzinfo=timezone.utc))
        _insert_ask(connection, "ask-at-until", "n1", "u1",
                    datetime(2026, 8, 2, 0, 0, 0, tzinfo=timezone.utc))
        _insert_ask(connection, "ask-after", "n1", "u1",
                    datetime(2026, 8, 2, 0, 0, 1, tzinfo=timezone.utc))

    since = "2026-08-01T10:00:00+00:00"
    until = "2026-08-02T00:00:00+00:00"

    since_only = store.list_user_activity("u1", since=since, limit=50)
    ids = {item["id"] for item in since_only["items"]}
    assert ids == {"ask-at-since", "ask-inside", "ask-at-until", "ask-after"}

    until_only = store.list_user_activity("u1", until=until, limit=50)
    ids = {item["id"] for item in until_only["items"]}
    assert ids == {"ask-before", "ask-at-since", "ask-inside"}

    day = store.list_user_activity("u1", since=since, until=until, limit=50)
    ids = {item["id"] for item in day["items"]}
    assert ids == {"ask-at-since", "ask-inside"}
    assert "ask-at-until" not in ids
    assert "ask-before" not in ids
    assert "ask-after" not in ids


def test_since_until_combined_with_cursor_pagination(postgres_database, store):
    with postgres_database.write() as connection:
        _insert_user(connection, "u1")
        _insert_notebook(connection, "n1", "u1")
        _insert_ask(connection, "ask-outside-early", "n1", "u1",
                    datetime(2026, 8, 1, 0, 0, 0, tzinfo=timezone.utc))
        _insert_ask(connection, "ask-outside-late", "n1", "u1",
                    datetime(2026, 8, 3, 0, 0, 0, tzinfo=timezone.utc))
        for i in range(4):
            _insert_ask(connection, f"ask-in-{i}", "n1", "u1",
                        datetime(2026, 8, 1, 10 + i, 0, 0, tzinfo=timezone.utc))

    since = "2026-08-01T10:00:00+00:00"
    until = "2026-08-02T00:00:00+00:00"
    seen: list[str] = []
    cursor_ts = None
    cursor_id = None
    pages = 0
    while True:
        pages += 1
        assert pages <= 10
        page = store.list_user_activity(
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


# ------------------------------------------------- 时间边界:双后端共用用例表

def _seed_parity_rows(connection) -> None:
    _insert_user(connection, "u1")
    _insert_notebook(connection, "n1", "u1")
    for ask_id, created_at in ACTIVITY_ROWS:
        _insert_ask(connection, ask_id, "n1", "u1", datetime.fromisoformat(created_at))


@pytest.mark.parametrize("since,until,expected", BOUND_PARITY_CASES)
def test_bound_spellings_select_the_same_rows(postgres_database, store, since, until, expected):
    """与 SQLite 侧的同名测试跑**同一张** BOUND_PARITY_CASES。

    这条分叉曾经躲过两份测试:SQLite 的用例只喂裸 naive、PostgreSQL 的只喂
    `+00:00`,而浏览器实际发的是 `...Z`。同一张表才拦得住。
    """
    with postgres_database.write() as connection:
        _seed_parity_rows(connection)

    result = store.list_user_activity("u1", since=since, until=until, limit=50)
    assert {item["id"] for item in result["items"]} == expected


def test_null_created_at_sorts_last_instead_of_500(postgres_database, store):
    """``ask_jobs.created_at`` 在 PG schema 里**可空**,不能让它把整条流打成 500(F2)。

    ``0001_initial.sql`` 里这一列没有 NOT NULL(``sources`` / ``reports`` 有);而停机
    importer 的 ``_parse_timestamp("") -> None`` 会把 SQLite 那侧
    ``TEXT NOT NULL DEFAULT ''`` 的空串行迁成 NULL。

    少了 COALESCE 兜底会同时炸两处:PG 的 ``ORDER BY ... DESC`` 默认 **NULLS FIRST**,
    这行必定在第 1 页;而 Python 归并键里的 None 直接
    ``TypeError: '<' not supported between instances of 'NoneType' and 'datetime.datetime'``。
    于是该用户的活动流每次请求都 500。

    口径与 SQLite 孪生一致:排到最末,不是排到最前、更不是抛错。
    """
    with postgres_database.write() as connection:
        _insert_user(connection, "u1")
        _insert_notebook(connection, "n1", "u1")
        _insert_ask(connection, "ask-null", "n1", "u1", None)
        _insert_ask(connection, "ask-old", "n1", "u1",
                    datetime(2026, 8, 1, 9, tzinfo=timezone.utc))
        _insert_ask(connection, "ask-new", "n1", "u1",
                    datetime(2026, 8, 1, 10, tzinfo=timezone.utc))

    result = store.list_user_activity("u1", limit=50)
    # 不抛错,三行都在,NULL 那行排最末(而不是被 NULLS FIRST 顶到第一位)。
    assert [item["id"] for item in result["items"]] == ["ask-new", "ask-old", "ask-null"]

    # 翻页也要能走过那一行:游标回传的 ts 必须解析得回来(与 F3 同一条契约)。
    page = store.list_user_activity("u1", limit=2)
    assert page["has_more"] is True
    tail = store.list_user_activity(
        "u1", before_ts=page["next_cursor"]["ts"],
        before_id=page["next_cursor"]["id"], limit=2,
    )
    assert [item["id"] for item in tail["items"]] == ["ask-null"]


def test_source_count_excludes_hidden_projection_sources(postgres_database, store):
    """与 SQLite 侧逐字同一条(F4):表头计数与展开清单同口径。"""
    with postgres_database.write() as connection:
        _insert_user(connection, "u1")
        _insert_notebook(connection, "n1", "u1")
        for source_id, source_type in (
            ("s-real", "pdf"), ("s-mem", "memory"), ("s-know", "knowhow"),
        ):
            _insert_source(connection, source_id, "n1", NOW, source_type=source_type)

    rows = store.list_user_notebooks("u1")
    assert rows[0]["sources"] == 1


def test_reports_count_matches_activity_stream_entries(postgres_database, store):
    """与 SQLite 侧逐字同一条(P2-2,codex 第 3 轮):左栏「报告 N」必须与中栏活动流
    真正展开的报告条目数一致——两者都必须按 created_by 收窄,否则共享笔记本里别的
    可写成员建的报告会被计进 owner 的表头,点进去却在活动流里看不到对应条目。"""
    with postgres_database.write() as connection:
        _insert_user(connection, "u1")
        _insert_user(connection, "u2")
        _insert_notebook(connection, "n1", "u1")
        _insert_report(connection, "rep-mine", "n1", "u1", NOW)
        _insert_report(connection, "rep-other-member", "n1", "u2", NOW)

    rows = store.list_user_notebooks("u1")
    reports_count = rows[0]["reports"]
    assert reports_count == 1

    activity = store.list_user_activity("u1", notebook_id="n1", limit=50)
    report_ids = {item["id"] for item in activity["items"] if item["type"] == "report"}
    assert report_ids == {"rep-mine"}
    assert len(report_ids) == reports_count


def test_pending_report_of_a_member_without_own_notebooks(postgres_database, store):
    """PostgreSQL 侧的同一条 P1-T3b 断言:待确认报告那一半**不在**
    `if notebook_ids:` 闸内。

    共享笔记本里的成员可以建自己的报告,但他可能一个自有库都没有;闸内的写法会让
    他的铃铛恒为 0,而报告卡在 intent_ready 等他确认。两个后端各有一份实现,所以
    这条不变量必须两边各钉一次。

    P1-T4 又加了一条:库名不再取自「我自有的库」映射(共享库不在其中,条目会显示成
    一条没有出处的报告),而是随报告行 LEFT JOIN 出来。"""
    with postgres_database.write() as connection:
        _insert_user(connection, "u-owner")
        _insert_user(connection, "u-member")            # 没有任何自有 notebook
        _insert_notebook(connection, "n-shared", "u-owner")
        # 建报告的前提本来就是读权;铃铛现叠加同一读谓词(codex #517 R1 P2),
        # 夹具如实给出成员行。
        connection.execute(
            "INSERT INTO notebook_members (notebook_id, user_id, role, added_at) "
            "VALUES (%s,%s,'reader',%s)",
            ("n-shared", "u-member", NOW),
        )
        _insert_report(connection, "rep-member", "n-shared", "u-member", NOW,
                       status="intent_ready")

    projection = store.pending_actions_projection_rows("u-member")
    assert projection["notebook_ids"] == []
    reports = [it for it in projection["items"] if it["type"] == "report_outline"]
    assert [it["report_id"] for it in reports] == ["rep-member"]
    assert reports[0]["notebook_id"] == "n-shared"
    # 库名来自共享库那一行本身(P1-T4),不是「我自有的库」映射。
    assert reports[0]["notebook_name"] == "NB-n-shared"

    # 反向:owner 的铃铛里不出现成员的报告。
    owner_reports = [
        it for it in store.pending_actions_projection_rows("u-owner")["items"]
        if it["type"] == "report_outline"
    ]
    assert owner_reports == []

    # 撤权即出铃铛、恢复即回(codex #517 R1 P2 的 PG 侧对等断言)。
    with postgres_database.write() as connection:
        connection.execute(
            "DELETE FROM notebook_members WHERE notebook_id=%s AND user_id=%s",
            ("n-shared", "u-member"),
        )
    gone = [
        it for it in store.pending_actions_projection_rows("u-member")["items"]
        if it["type"] == "report_outline"
    ]
    assert gone == []


def test_browser_local_day_window_selects_that_local_day(postgres_database, store):
    """与 SQLite 侧逐字同一条(F1):选「浏览器本地日」要真的选中那一天的行。

    BOUND_PARITY_CASES 全是同一时刻的不同写法,证明不了日边界落在哪。
    """
    with postgres_database.write() as connection:
        _insert_user(connection, "u1")
        _insert_notebook(connection, "n1", "u1")
        for ask_id, created_at in LOCAL_DAY_ROWS:
            _insert_ask(connection, ask_id, "n1", "u1",
                        datetime.fromisoformat(created_at))

    result = store.list_user_activity(
        "u1", since=LOCAL_DAY_SINCE, until=LOCAL_DAY_UNTIL, limit=50,
    )
    assert {item["id"] for item in result["items"]} == LOCAL_DAY_EXPECTED


@pytest.mark.parametrize("bad", MALFORMED_BOUNDS)
@pytest.mark.parametrize("field", ["since", "until"])
def test_malformed_range_bound_raises_value_error(postgres_database, store, bad, field):
    with postgres_database.write() as connection:
        _seed_parity_rows(connection)
    with pytest.raises(ValueError):
        store.list_user_activity("u1", **{field: bad}, limit=50)


@pytest.mark.parametrize("bad", MALFORMED_BOUNDS)
def test_malformed_cursor_raises_value_error(postgres_database, store, bad):
    with postgres_database.write() as connection:
        _seed_parity_rows(connection)
    with pytest.raises(ValueError):
        store.list_user_activity("u1", before_ts=bad, before_id="ask-a", limit=2)


# ------------------------------------------------------------ 披露面 / 取数序

def test_source_never_exposes_the_raw_error_message(postgres_database, store):
    """与 SQLite 侧逐字同一条:原始诊断不出库,只留 parse_failed 布尔。"""
    leaky = "[pdf-python-fallback] FileNotFoundError: /srv/app/storage/notebooks/n1/x.pdf"
    with postgres_database.write() as connection:
        _insert_user(connection, "u1")
        _insert_notebook(connection, "n1", "u1")
        _insert_source(connection, "src-failed", "n1",
                        datetime(2026, 8, 1, 10, tzinfo=timezone.utc),
                        parse_status="failed", status="failed", error_message=leaky)
        _insert_source(connection, "src-ok", "n1",
                        datetime(2026, 8, 1, 9, tzinfo=timezone.utc))

    result = store.list_user_activity("u1", limit=50)
    by_id = {item["id"]: item for item in result["items"]}

    failed = by_id["src-failed"]
    assert "error_message" not in failed
    assert leaky not in json.dumps(result, ensure_ascii=False, default=str)
    assert failed["parse_failed"] is True
    assert failed["parse_quality_warning"] is True
    assert by_id["src-ok"]["parse_failed"] is False


def test_latest_extraction_run_breaks_created_at_ties_by_ordinal(postgres_database, store):
    """并列 created_at 下「最近一次抽取」必须由 ordinal 定序。

    ordinal 存在的唯一理由就是给平局提供确定序;少了它,同秒写入的两条抽取记录
    谁算「最近一次」由 planner 决定,同一份来源的告警会时有时无。
    """
    same = datetime(2026, 8, 1, 10, tzinfo=timezone.utc)
    with postgres_database.write() as connection:
        _insert_user(connection, "u1")
        _insert_notebook(connection, "n1", "u1")
        _insert_source(connection, "src-1", "n1", same)
        # 先写降级的那条,再写干净的那条(ordinal 更大 = 更晚)。
        _insert_extraction_run(connection, "run-old", "n1", "src-1",
                                "windows_failed=2/5", created_at=same)
        _insert_extraction_run(connection, "run-new", "n1", "src-1", "",
                                created_at=same)

    result = store.list_user_activity("u1", limit=50)
    by_id = {item["id"]: item for item in result["items"]}
    assert by_id["src-1"]["extraction_warning"] is None
