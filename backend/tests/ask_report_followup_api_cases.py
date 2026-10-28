"""HTTP scenarios for asking about a deep report (plan 2026-10-21 T9), defined
once.

``test_ask_report_followup_api.py`` runs them on SQLite and
``postgres/test_ask_report_followup_api_pg.py`` on PostgreSQL, through the real
HTTP routes with real users (the world of ``report_share_disclosure_cases``: the
owner of a shared notebook, a member Alice who writes the reports and asks, and
the deployment admin).

Three surfaces:

* the report gate in front of ``/ask``, ``/ask/stream``, ``/ask/intent`` and
  ``/ask/intent/stream`` -- a report that is missing, another member's, another
  notebook's or unfinished is refused above any durable state, with one answer
  for "missing" and "not yours";
* the pre-send check ``POST /ask/report-check`` -- the same verdict the run
  gives, asked before sending, writing nothing;
* ``GET /reports/{id}/followup`` -- the scope a "follow up on this report" Ask
  starts from.

No model is configured, so an ``/ask`` that passes the gate answers
deterministically; what the scenarios read off it is the receipt the route
threaded through.
"""
from __future__ import annotations

import json
from dataclasses import replace
from itertools import count
from typing import Any, Callable

from tests.public_page_mount_recheck_cases import mount, mounted_library
from tests.report_share_disclosure_cases import (
    User,
    World,
    make_memory,
    make_report,
    memory_ref,
    source_ref,
)

NOT_FOUND = "找不到这份报告，它可能已被删除"
NOT_READY = "这份报告还没有生成完成，完成后才能就此追问"
EMPTY_SCOPE = "当前检索范围为空，请至少选择一个来源或挂载参考库"
NOTHING_TO_ANSWER_FROM = (
    "该笔记本还没有可用于回答的内容，请先添加来源，"
    "或在「笔记本设置」的「参考库」里挂载一个参考库。"
)
QUESTION = "这份报告第二节的依据是什么？"
_KEYS = count(1)
_DURABLE_TABLES = ("ask_jobs", "conversations", "answers")


# --- helpers -----------------------------------------------------------------


def ready(world: World) -> str:
    """Make the notebook answerable for Alice (a confirmed Memory of hers
    counts as retrievable content); returns the Memory id."""
    return make_memory(world, world.alice, "a1")


def extra_source(world: World, label: str) -> str:
    """Another visible document of the world's notebook."""
    source_id = f"src-extra-{label}-{next(_KEYS)}"
    world.repo._runtime.source_ingestion.sources.insert_source(
        source_id=source_id, notebook_id=world.notebook, title=f"补充 {label}",
        source_type="upload", status="active", parse_status="parsed",
        file_name=f"{label}.md", file_path="", file_size=1, file_hash="h",
        summary="", doc_type="",
    )
    return source_id


def library_text(world: World, library: str) -> None:
    """One visible document with one chunk in ``library``: evidence the
    federated passage lane reaches, so a ticked library holds up a retrieval
    scope (the empty-scope refusal judges reference libraries by evidence)."""
    from app.repositories.ports import ChunkWrite

    source_id = f"src-library-{next(_KEYS)}"
    world.repo._runtime.source_ingestion.sources.insert_source(
        source_id=source_id, notebook_id=library, title="参考原文",
        source_type="upload", status="active", parse_status="parsed",
        file_name="lib.md", file_path="", file_size=1, file_hash="h",
        summary="", doc_type="",
    )
    world.repo._runtime.chunk_store.replace_source_chunks(
        source_id, library,
        [ChunkWrite(id=f"chunk-{source_id}", text="参考原文", section_path="",
                    element_ids=())],
        created_at="2026-10-28T00:00:00+00:00",
    )


def durable_counts(world: World) -> dict[str, int]:
    """Row counts of everything an Ask turn leaves behind."""
    counts: dict[str, int] = {}
    with world.repo._runtime.database.connect() as db:
        for table in _DURABLE_TABLES:
            counts[table] = int(
                db.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
            )
    return counts


