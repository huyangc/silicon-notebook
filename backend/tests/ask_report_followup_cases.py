"""Backend-neutral scenarios for the report a question attaches, judged and
read where the Ask engine is entered (plan 2026-10-21 T6, D4/D5/D11).

Each scenario enters ``AskService._engine_scope`` exactly as ``AskService.ask``
does -- the production default ceiling over the production readers, the
retrieval run, then the run's ``ReportFollowup`` -- and reads what the engine
would read (``current_report_followup()``): the verdict under the real ceiling
and, for a readable report, the cited items read back through the retrieval
layer. The pre-send check (``AskService.report_link_check``) is asked the same
question and must give the same answer field for field.

The world is the restore's (``tests/report_reference_restore_cases.py``):
Alice asks in ``nb`` (sources ``src-a`` / ``src-b``, the relation ``rel-a``,
her Memory ``mem-alice``), which mounts ``lib`` (``lib-src``, ``kg-lib``);
Bob is another member. ``REPORT`` cites one item of every kind.

``tests/test_ask_report_followup_service.py`` runs these on SQLite and
``tests/postgres/test_ask_report_followup_pg.py`` on PostgreSQL.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from app.domain.report_followup import (
    REASON_LIBRARY_UNAVAILABLE,
    REASON_MEMORY_CLOSED,
    REASON_REPORT_UNAVAILABLE,
    REASON_SCOPE_LIBRARIES,
    REASON_SCOPE_SOURCES,
    ReportFollowup,
)
from app.models.ask import AskRequest
from app.models.source_scope import BaseNotebookScope, SourceScope
from app.services.report_followup import current_report_followup
from app.services.source_scope import memory_access_context
from tests.report_reference_restore_cases import NOW, World, as_request_user, build_world

__all__ = ["SCENARIOS", "build_world"]

TITLE = "两种退火工艺的差异与依据"

CONTENT_MD = "\n".join([
    f"# {TITLE}",
    "",
    "## 执行摘要",
    "两种工艺在温度窗口上不同。",
    "",
    "## 资料基础",
    "本报告依据笔记本中的资料写成。",
    "",
    "## 工艺甲",
    "甲的温度窗口较窄 [k1]，其依据见 [k2, k4]。",
    "",
    "## 工艺乙",
    "乙沿用参考库的概念 [k3]，并结合个人记忆 [k5]。",
    "",
    "## 参考文献",
    "[k1] 甲段落",
])

SECTIONS = [{"title": "工艺甲"}, {"title": "工艺乙"}]


def _ref(key: str, object_type: str, object_id: str, *, source_id: str = "",
         element_id: str = "") -> dict:
    return {
        "key": key, "object_type": object_type, "object_id": object_id,
        "source_id": source_id, "element_id": element_id,
    }


#: One reference of every kind the restore reads back, plus the two kinds it
#: skips (a whole source and external material).
REFERENCES = [
    _ref("k1", "chunk", "c-a", source_id="src-a"),
    _ref("k2", "element", "el-b1", source_id="src-b", element_id="el-b1"),
    _ref("k3", "concept", "kg-lib", source_id="lib-src"),
    _ref("k4", "relation", "rel-a", source_id="src-a", element_id="el-a2"),
    _ref("k5", "memory", "mem-alice"),
    _ref("k6", "external", "https://example.org/paper"),
    _ref("k7", "source", "src-b", source_id="src-b"),
]


def insert_report(
    world: World,
    report_id: str,
    *,
    created_by: Optional[str] = None,
    notebook_id: Optional[str] = None,
    status: str = "done",
    references: Optional[list] = None,
    memory_used: tuple[str, ...] = (),
) -> str:
    """A stored deep report: Alice's finished report of ``nb`` by default."""
    world.execute(
        "INSERT INTO reports (id,notebook_id,question,outline_json,sections_json,"
        "references_json,content_md,status,created_by,understanding_json,"
        "created_at,updated_at) VALUES "
        "({ph},{ph},{ph},{js},{js},{js},{ph},{ph},{ph},{js},{ph},{ph})",
        (
            report_id, notebook_id or world.nb, TITLE,
            json.dumps(SECTIONS, ensure_ascii=False),
            json.dumps(SECTIONS, ensure_ascii=False),
            json.dumps(REFERENCES if references is None else references),
            CONTENT_MD, status, created_by or world.alice.id,
            json.dumps({"_memory_used": list(memory_used)}), NOW, NOW,
        ),
    )
    return report_id


def _service(world: World):
    return world.repo._runtime.ask_service()


def _payload(report_id: str, scopes: dict) -> AskRequest:
    source = scopes.get("source_scope")
    base = scopes.get("base_scope")
    return AskRequest(
        question="两种工艺的依据是什么？", mode="chunk", report_id=report_id,
        source_scope=SourceScope(**source) if source else None,
        base_scope=BaseNotebookScope(**base) if base else None,
    )


