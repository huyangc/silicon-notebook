"""The report list reads summary columns only, and its rows did not change.

``ReportStore.list_reports`` feeds the web list (``GET /api/notebooks/{id}/reports``)
and the MCP ``list_reports`` tool. A list row needs a dozen small fields, so the
statement selects those and derives the two JSON-backed ones (``section_count``
from the outline, ``generation_started_at`` from the understanding contract) in
SQL, instead of pulling every report's body, sections and references back.

Backend-agnostic: ``tests/test_report_list_summary.py`` (SQLite) and
``tests/postgres/test_report_list_summary_pg.py`` (PostgreSQL) run every case in
``CASES`` against ``create_app()`` and its real report store.

Two oracles pin "the rows did not change". The literal rows below were produced
by the pre-change statement (``SELECT *`` + ``ReportStore.row_to_dict`` deriving
both fields in Python) and the web response is compared against them field by
field; and the store's rows are compared with ``row_to_dict`` over a ``SELECT *``
of the same rows, which is still how ``get_report`` builds its summary part.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime

from tests.answer_link_service_cases import _notebook, _user, _write

FIRST = "2026-10-21T08:00:03+00:00"
SECOND = "2026-10-21T08:00:02+00:00"
THIRD = "2026-10-21T08:00:01+00:00"
STARTED = "2026-10-21T08:00:30+00:00"
LATER_START = "2026-10-21T08:01:00+00:00"


def _insert(env, **columns) -> None:
    names = ", ".join(columns)
    marks = ", ".join("?" for _ in columns)
    with _write(env) as db:
        db.execute(
            f"INSERT INTO reports ({names}) VALUES ({marks})", tuple(columns.values())
        )


def _seed(env, notebook_id: str, owner_id: str, other_id: str) -> None:
    sections = [{"title": name, "markdown": f"{name}节正文"} for name in "一二三"]
    # A complete report: every JSON column and the body filled in.
    _insert(
        env, id="rep-ls-1", notebook_id=notebook_id, question="完整报告",
        status="done", progress="完成", error="", depth=3, created_by=owner_id,
        created_at=FIRST, updated_at="2026-10-21T08:09:00+00:00",
        outline_json=json.dumps([{"title": s["title"]} for s in sections], ensure_ascii=False),
        sections_json=json.dumps(sections, ensure_ascii=False),
        references_json=json.dumps([{"key": "k1", "snippet": "摘录"}], ensure_ascii=False),
        content_md="# 正文\n\n" + "很长的正文。" * 200,
        understanding_json=json.dumps(
            {"resolved_question": "完整报告", "confirmed": True,
             "_generation_started_at": STARTED}, ensure_ascii=False),
    )
    # An old row: only the columns that were always required. No outline, no
    # sections, no understanding contract -- every JSON column is its default.
    _insert(
        env, id="rep-ls-2", notebook_id=notebook_id, question="旧报告",
        created_by=owner_id, created_at=SECOND, updated_at=SECOND,
    )
    # A failed report with an outline, and a stamp that is JSON null.
    _insert(
        env, id="rep-ls-3", notebook_id=notebook_id, question="失败的报告",
        status="failed", progress="失败", error="模型服务暂时不可用", depth=1,
        created_by=owner_id, created_at=THIRD, updated_at=THIRD,
        outline_json=json.dumps([{"title": "甲"}, {"title": "乙"}], ensure_ascii=False),
        understanding_json=json.dumps(
            {"objective": "q", "_generation_started_at": None}),
    )
    # Same creation instant as the one above: the id breaks the tie.
    _insert(
        env, id="rep-ls-4", notebook_id=notebook_id, question="同一时刻的报告",
        created_by=owner_id, created_at=THIRD, updated_at=THIRD,
        understanding_json=json.dumps({"_generation_started_at": LATER_START}),
    )
    _insert(
        env, id="rep-ls-other", notebook_id=notebook_id, question="别人的报告",
        created_by=other_id, created_at=FIRST, updated_at=FIRST,
        outline_json=json.dumps([{"title": "丙"}]),
    )


# What the pre-change statement returned for each seeded row, as the web list
# presents it (``ReportSummary``; the timestamps are compared as instants).
def _expected_web(owner_id: str) -> list[dict]:
    def row(rid, question, status, progress, sections, started, created):
        return {
            "id": rid, "question": question, "status": status, "progress": progress,
            "section_count": sections, "generation_started_at": started,
            "created_by": owner_id, "_created": created,
        }

    return [
        row("rep-ls-1", "完整报告", "done", "完成", 3, STARTED, FIRST),
        row("rep-ls-2", "旧报告", "pending", "", 0, "", SECOND),
        row("rep-ls-3", "失败的报告", "failed", "失败", 2, "", THIRD),
        row("rep-ls-4", "同一时刻的报告", "pending", "", 0, LATER_START, THIRD),
    ]


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _world(env):
    headers, owner_id = _user(env)
    notebook_id = _notebook(env, headers)
    _, other_id = _user(env)
    _seed(env, notebook_id, owner_id, other_id)
    return headers, owner_id, other_id, notebook_id


def _oracle(env, store, notebook_id: str, creator: str | None) -> list[dict]:
    """The rows as the pre-change statement built them."""
    sql = "SELECT * FROM reports WHERE notebook_id = ?"
    args: list = [notebook_id]
    if creator is not None:
        sql += " AND created_by = ?"
        args.append(creator)
    sql += " ORDER BY created_at DESC, id"
    with store.database.connect() as db:
        rows = db.execute(env.sql(sql), args).fetchall()
    return [store.row_to_dict(row, full=False) for row in rows]


async def web_list_is_unchanged(env):
    headers, owner_id, _other, notebook_id = _world(env)
    response = env.client.get(f"/api/notebooks/{notebook_id}/reports", headers=headers)
    assert response.status_code == 200, response.text
    shown = response.json()
    expected = _expected_web(owner_id)
    assert [item["id"] for item in shown] == [item["id"] for item in expected]
    for item, wanted in zip(shown, expected):
        created = wanted.pop("_created")
        assert set(item) == {
            "id", "question", "status", "progress", "section_count", "created_at",
            "generation_started_at", "updated_at", "created_by",
        }
        for key, value in wanted.items():
            assert item[key] == value, (item["id"], key)
        assert _instant(item["created_at"]) == _instant(created)
        assert _instant(item["updated_at"]) >= _instant(created)


async def store_rows_equal_the_pre_change_rows(env):
    _headers, owner_id, other_id, notebook_id = _world(env)
    store = env.repo._runtime.report_store
    for creator in (owner_id, other_id, None, "nobody"):
        listed = store.list_reports(notebook_id, created_by=creator)
        assert listed == _oracle(env, store, notebook_id, creator), creator
    # The whole row shape, not only what the web list keeps.
    [first] = [
        item for item in store.list_reports(notebook_id, created_by=owner_id)
        if item["id"] == "rep-ls-1"
    ]
    assert list(first) == [
        "id", "notebook_id", "question", "status", "progress", "error",
        "created_by", "created_at", "generation_started_at", "updated_at",
        "depth", "section_count",
    ]
    assert first["depth"] == 3 and first["error"] == "" and first["section_count"] == 3
    failed = {item["id"]: item for item in store.list_reports(
        notebook_id, created_by=owner_id)}["rep-ls-3"]
    assert failed["error"] == "模型服务暂时不可用" and failed["depth"] == 1
    assert store.list_reports("nb-nothing", created_by=None) == []


class _RecordingConnection:
    def __init__(self, inner, statements: list[str]) -> None:
        self._inner = inner
        self._statements = statements

    def execute(self, sql, *args, **kwargs):
        self._statements.append(str(sql))
        return self._inner.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


async def list_is_one_statement_over_summary_columns(env):
    """A list never pulls a report's body, sections, references or the whole
    understanding contract back, and costs one statement per notebook."""
    _headers, owner_id, _other, notebook_id = _world(env)
    store = env.repo._runtime.report_store
    statements: list[str] = []
    real = store.database.connect

    @contextmanager
    def recording():
        with real() as db:
            yield _RecordingConnection(db, statements)

    env.monkeypatch.setattr(store.database, "connect", recording)
    store.list_reports(notebook_id, created_by=owner_id)
    store.list_reports(notebook_id, created_by=None)
    assert len(statements) == 2, statements
    for sql in statements:
        assert "SELECT *" not in sql.upper().replace("  ", " "), sql
        for heavy in ("content_md", "sections_json", "references_json",
                      "gaps_json", "section_status_json", "share_token"):
            assert heavy not in sql, (heavy, sql)


CASES = {
    case.__name__: case
    for case in (
        web_list_is_unchanged,
        store_rows_equal_the_pre_change_rows,
        list_is_one_statement_over_summary_columns,
    )
}