def notebook_path(world: World, tail: str) -> str:
    return f"/api/notebooks/{world.notebook}{tail}"


def entry_points(world: World, report_id: str) -> dict[str, tuple[str, dict]]:
    """Every Ask entry that carries ``report_id``: ``name -> (path, body)``."""
    scoped = {"question": QUESTION, "report_id": report_id}
    return {
        "ask": (notebook_path(world, "/ask"), {**scoped, "mode": "chunk"}),
        # The reasoning mode runs its follow-up preflight (before the
        # availability check and the report gate); a bad report must still be
        # refused by the gate behind it.
        "ask_reasoning": (notebook_path(world, "/ask"), {**scoped, "mode": "reasoning"}),
        "ask_stream": (
            notebook_path(world, "/ask/stream"),
            {**scoped, "mode": "chunk", "client_request_id": f"req-gate-{next(_KEYS)}"},
        ),
        "intent": (notebook_path(world, "/ask/intent"), scoped),
        "intent_stream": (notebook_path(world, "/ask/intent/stream"), scoped),
    }


def post(world: World, user: User, path: str, body: dict):
    return world.client.post(path, json=body, headers=user.headers)


def other_notebook_report(world: World) -> str:
    """Alice's finished report in a notebook of her own."""
    other = world.client.post(
        "/api/notebooks", json={"name": "我的另一本"}, headers=world.alice.headers
    ).json()["id"]
    return make_report(
        replace(world, notebook=other), world.alice, [source_ref(world.doc_source, "k1")]
    )


def scoped_report(world: World, *, source_scope: dict | None = None,
                  base_scope: dict | None = None) -> str:
    """Alice's finished report created through the API WITH a retrieval scope,
    so the stored ``understanding`` carries the frozen selection exactly as a
    scoped report does."""
    body: dict[str, Any] = {"question": "两种退火工艺的差异？"}
    if source_scope is not None:
        body["source_scope"] = source_scope
    if base_scope is not None:
        body["base_scope"] = base_scope
    created = world.client.post(
        notebook_path(world, "/reports"), json=body, headers=world.alice.headers
    )
    assert created.status_code == 200, created.text
    report_id = created.json()["report_id"]
    world.repo.update_report(world.notebook, report_id, status="outline_ready")
    assert world.repo.claim_report_generation(world.notebook, report_id)
    world.repo.update_report(
        world.notebook, report_id, status="done", progress="完成",
        content_md="# 结论\n\n环路稳定 [k1]。",
        references=[source_ref(world.doc_source, "k1")],
    )
    return report_id


def second_library(world: World) -> str:
    """A second library of the owner, readable by Alice (nothing is mounted)."""
    library = world.client.post(
        "/api/notebooks", json={"name": "第二参考库"}, headers=world.owner.headers
    ).json()["id"]
    world.repo._runtime.sharing.add_member(library, world.alice.id)
    return library


def followup(world: World, user: User, report_id: str):
    return world.client.get(
        notebook_path(world, f"/reports/{report_id}/followup"), headers=user.headers
    )


def check(world: World, report_id: str, **extra: Any):
    return post(world, world.alice, notebook_path(world, "/ask/report-check"),
                {"report_id": report_id, **extra})


def final_event(stream_text: str) -> dict:
    """The ``final`` frame of an NDJSON Ask stream."""
    frames = [json.loads(line) for line in stream_text.splitlines() if line.strip()]
    (final,) = [frame for frame in frames if frame.get("event") == "final"]
    return final["response"]


# --- the report gate in front of every Ask entry -----------------------------