def engine_followup(world: World, report_id: str, user: Any = None,
                    **scopes: Any) -> Optional[ReportFollowup]:
    """What the engine reads: the run's ``ReportFollowup`` inside the very
    entry ``AskService.ask`` takes (ceiling, retrieval run, report)."""
    user = user or world.alice
    service = _service(world)
    payload = _payload(report_id, scopes)
    spec = service._resolve_ask_mode(payload.mode)
    with as_request_user(user), service._engine_scope(
        spec, world.nb, payload, user_id=user.id, job_id="",
        cancel_event=None, run=True,
    ):
        return current_report_followup()


def link_check(world: World, report_id: str, user: Any = None, **scopes: Any):
    user = user or world.alice
    payload = _payload(report_id, scopes)
    with as_request_user(user):
        return _service(world).report_link_check(
            world.nb, report_id, user_id=user.id, mode_supported=True,
            source_scope=payload.source_scope, base_scope=payload.base_scope,
        )


def judged(world: World, report_id: str, user: Any = None,
           **scopes: Any) -> ReportFollowup:
    """The run's followup, after asserting the pre-send check says exactly
    what the run does (one function, D4)."""
    followup = engine_followup(world, report_id, user, **scopes)
    assert followup is not None
    check = link_check(world, report_id, user, **scopes)
    assert (check.report_id, check.title, check.readable, check.reason_code,
            check.reason) == (
        followup.report_id, followup.title, followup.verdict.readable,
        followup.verdict.reason_code, followup.verdict.reason,
    )
    return followup


@contextmanager
def _closed_memory() -> Iterator[None]:
    with memory_access_context(False):
        yield


# ----------------------------------------------------------------- scenarios
def scenario_all_selected_reads_the_report_and_restores_it(world: World) -> None:
    """Every cited item is in the all-selected ceiling: the report is
    readable, and each item -- a passage, an element, a mounted library's
    knowledge object, a relation (as its evidence element), the asker's
    Memory -- is read back under the key the report gave it. The external
    and the whole-source references name no item to read back: neither is
    read, neither counts as gone. The run (and only the run) carries the
    parsed body."""
    report = insert_report(world, "rep-all")
    followup = judged(world, report)

    assert followup.title == TITLE
    assert followup.verdict.readable and followup.verdict.reason_code == ""
    assert followup.verdict.gone == 0
    assert followup.verdict.uses_memory
    assert followup.digest is not None
    assert followup.digest.section_titles == ("工艺甲", "工艺乙")
    assert followup.digest.summary == "两种工艺在温度窗口上不同。"
    assert followup.digest.sections[0].keys == ("k1", "k2", "k4")
    restored = followup.restored
    assert restored is not None
    assert dict(restored.key_by_object) == {
        ("chunk", "c-a"): ("k1",),
        ("element", "el-b1"): ("k2",),
        ("kg", "kg-lib"): ("k3",),
        ("element", "el-a2"): ("k4",),
        ("memory", "mem-alice"): ("k5",),
    }
    assert restored.gone == 0
    assert [hit.notebook_id for hit in restored.knowledge] == [world.lib]


def scenario_deleted_source_is_gone_not_a_conflict(world: World) -> None:
    """A source deleted since the report is no conflict: the report stays
    readable, the items it cited count as gone in the verdict (the
    whole-source reference to it does not: it names no item), and they are
    not sent to the restore (which would count them a second time)."""
    report = insert_report(world, "rep-deleted")
    for table in ("chunks", "source_elements", "knowledge_object_sources"):
        world.execute(f"DELETE FROM {table} WHERE source_id={{ph}}", ("src-b",))
    world.execute("DELETE FROM knowledge_objects WHERE id={ph}", ("kg-b",))
    world.execute("DELETE FROM sources WHERE id={ph}", ("src-b",))

    followup = judged(world, report)

    assert followup.verdict.readable
    assert followup.verdict.gone == 1  # k2 (an element of src-b)
    assert followup.restored is not None
    assert ("element", "el-b1") not in followup.restored.key_by_object
    assert followup.restored.gone == 0


def scenario_narrowed_sources_conflict(world: World) -> None:
    """A local scope that leaves out a cited source: ``scope_sources``,
    counted once per source (k2 and k7 both cite ``src-b``), nothing read
    back."""
    report = insert_report(world, "rep-narrowed")
    followup = judged(world, report, source_scope={
        "mode": "include", "source_ids": ["src-a"], "narrowed": True,
    })
    assert followup.verdict.reason_code == REASON_SCOPE_SOURCES
    assert followup.verdict.reason == (
        "本轮不读取这份报告：它引用的 1 份资料不在当前检索范围内"
    )
    assert followup.restored is None


def scenario_unticked_library_conflicts(world: World) -> None:
    """The reference library the report cites is mounted but unticked:
    ``scope_libraries``, naming it by the name it is mounted under."""
    report = insert_report(world, "rep-unticked")
    followup = judged(world, report, base_scope={
        "mode": "exclude", "notebook_ids": [world.lib], "narrowed": True,
    })
    assert followup.verdict.reason_code == REASON_SCOPE_LIBRARIES
    assert followup.verdict.reason == "本轮不读取这份报告：它引用的参考库《参考库》没有勾选"
    assert followup.restored is None


def scenario_unmounted_library_is_unavailable(world: World) -> None:
    """The library the report cites is no longer mounted: it is not one of
    the asker's participants, so ``library_unavailable``."""
    report = insert_report(world, "rep-unmounted")
    world.execute(
        "DELETE FROM notebook_bases WHERE notebook_id={ph} AND base_notebook_id={ph}",
        (world.nb, world.lib),
    )
    followup = judged(world, report)
    assert followup.verdict.reason_code == REASON_LIBRARY_UNAVAILABLE
    assert followup.verdict.reason == "本轮不读取这份报告：它引用的 1 个参考库本轮无法使用"
    assert followup.restored is None


def scenario_closed_memory_conflicts_in_both_wordings(world: World) -> None:
    """A report written from personal Memory: with the Memory channel closed
    for the caller (an Agent token without the read grant) and with the scope
    narrowed (which closes Memory for the turn), ``memory_closed`` -- each in
    its own wording."""
    report = insert_report(world, "rep-memory", memory_used=("mem-alice",))
    with _closed_memory():
        closed = judged(world, report)
    assert closed.verdict.reason_code == REASON_MEMORY_CLOSED
    assert closed.verdict.reason == (
        "本轮不读取这份报告：它用到了个人记忆，而本次提问不能读取个人记忆"
    )
    assert closed.restored is None

    narrowed = judged(world, report, source_scope={
        "mode": "include", "source_ids": ["src-a", "src-b"], "narrowed": True,
    })
    assert narrowed.verdict.reason_code == REASON_MEMORY_CLOSED
    assert narrowed.verdict.reason == (
        "本轮不读取这份报告：它用到了你的个人记忆，而收窄检索范围后本轮不读取个人记忆"
    )
    assert narrowed.restored is None


def scenario_reports_the_asker_may_not_follow_are_unavailable(world: World) -> None:
    """Bob's report, Alice's report of another notebook, her unfinished
    report and an unknown id all read as ``report_unavailable`` -- with no
    title, so the receipt never confirms that someone else's report exists."""
    insert_report(world, "rep-bob", created_by=world.bob.id)
    insert_report(world, "rep-lib", notebook_id=world.lib)
    insert_report(world, "rep-running", status="running")
    for report in ("rep-bob", "rep-lib", "rep-running", "rep-missing"):
        followup = judged(world, report)
        assert followup.verdict.reason_code == REASON_REPORT_UNAVAILABLE, report
        assert followup.title == "" and followup.digest is None, report
        assert followup.restored is None, report
    # Bob follows his own report.
    assert judged(world, "rep-bob", user=world.bob).title == TITLE


def scenario_no_report_installs_none(world: World) -> None:
    """A request without a report runs with no followup at all."""
    assert engine_followup(world, "") is None


def scenario_every_answer_to_a_report_question_stores_a_receipt(world: World) -> None:
    """End to end through the synchronous Ask surface (``repo.ask``): every
    answer of a request that attached a report is saved with its receipt --
    the runtime gate's ``report_unavailable`` for another member's report, the
    verdict's reason for a conflict, and ``no_synthesis`` for a readable report
    whose turn never called the answer model (none is configured here). A
    request without a report saves no receipt at all."""
    insert_report(world, "rep-e2e")
    insert_report(world, "rep-e2e-bob", created_by=world.bob.id)
    store = world.repo._runtime.ask_state

    def asked(report_id: str, **scopes: Any):
        response = world.repo.ask(world.nb, _payload(report_id, scopes))
        stored = store.ask_answer_detail(response.answer_id)["payload"]
        return response, stored.get("linked_report")

    with as_request_user(world.alice):
        for report_id, scopes, code, title in (
            ("rep-e2e", {}, "no_synthesis", TITLE),
            ("rep-e2e-bob", {}, REASON_REPORT_UNAVAILABLE, ""),
            ("rep-e2e", {"source_scope": {
                "mode": "include", "source_ids": ["src-a"], "narrowed": True,
            }}, REASON_SCOPE_SOURCES, TITLE),
        ):
            response, stored = asked(report_id, **scopes)
            receipt = response.linked_report
            assert (receipt.report_id, receipt.title, receipt.status,
                    receipt.reason_code) == (report_id, title, "skipped", code)
            assert stored == receipt.model_dump(mode="json")
        response, stored = asked("")
        assert response.linked_report is None and stored is None


SCENARIOS = (
    scenario_all_selected_reads_the_report_and_restores_it,
    scenario_deleted_source_is_gone_not_a_conflict,
    scenario_narrowed_sources_conflict,
    scenario_unticked_library_conflicts,
    scenario_unmounted_library_is_unavailable,
    scenario_closed_memory_conflicts_in_both_wordings,
    scenario_reports_the_asker_may_not_follow_are_unavailable,
    scenario_no_report_installs_none,
    scenario_every_answer_to_a_report_question_stores_a_receipt,
)