def case_a_report_that_is_not_the_askers_is_refused_before_any_durable_state(
    world: World,
) -> None:
    """Missing, another member's and another notebook's report are ONE 404 (the
    gate is no oracle for "does this id exist"); the asker's own unfinished
    report is a 409. Every entry refuses with the same marked sentence, and a
    refused request leaves no job, conversation or answer behind."""
    ready(world)
    owners = make_report(world, world.owner, [source_ref(world.doc_source, "k1")])
    unfinished = make_report(world, world.alice, [], finished=False)
    refused = {
        "missing": ("rep-never-existed", 404, NOT_FOUND),
        "another member's": (owners, 404, NOT_FOUND),
        "another notebook's": (other_notebook_report(world), 404, NOT_FOUND),
        "unfinished": (unfinished, 409, NOT_READY),
    }
    before = durable_counts(world)
    for label, (report_id, status, message) in refused.items():
        for name, (path, body) in entry_points(world, report_id).items():
            response = post(world, world.alice, path, body)
            where = f"{label} via {name}"
            assert response.status_code == status, (where, response.text)
            assert response.json() == {"detail": message}, where
            assert response.headers.get("X-User-Message") == "1", where
    assert durable_counts(world) == before


def case_the_gate_runs_after_availability_and_scope_checks(world: World) -> None:
    """The gate sits after ``_require_ask_available``: an empty scope is the
    scope's 409 even with a bad report attached, and a notebook with nothing to
    answer from is the availability 409 -- the report never decides first."""
    ready(world)
    empty = {"source_scope": {"mode": "include", "source_ids": []}}
    for name, (path, body) in entry_points(world, "rep-never-existed").items():
        response = post(world, world.alice, path, {**body, **empty})
        assert response.status_code == 409, (name, response.text)
        assert response.json() == {"detail": EMPTY_SCOPE}, name

    bare = world.client.post(
        "/api/notebooks", json={"name": "空笔记本"}, headers=world.alice.headers
    ).json()["id"]
    for name, (path, body) in entry_points(replace(world, notebook=bare),
                                           "rep-never-existed").items():
        response = post(world, world.alice, path, body)
        assert response.status_code == 409, (name, response.text)
        assert response.json() == {"detail": NOTHING_TO_ANSWER_FROM}, name


def case_a_finished_own_report_passes_the_gate_and_reaches_the_receipt(
    world: World,
) -> None:
    """The route threads ``report_id`` to the engine: ``/ask`` and the stream
    answer with a receipt that names the report, and the intent previews hand
    it to the understanding step (``preview_reasoning_intent(report_id=...)``)."""
    ready(world)
    report_id = make_report(world, world.alice, [source_ref(world.doc_source, "k1")])
    seen: list[str | None] = []
    original = world.repo.preview_reasoning_intent

    def spy(*args: Any, **kwargs: Any):
        seen.append(kwargs.get("report_id"))
        return original(*args, **kwargs)

    world.monkeypatch.setattr(world.repo, "preview_reasoning_intent", spy)
    points = entry_points(world, report_id)

    for name in ("ask", "ask_reasoning"):
        asked = post(world, world.alice, *points[name])
        assert asked.status_code == 200, (name, asked.text)
        assert asked.json()["linked_report"]["report_id"] == report_id, name

    streamed = post(world, world.alice, *points["ask_stream"])
    assert streamed.status_code == 200, streamed.text
    assert final_event(streamed.text)["linked_report"]["report_id"] == report_id

    for name in ("intent", "intent_stream"):
        previewed = post(world, world.alice, *points[name])
        assert previewed.status_code == 200, (name, previewed.text)
    assert seen == [report_id, report_id]

    # A request without a report is untouched: no receipt, no key.
    plain = post(world, world.alice, notebook_path(world, "/ask"),
                 {"question": QUESTION, "mode": "chunk"})
    assert plain.status_code == 200 and "linked_report" not in plain.json()


def case_a_blank_report_id_is_no_report(world: World) -> None:
    ready(world)
    response = post(world, world.alice, notebook_path(world, "/ask"),
                    {"question": QUESTION, "mode": "chunk", "report_id": "   "})
    assert response.status_code == 200, response.text
    assert "linked_report" not in response.json()


def case_a_keyed_reattach_does_not_run_the_report_gate(world: World) -> None:
    """A keyed re-submission attaches to the job its key already created BEFORE
    the gate: the original submission passed it, and the retry must replay that
    job even though the report has since been deleted. A fresh key with the
    deleted report is refused as any other missing report."""
    ready(world)
    report_id = make_report(world, world.alice, [source_ref(world.doc_source, "k1")])
    path = notebook_path(world, "/ask/stream")
    body = {"question": QUESTION, "mode": "chunk", "report_id": report_id,
            "client_request_id": f"req-keyed-{next(_KEYS)}"}
    first = post(world, world.alice, path, body)
    assert first.status_code == 200, first.text
    receipt = final_event(first.text)["linked_report"]
    assert receipt["report_id"] == report_id

    deleted = world.client.delete(
        notebook_path(world, f"/reports/{report_id}"), headers=world.alice.headers
    )
    assert deleted.status_code == 200, deleted.text
    jobs = durable_counts(world)["ask_jobs"]

    replay = post(world, world.alice, path, body)
    assert replay.status_code == 200, replay.text
    assert final_event(replay.text)["linked_report"] == receipt
    assert durable_counts(world)["ask_jobs"] == jobs

    fresh = post(world, world.alice, path,
                 {**body, "client_request_id": f"req-keyed-{next(_KEYS)}"})
    assert fresh.status_code == 404 and fresh.json() == {"detail": NOT_FOUND}
    assert durable_counts(world)["ask_jobs"] == jobs


# --- the pre-send check -------------------------------------------------------


def case_check_says_readable_for_a_report_inside_the_scope(world: World) -> None:
    ready(world)
    report_id = make_report(world, world.alice, [source_ref(world.doc_source, "k1")])
    response = check(world, report_id)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "report_id": report_id, "title": "环路为什么稳定？", "readable": True,
        "reason_code": "", "reason": "",
    }
    # The reasoning mode reads reports too.
    assert check(world, report_id, mode="reasoning").json()["readable"] is True


def case_check_refuses_the_reports_the_run_would_refuse(world: World) -> None:
    ready(world)
    owners = make_report(world, world.owner, [source_ref(world.doc_source, "k1")])
    unfinished = make_report(world, world.alice, [], finished=False)
    for report_id, status, message in (
        ("rep-never-existed", 404, NOT_FOUND),
        (owners, 404, NOT_FOUND),
        (other_notebook_report(world), 404, NOT_FOUND),
        (unfinished, 409, NOT_READY),
    ):
        response = check(world, report_id)
        assert response.status_code == status, response.text
        assert response.json() == {"detail": message}
        assert response.headers.get("X-User-Message") == "1"


def case_check_names_each_reason_a_turn_would_not_read_the_report(world: World) -> None:
    """The four scope conflicts, one report each, and each verdict is exactly
    what ``/ask`` then writes into the turn's receipt (one function, three
    callers)."""
    memory_id = ready(world)
    kept = extra_source(world, "kept")
    inside = make_report(world, world.alice, [source_ref(world.doc_source, "k1")])
    library, library_source = mounted_library(world)
    in_library = make_report(world, world.alice, [source_ref(library_source, "k1")])
    with_memory = make_report(world, world.alice, [
        source_ref(world.doc_source, "k1"), memory_ref(memory_id, "k2"),
    ])

    def both(report_id: str, **scopes: Any) -> tuple[dict, dict]:
        verdict = check(world, report_id, **scopes)
        assert verdict.status_code == 200, verdict.text
        asked = post(world, world.alice, notebook_path(world, "/ask"),
                     {"question": QUESTION, "mode": "chunk",
                      "report_id": report_id, **scopes})
        assert asked.status_code == 200, asked.text
        return verdict.json(), asked.json()["linked_report"]

    cases = (
        # The report's passage is not among the ticked sources.
        ("scope_sources", inside, {"source_scope": {"mode": "include", "source_ids": [kept]}},
         "本轮不读取这份报告：它引用的 1 份资料不在当前检索范围内"),
        # The library it cites is mounted but unticked.
        ("scope_libraries", in_library,
         {"base_scope": {"mode": "include", "notebook_ids": []}},
         "本轮不读取这份报告：它引用的参考库《参考库》没有勾选"),
        # Narrowing the sources closes the personal-memory channel.
        ("memory_closed", with_memory,
         {"source_scope": {"mode": "include", "source_ids": [world.doc_source]}},
         "本轮不读取这份报告：它用到了你的个人记忆，而收窄检索范围后本轮不读取个人记忆"),
    )
    for code, report_id, scopes, reason in cases:
        verdict, receipt = both(report_id, **scopes)
        assert verdict["readable"] is False, code
        assert (verdict["reason_code"], verdict["reason"]) == (code, reason)
        assert receipt["status"] == "skipped"
        assert (receipt["reason_code"], receipt["reason"]) == (code, reason)
    # The library is no longer mounted: it cannot be used this turn at all.
    mount(world, [])
    verdict, receipt = both(in_library)
    reason = "本轮不读取这份报告：它引用的 1 个参考库本轮无法使用"
    assert (verdict["reason_code"], verdict["reason"]) == ("library_unavailable", reason)
    assert (receipt["reason_code"], receipt["reason"]) == ("library_unavailable", reason)
    # And an untouched scope reads it again.
    mount(world, [library])
    assert both(inside)[0]["readable"] is True


def case_check_for_an_extension_engine_is_engine_unsupported(world: World) -> None:
    """An ``ask.engine`` mode never reads a report: the check says so after the
    report row alone, and the question is not refused (the tag stays, grey)."""
    import app.api.ask_routes as routes
    from app.domain.ask import AskMode

    ready(world)
    report_id = make_report(world, world.alice, [source_ref(world.doc_source, "k1")])
    extension = AskMode("ext.test", "ask_extension", "extension", True, False, True)
    world.monkeypatch.setattr(routes, "_extension_ask_modes", lambda: (extension,))
    response = check(world, report_id, mode="ext.test")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "report_id": report_id, "title": "环路为什么稳定？", "readable": False,
        "reason_code": "engine_unsupported",
        "reason": "本轮不读取这份报告：当前回答方式不读取关联报告，请改用「通用问答」或「逐步推理」",
    }


def case_check_validates_like_ask(world: World) -> None:
    """The same preconditions as ``/ask``, with the same bodies: an unknown
    mode and an empty scope; and a check names its report (blank or overlong
    is a 422, there is no "no report" reading here)."""
    ready(world)
    report_id = make_report(world, world.alice, [source_ref(world.doc_source, "k1")])
    unknown = check(world, report_id, mode="no-such-mode")
    asked = post(world, world.alice, notebook_path(world, "/ask"),
                 {"question": QUESTION, "mode": "no-such-mode", "report_id": report_id})
    assert unknown.status_code == asked.status_code == 422
    assert unknown.json() == asked.json()

    empty = {"source_scope": {"mode": "include", "source_ids": []}}
    refused = check(world, report_id, **empty)
    assert refused.status_code == 409 and refused.json() == {"detail": EMPTY_SCOPE}
    assert refused.headers.get("X-User-Message") == "1"

    assert check(world, "").status_code == 422          # a check names its report
    assert check(world, "r" * 201).status_code == 422


def case_check_does_not_read_the_report_before_the_verdict(world: World) -> None:
    """The report row is read where the verdict is taken (the Ask service, one
    gate), not once more in front of it: the route touches the report itself
    only to tell a report that is not available apart (404 against 409)."""
    ready(world)
    report_id = make_report(world, world.alice, [source_ref(world.doc_source, "k1")])
    unfinished = make_report(world, world.alice, [], finished=False)
    reads: list[str] = []
    original = world.repo.get_report

    def spy(notebook_id: str, report: str):
        reads.append(report)
        return original(notebook_id, report)

    world.monkeypatch.setattr(world.repo, "get_report", spy)
    assert check(world, report_id).json()["readable"] is True
    assert reads == []
    assert check(world, report_id, mode="reasoning").json()["readable"] is True
    assert reads == []
    # Not available: the service's verdict says so, the route names which.
    for report, status in (("rep-never-existed", 404), (unfinished, 409)):
        assert check(world, report).status_code == status
        assert reads[-1] == report
    assert len(reads) == 2


def case_check_writes_nothing(world: World) -> None:
    ready(world)
    report_id = make_report(world, world.alice, [source_ref(world.doc_source, "k1")])
    row = world.repo.get_report(world.notebook, report_id)
    before = durable_counts(world)
    for scopes in ({}, {"source_scope": {"mode": "include", "source_ids": []}},
                   {"mode": "reasoning"}):
        check(world, report_id, **scopes)
    assert durable_counts(world) == before
    assert world.repo.get_report(world.notebook, report_id) == row


# --- the follow-up scope -----------------------------------------------------


def case_followup_of_an_unscoped_report_is_everything(world: World) -> None:
    report_id = make_report(world, world.alice, [source_ref(world.doc_source, "k1")])
    response = followup(world, world.alice, report_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["report_id"] == report_id and body["title"] == "环路为什么稳定？"
    assert (body["source_scope"]["mode"], body["source_scope"]["source_ids"]) == ("exclude", [])
    assert (body["base_scope"]["mode"], body["base_scope"]["notebook_ids"]) == ("exclude", [])
    assert (body["dropped_sources"], body["dropped_libraries"], body["fell_back_to_all"]) == (
        0, 0, False)


def case_followup_of_an_all_selected_report_is_everything(world: World) -> None:
    """The browser's default all-selected request freezes into an include list
    that narrows nothing; that is "everything", not a selection to restore."""
    sources = [world.doc_source, extra_source(world, "b")]
    report_id = scoped_report(
        world, source_scope={"mode": "exclude", "source_ids": []},
        base_scope={"mode": "exclude", "notebook_ids": []},
    )
    stored = world.repo.get_report(world.notebook, report_id)["understanding"]
    assert stored["source_scope"]["narrowed"] is False
    assert sorted(stored["source_scope"]["source_ids"]) == sorted(sources)
    body = followup(world, world.alice, report_id).json()
    assert body["source_scope"]["mode"] == "exclude" and body["source_scope"]["source_ids"] == []
    assert body["base_scope"]["mode"] == "exclude" and body["base_scope"]["notebook_ids"] == []
    assert body["dropped_sources"] == 0 and body["fell_back_to_all"] is False


def case_followup_restores_the_narrowed_sources_that_still_exist(world: World) -> None:
    kept, gone = extra_source(world, "kept"), extra_source(world, "gone")
    extra_source(world, "never-ticked")
    report_id = scoped_report(
        world, source_scope={"mode": "include", "source_ids": [kept, gone]})
    deleted = world.client.delete(f"/api/sources/{gone}", headers=world.owner.headers)
    assert deleted.status_code == 204, deleted.text
    body = followup(world, world.alice, report_id).json()
    assert body["source_scope"]["mode"] == "include"
    assert body["source_scope"]["source_ids"] == [kept]
    assert body["base_scope"]["mode"] == "exclude" and body["base_scope"]["notebook_ids"] == []
    assert (body["dropped_sources"], body["dropped_libraries"], body["fell_back_to_all"]) == (
        1, 0, False)


def case_followup_restores_the_narrowed_libraries_that_are_still_mounted(
    world: World,
) -> None:
    first, _source = mounted_library(world)
    second = second_library(world)
    mount(world, [first, second])
    report_id = scoped_report(
        world, base_scope={"mode": "include", "notebook_ids": [first, second]},
    )
    # Both libraries ticked is "all libraries": nothing narrowed yet. Narrow it.
    narrowed = scoped_report(world, base_scope={"mode": "include", "notebook_ids": [first]})
    stored = world.repo.get_report(world.notebook, narrowed)["understanding"]
    assert stored["base_scope"]["narrowed"] is True
    body = followup(world, world.alice, narrowed).json()
    assert body["base_scope"] == {"mode": "include", "notebook_ids": [first], "narrowed": None}
    assert body["dropped_libraries"] == 0 and body["fell_back_to_all"] is False
    assert followup(world, world.alice, report_id).json()["base_scope"]["mode"] == "exclude"

    # The ticked library is unmounted: the local dimension is whole, so the
    # selection that remains is "no library" -- not a fallback to everything.
    mount(world, [second])
    body = followup(world, world.alice, narrowed).json()
    assert body["base_scope"]["mode"] == "include" and body["base_scope"]["notebook_ids"] == []
    assert body["dropped_libraries"] == 1 and body["fell_back_to_all"] is False


def case_followup_falls_back_to_everything_when_nothing_selected_is_left(
    world: World,
) -> None:
    library, _source = mounted_library(world)
    second = second_library(world)
    mount(world, [library, second])
    kept = extra_source(world, "kept")
    extra_source(world, "never-ticked")
    report_id = scoped_report(
        world,
        source_scope={"mode": "include", "source_ids": [kept]},
        base_scope={"mode": "include", "notebook_ids": [library]},
    )
    assert world.client.delete(f"/api/sources/{kept}", headers=world.owner.headers
                               ).status_code == 204
    mount(world, [second])
    body = followup(world, world.alice, report_id).json()
    assert body["fell_back_to_all"] is True
    assert body["source_scope"]["mode"] == "exclude" and body["source_scope"]["source_ids"] == []
    assert body["base_scope"]["mode"] == "exclude" and body["base_scope"]["notebook_ids"] == []
    # What was lost is still counted, so the tag can say so.
    assert (body["dropped_sources"], body["dropped_libraries"]) == (1, 1)


def case_followup_falls_back_when_a_library_only_notebook_loses_its_ticked_library(
    world: World,
) -> None:
    """A notebook that holds nothing itself and only mounts libraries has no
    local evidence to stand on: the local dimension is not narrowed, but once
    the one ticked library is unmounted the reduced selection is empty by the
    very rule ``/ask`` applies, so the scope goes back to everything instead of
    handing over a selection the Ask box refuses."""
    headers = world.alice.headers
    create = lambda name: world.client.post(  # noqa: E731 - three one-line creates
        "/api/notebooks", json={"name": name}, headers=headers).json()["id"]
    bare, first, second = create("只挂参考库"), create("库甲"), create("库乙")
    library_text(world, first)
    library_text(world, second)
    local = replace(world, notebook=bare)

    def mount_for_alice(libraries: list[str]) -> None:
        put = world.client.put(f"/api/notebooks/{bare}/bases",
                               json={"base_notebook_ids": libraries}, headers=headers)
        assert put.status_code == 200, put.text

    mount_for_alice([first, second])
    report_id = scoped_report(
        local, base_scope={"mode": "include", "notebook_ids": [first]})
    assert world.repo.get_report(bare, report_id)["understanding"]["base_scope"][
        "narrowed"] is True
    body = followup(local, world.alice, report_id).json()
    assert body["base_scope"]["notebook_ids"] == [first]
    assert body["fell_back_to_all"] is False

    mount_for_alice([second])
    body = followup(local, world.alice, report_id).json()
    assert body["fell_back_to_all"] is True
    assert body["source_scope"]["mode"] == "exclude" and body["source_scope"]["source_ids"] == []
    assert body["base_scope"]["mode"] == "exclude" and body["base_scope"]["notebook_ids"] == []
    assert body["dropped_libraries"] == 1


def case_followup_is_the_owners_and_finished_only(world: World) -> None:
    alices = make_report(world, world.alice, [source_ref(world.doc_source, "k1")])
    unfinished = make_report(world, world.alice, [], finished=False)
    other = other_notebook_report(world)
    for user, report_id, status in (
        (world.owner, alices, 404),                    # the notebook owner included
        (world.alice, "rep-never-existed", 404),
        (world.alice, other, 404),
        (world.alice, unfinished, 409),
    ):
        response = followup(world, user, report_id)
        assert response.status_code == status, (report_id, response.text)
    refused = followup(world, world.alice, unfinished)
    assert refused.json() == {"detail": NOT_READY}
    assert refused.headers.get("X-User-Message") == "1"
    # The 404 is the report routes' own, the same as for any other report read.
    assert followup(world, world.owner, alices).json() == world.client.get(
        notebook_path(world, f"/reports/{alices}"), headers=world.owner.headers
    ).json()


CASES: dict[str, Callable[[World], None]] = {
    name: case for name, case in globals().items()
    if name.startswith("case_") and callable(case)
}
