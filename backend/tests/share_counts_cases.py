"""The share dialog's exact numbers for a GLOBAL conversation
(``GET /global-ask/conversations/{id}/share/counts``), defined once.

``test_global_share_counts.py`` runs every case on SQLite and
``postgres/test_global_share_counts_pg.py`` runs them, unchanged, on
PostgreSQL -- through the real HTTP routes, with real users (the world of
``report_share_disclosure_cases``) and jobs written directly in each backend's
dialect through the global store's ``_sql``.

Three kinds of case:

* the shared golden (``fixtures/share_counts_golden.json``): the endpoint
  must answer exactly what the browser's ``shareCountsFromTurns`` computes for
  every scenario (``frontend/tests/unit/share-counts-golden.test.mjs`` holds
  the browser to the same numbers);
* the differential: in a conversation longer than ``MAX_TURNS``, every number
  equals what the public page actually serves -- the page fetched anonymously
  and the conversation paged out in full the way the browser reads it, counted
  independently here;
* authority: a library the sharer lost on a turn the page shows refuses the
  count (and the share), one on a turn the page does not show refuses nothing.
"""
from __future__ import annotations

import json
from itertools import count
from pathlib import Path
from typing import Any, Callable

from app.domain.conversation_public_view import MAX_TURNS
from tests.global_ask_share_cases import insert_conversation
from tests.report_share_disclosure_cases import World

GOLDEN = json.loads(
    (Path(__file__).parent / "fixtures" / "share_counts_golden.json").read_text("utf-8")
)
LOST = {"detail": "部分笔记本已无法访问，请重新选择范围。"}
_IDS = count(1)


# --- seeding -------------------------------------------------------------------


def instant(index: int, ties: list | None = None) -> str:
    seconds = index
    for start, end in ties or ():
        if start < index <= end:
            seconds = start
    return (f"2026-01-01T{seconds // 3600:02d}:{seconds // 60 % 60:02d}:"
            f"{seconds % 60:02d}+00:00")


def turn_id(index: int) -> str:
    return f"t{index:04d}"


def bulk_jobs(world: World, conversation_id: str, user_id: str,
              rows: list[tuple[str, str, str, dict]]) -> None:
    """``(job id, created_at, status, payload)`` rows in ONE write transaction."""
    store = world.repo._runtime.global_ask_store
    statement = store._sql(
        "INSERT INTO global_ask_jobs(id,conversation_id,user_id,client_request_id,"
        "request_json,status,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?)"
    )
    with store.database.write() as db:
        for job_id, created_at, status, payload in rows:
            stored = {"conversation_id": conversation_id, "created_at": created_at, **payload}
            db.execute(statement, (job_id, conversation_id, user_id, None, "{}", status,
                                   json.dumps(stored, ensure_ascii=False), created_at))


def job_payload(job_id: str, question: str, *, anchors=(), citations=(),
                resolved=(), legacy: bool = False, status: str = "done") -> dict:
    answer = {
        "conclusion": "结论 [k1]。", "answer": "结论 [k1]。", "evidence_level": "grounded",
        "anchors": list(anchors), "citations": list(citations),
    }
    if legacy:
        # The pre-engine ``response`` shape (``GlobalAskResponse``) carries its own run facts.
        answer.update({
            "answer_id": job_id, "question": question, "created_at": "2026-01-01T00:00:00+00:00",
            "notebook_scope": {"mode": "all", "notebook_ids": []},
            "resolved_notebook_ids": list(resolved), "searched_notebook_ids": list(resolved),
            "cited_notebook_ids": list(resolved)[:1],
        })
    return {
        "job_id": job_id, "status": status, "question": question,
        "notebook_scope": {"mode": "all", "notebook_ids": []},
        "resolved_notebook_ids": list(resolved), "cited_notebook_ids": list(resolved)[:1],
        "searched_notebook_ids": list(resolved),
        ("response" if legacy else "answer"): answer,
    }


def imaged(asset_ids, *, notebook_id: str = "", kind: str = "anchor") -> list[dict]:
    """One anchor (or citation) per asset id, each carrying that one image,
    in the stored ``AnswerAnchor`` / ``Citation`` shape."""
    items = []
    for index, asset_id in enumerate(asset_ids):
        image = {"element_id": f"el-{asset_id}", "asset_id": asset_id}
        common = {"label": "图", "location_label": "第 1 页", "source_id": f"src-{asset_id}",
                  "element_id": f"el-{asset_id}", "images": [image]}
        item = ({**common, "key": f"k{index + 1}", "object_id": f"el-{asset_id}",
                 "object_type": "element", "name": "图", "snippet": "摘录",
                 "source_title": "资料", "tier": "personal"}
                if kind == "anchor" else {**common, "quoted_span": "摘录"})
        if notebook_id:
            item["notebook_id"] = notebook_id
        items.append(item)
    return items


def seed_golden(world: World, spec: dict) -> str:
    """One conversation of Alice's expanded from a golden scenario's spec."""
    cid = f"gconv-sc-{next(_IDS)}"
    store = world.repo._runtime.global_ask_store
    insert_conversation(store, cid, world.alice.id)
    legacy = set(spec.get("legacy") or ())
    failed = set(spec.get("failed_after") or ())
    rows = []
    for index in range(spec["turns"]):
        images = spec["images"].get(str(index)) or {"anchors": [], "citations": []}
        # Job ids are global: the golden's ``t0003`` is ``<conversation>-t0003`` here.
        job_id = f"{cid}-{turn_id(index)}"
        rows.append((job_id, instant(index, spec.get("ties")), "done", job_payload(
            job_id, f"问题 {index}", anchors=imaged(images["anchors"]),
            citations=imaged(images["citations"], kind="citation"), legacy=index in legacy,
        )))
        if index in failed:
            rows.append((f"{job_id}-f", instant(index, spec.get("ties")), "failed",
                         job_payload(f"{job_id}-f", "失败的一轮", status="failed")))
    bulk_jobs(world, cid, world.alice.id, rows)
    return cid


def golden_query(scenario: dict, cid: str) -> dict:
    through = scenario["through"]
    mark = scenario["watermark"]
    query = {"through_id": "" if through is None else
             f"{cid}-t-missing" if through == "missing" else f"{cid}-{turn_id(through)}",
             "watermark_id": "", "watermark_at": ""}
    if isinstance(mark, int):
        query.update(watermark_id=f"{cid}-{turn_id(mark)}",
                     watermark_at=instant(mark, scenario.get("ties")))
    elif mark:
        query.update(watermark_id=f"{cid}-t-gone",
                     watermark_at=instant(mark["gone_at"], scenario.get("ties")))
    return query


def counts_path(cid: str) -> str:
    return f"/api/global-ask/conversations/{cid}/share/counts"


def share_counts(world: World, cid: str, **query):
    return world.client.get(counts_path(cid), params=query, headers=world.alice.headers)


def share(world: World, cid: str, through_id: str):
    return world.client.post(f"/api/global-ask/conversations/{cid}/share",
                             json={"expected_through_id": through_id},
                             headers=world.alice.headers)


def stored_counts(world: World, cid: str, **query):
    """The endpoint asked with the link's watermark exactly as stored."""
    state = world.repo._runtime.global_ask_store.conversation_share_state(cid, world.alice.id)
    return share_counts(world, cid, watermark_id=state["shared_through_id"],
                        watermark_at=state["shared_through_at"], **query)


# --- the golden ------------------------------------------------------------------


def case_every_golden_scenario_matches_the_browser_algorithm(world: World) -> None:
    conversations: dict[str, str] = {}
    mismatches = []
    for scenario in GOLDEN["scenarios"]:
        spec = {key: scenario.get(key) for key in
                ("turns", "images", "legacy", "failed_after", "ties")}
        key = json.dumps(spec, sort_keys=True)
        if key not in conversations:
            conversations[key] = seed_golden(world, spec)
        cid = conversations[key]
        query = golden_query(scenario, cid)
        expected = dict(scenario["expected"])
        if expected["through_id"]:
            expected["through_id"] = f"{cid}-{expected['through_id']}"
        # The body echoes the watermark it counted against.
        expected.update(watermark_id=query["watermark_id"], watermark_at=query["watermark_at"])
        response = share_counts(world, cid, **query)
        if response.status_code != 200 or response.json() != expected:
            mismatches.append((scenario["name"], response.status_code, response.text))
    assert mismatches == []


# --- the differential over a conversation longer than MAX_TURNS -----------------


def _long_world(world: World, turns: int, *, lost_at: tuple[int, ...] = ()) -> tuple[str, str]:
    """Alice's conversation of ``turns`` completed jobs over the world's notebook
    (a failed job every 97 turns), images on every seventh turn -- shared both
    as anchors and citations on every fourteenth (deduplicated per turn) --
    and ``lost_at`` turns quoting a library Alice is then removed from.
    Returns ``(conversation id, lost notebook id)``."""
    lost = world.client.post(
        "/api/notebooks", json={"name": f"撤权库 {next(_IDS)}"}, headers=world.owner.headers
    ).json()["id"]
    world.repo._runtime.sharing.add_member(lost, world.alice.id)
    cid = f"gconv-long-{next(_IDS)}"
    insert_conversation(world.repo._runtime.global_ask_store, cid, world.alice.id)
    rows = []
    for index in range(turns):
        library = lost if index in lost_at else world.notebook
        assets = [f"img-{index}"] if index % 7 == 0 else []
        job_id = job_of(cid, index)
        rows.append((job_id, instant(index), "done", job_payload(
            job_id, f"第 {index} 问", resolved=[library],
            anchors=imaged(assets, notebook_id=library),
            citations=imaged(assets if index % 14 == 0 else [], notebook_id=library,
                             kind="citation"),
            legacy=index % 31 == 0,
        )))
        if index % 97 == 0:
            rows.append((f"{job_id}-f", instant(index), "failed",
                         job_payload(f"{job_id}-f", "失败", status="failed")))
    bulk_jobs(world, cid, world.alice.id, rows)
    world.repo._runtime.sharing.remove_member(lost, world.alice.id)
    return cid, lost


def _browser_turns(world: World, cid: str) -> list[dict]:
    """Every completed turn, paged out the way the browser reads the
    conversation (newest page first), oldest first."""
    pages, offset = [], 0
    while True:
        detail = world.client.get(
            f"/api/global-ask/conversations/{cid}",
            params={"limit": 50, "offset": offset}, headers=world.alice.headers,
        ).json()
        pages.append(detail["turns"])
        if not detail["has_more"] or detail["next_offset"] is None:
            break
        offset = detail["next_offset"]
    return [job for page in reversed(pages) for job in page if job["status"] == "done"]


def _all_done(world: World, cid: str) -> list[str]:
    """Every completed turn's question, oldest first, read straight off the
    table in the canonical order (works whatever the owner may read now)."""
    store = world.repo._runtime.global_ask_store
    with store.database.connect() as db:
        rows = db.execute(store._sql(
            "SELECT payload_json FROM global_ask_jobs WHERE conversation_id=? "
            "AND status='done' ORDER BY created_at, id"
        ), (cid,)).fetchall()
    return [json.loads(row["payload_json"])["question"] for row in rows]


def job_of(cid: str, index: int) -> str:
    return f"{cid}-j{index:04d}"


def _stored_images(world: World, cid: str) -> dict[str, int]:
    """Per question: each turn's anchor + citation images, deduplicated."""
    store = world.repo._runtime.global_ask_store
    with store.database.connect() as db:
        rows = db.execute(store._sql(
            "SELECT payload_json FROM global_ask_jobs WHERE conversation_id=? AND status='done'"
        ), (cid,)).fetchall()
    found = {}
    for row in rows:
        payload = json.loads(row["payload_json"])
        answer = payload.get("answer") or payload.get("response") or {}
        assets = {image["asset_id"] for key in ("anchors", "citations")
                  for item in answer.get(key) or () for image in item.get("images") or ()}
        found[payload["question"]] = len(assets)
    return found


def _page(world: World, cid: str) -> dict:
    state = world.repo._runtime.global_ask_store.conversation_share_state(cid, world.alice.id)
    response = world.client.get(f"/api/public/conversations/{state['share_token']}")
    assert response.status_code == 200, response.text
    return response.json()


def _assert_counts_describe_the_page(world: World, cid: str, counted: dict, *,
                                     bounded: bool = False) -> None:
    """Every number of ``counted`` (asked with the stored watermark) against the
    anonymous page and the whole conversation, counted here independently."""
    page = _page(world, cid)
    questions = _all_done(world, cid)
    shown = [turn["question"] for turn in page["turns"]]
    images = _stored_images(world, cid)
    end = questions.index(shown[-1]) + 1
    assert counted["total_turns"] == len(questions)
    assert counted["published_turns"] == len(shown) == min(end, MAX_TURNS)
    assert shown == questions[end - len(shown):end]
    assert counted["earlier_turns"] == page.get("omitted_earlier_turns", 0) == end - len(shown)
    assert page["truncated_turns"] is (counted["earlier_turns"] > 0)
    assert counted["new_turns"] == (0 if bounded else len(questions) - end)
    assert counted["image_count"] == sum(images[question] for question in shown)


def case_a_long_conversation_counts_exactly_what_the_page_serves(world: World) -> None:
    turns = MAX_TURNS + 60
    cid, _lost = _long_world(world, turns)
    browser = _browser_turns(world, cid)
    # The browser pages out exactly the turns the table holds: the reference
    # below (``_all_done``) is the conversation as the user reads it.
    assert [job["question"] for job in browser] == _all_done(world, cid)
    boundary = job_of(cid, MAX_TURNS + 20)

    # Not shared yet: the dialog's numbers describe what sharing would publish.
    preview = share_counts(world, cid, through_id=boundary).json()
    assert preview["boundary_index"] == MAX_TURNS + 20
    assert preview["earlier_turns"] == preview["update_earlier_turns"] == 21
    assert preview["published_turns"] == MAX_TURNS

    assert share(world, cid, boundary).status_code == 200
    counted = stored_counts(world, cid).json()
    _assert_counts_describe_the_page(world, cid, counted)
    assert counted["new_turns"] == 39 and counted["earlier_turns"] == 21
    # The bounded view of the same link agrees: the link stands at the boundary.
    at = stored_counts(world, cid, through_id=boundary).json()
    assert (at["watermark_ahead"], at["watermark_behind"]) == (False, False)
    _assert_counts_describe_the_page(world, cid, at, bounded=True)

    # Update to the newest: the update preview is exactly the page afterwards.
    newest = counted["through_id"]
    assert newest == browser[-1]["job_id"] == job_of(cid, turns - 1)
    assert share(world, cid, newest).status_code == 200
    after = stored_counts(world, cid).json()
    _assert_counts_describe_the_page(world, cid, after)
    assert after["image_count"] == counted["update_image_count"]
    assert after["earlier_turns"] == counted["update_earlier_turns"] == turns - MAX_TURNS
    assert after["new_turns"] == 0
    # What the update newly showed: the images on turns past the old window.
    old_shown = set(range(21, MAX_TURNS + 21))
    images = _stored_images(world, cid)
    assert counted["update_new_image_count"] == sum(
        images[f"第 {index} 问"] for index in range(turns - MAX_TURNS, turns)
        if index not in old_shown
    )


def case_a_boundary_the_watermark_passed_or_that_does_not_resolve_is_reported(world: World) -> None:
    turns = MAX_TURNS + 30
    cid, _lost = _long_world(world, turns)
    assert share(world, cid, job_of(cid, turns - 1)).status_code == 200
    early = stored_counts(world, cid, through_id=job_of(cid, 40)).json()
    assert early["watermark_ahead"] is True and early["ahead_count"] == turns - 41
    # Past the boundary the link's content is described as a whole.
    whole = stored_counts(world, cid).json()
    assert {key: early[key] for key in ("published_turns", "earlier_turns", "image_count")} == {
        key: whole[key] for key in ("published_turns", "earlier_turns", "image_count")
    }
    unresolved = stored_counts(world, cid, through_id="job-missing").json()
    assert unresolved["boundary_unresolved"] is True and unresolved["through_id"] == ""
    assert unresolved["total_turns"] == turns and unresolved["image_count"] == 0
    # A watermark job that no longer exists is unknown, never "at".
    gone = share_counts(world, cid, through_id=job_of(cid, 40),
                        watermark_id="job-gone", watermark_at=instant(60)).json()
    assert gone["watermark_unknown"] is True and gone["watermark_ahead"] is False


def case_counting_is_owner_only_and_reads_only(world: World) -> None:
    cid, _lost = _long_world(world, 3)
    assert world.client.get(counts_path(cid), headers=world.owner.headers).status_code == 404
    assert share_counts(world, "gconv-missing").status_code == 404
    unshared = share_counts(world, cid)
    assert unshared.status_code == 200
    assert (unshared.json()["watermark_id"], unshared.json()["watermark_at"]) == ("", "")
    store = world.repo._runtime.global_ask_store
    assert store.conversation_share_state(cid, world.alice.id)["share_token"] == ""


def case_without_a_watermark_the_stored_one_is_counted_and_echoed(world: World) -> None:
    """The dialog asks before it has read the share state: left out, the
    watermark is the stored one, and the body says which it counted against."""
    turns = MAX_TURNS + 10
    cid, _lost = _long_world(world, turns)
    assert share(world, cid, job_of(cid, MAX_TURNS + 2)).status_code == 200
    state = world.repo._runtime.global_ask_store.conversation_share_state(cid, world.alice.id)
    implicit = share_counts(world, cid).json()
    assert (implicit["watermark_id"], implicit["watermark_at"]) == (
        state["shared_through_id"], state["shared_through_at"])
    assert implicit == stored_counts(world, cid).json()
    assert implicit["new_turns"] == 7 and implicit["earlier_turns"] == 3
    # An explicit empty watermark is "not shared", whatever is stored.
    explicit = share_counts(world, cid, watermark_id="", watermark_at="").json()
    assert explicit["watermark_id"] == "" and explicit["new_turns"] == 0


def case_a_job_finishing_between_the_aggregate_and_the_windows_is_not_counted(world: World) -> None:
    """Every window is bounded by the newest job the aggregate saw: a job that
    finishes between the two reads neither enters a window nor turns the
    answer into a refusal."""
    from app.repositories.global_ask_store import GlobalAskStore

    turns = 12
    cid, _lost = _long_world(world, turns)
    before = share_counts(world, cid).json()
    original = GlobalAskStore._share_ranks
    late: list[str] = []

    def job_finishes_in_between(facts, asked, mark_id, mark_at):
        result = original(facts, asked, mark_id, mark_at)
        if not late:
            late.append(f"{cid}-late")
            bulk_jobs(world, cid, world.alice.id, [(late[0], instant(turns + 5), "done", job_payload(
                late[0], "晚到的一问", resolved=[world.notebook],
                anchors=imaged(["img-late"], notebook_id=world.notebook),
            ))])
        return result

    world.monkeypatch.setattr(GlobalAskStore, "_share_ranks", staticmethod(job_finishes_in_between))
    during = share_counts(world, cid)
    assert during.status_code == 200, during.text
    assert during.json() == before
    world.monkeypatch.setattr(GlobalAskStore, "_share_ranks", staticmethod(original))
    after = share_counts(world, cid).json()
    assert after["total_turns"] == turns + 1 and after["through_id"] == late[0]


def case_a_watermark_job_past_its_retention_stamp_is_placed_as_the_page_places_it(world: World) -> None:
    """Deliberate change from the browser-side count: the watermark job itself
    expired (its stamp passed) but its row still anchors the public page's
    keyset. The browser, holding only live turns, could not find it and said
    "unknown"; the server places it exactly where the page does -- the link
    holds the live turns before it, and the boundary after it is "behind"."""
    turns = 10
    cid, _lost = _long_world(world, turns)
    assert share(world, cid, job_of(cid, 6)).status_code == 200
    store = world.repo._runtime.global_ask_store
    with store.database.write() as db:
        db.execute(store._sql("UPDATE global_ask_jobs SET retained_until=? WHERE id=?"),
                   ("2000-01-01T00:00:00+00:00", job_of(cid, 6)))
    page = _page(world, cid)
    assert [turn["question"] for turn in page["turns"]] == [f"第 {i} 问" for i in range(6)]
    whole = stored_counts(world, cid).json()
    assert (whole["total_turns"], whole["published_turns"], whole["new_turns"]) == (9, 6, 3)
    bounded = stored_counts(world, cid, through_id=job_of(cid, 9)).json()
    assert bounded["watermark_unknown"] is False and bounded["watermark_behind"] is True
    assert bounded["published_turns"] == 6 and bounded["new_turns"] == 3


def case_the_counts_resolve_the_boundary_and_watermark_the_share_pins(world: World) -> None:
    """Same inputs, same resolution: the boundary the counts report is the one
    a share pins (a tie at one instant included), a boundary the share would
    refuse as stale is one the counts report as unresolved, and the watermark
    the counts echo is the one stored."""
    cid = f"gconv-tie-{next(_IDS)}"
    insert_conversation(world.repo._runtime.global_ask_store, cid, world.alice.id)
    rows = [(f"{cid}-{name}", instant(at), "done", job_payload(f"{cid}-{name}", f"问 {name}",
             resolved=[world.notebook])) for name, at in (("a", 1), ("c", 2), ("b", 2))]
    bulk_jobs(world, cid, world.alice.id, rows)
    newest = share_counts(world, cid).json()
    # (created_at, id) order: a, b, c -- the newest is c, inserted before b.
    assert newest["through_id"] == f"{cid}-c" and newest["total_turns"] == 3
    shared = share(world, cid, "")
    assert shared.status_code == 200 and shared.json()["shared_through_id"] == newest["through_id"]
    pinned = share_counts(world, cid, through_id=f"{cid}-b").json()
    assert pinned["boundary_index"] == 1 and pinned["watermark_ahead"] is True
    assert pinned["ahead_count"] == 1
    gone = share_counts(world, cid, through_id=f"{cid}-x").json()
    stale = share(world, cid, f"{cid}-x")
    assert gone["boundary_unresolved"] is True and stale.status_code == 409
    implicit = share_counts(world, cid).json()
    assert (implicit["watermark_id"], implicit["watermark_at"]) == (
        shared.json()["shared_through_id"], shared.json()["shared_through_at"])


# --- authority: the libraries of the turns the page shows ------------------------


def case_a_lost_library_on_a_turn_the_page_shows_refuses_counts_and_share(world: World) -> None:
    """Shared at turn 520 of 560, the page shows turns 21..520. Turn 40 is on it
    -- the old sweep (the newest 500 completed turns, cut at the boundary:
    60..520) never looked at it and minted a link born dead."""
    turns = MAX_TURNS + 60
    cid, _lost = _long_world(world, turns, lost_at=(40,))
    boundary = job_of(cid, MAX_TURNS + 20)
    for attempt in (share_counts(world, cid, through_id=boundary), share(world, cid, boundary)):
        assert attempt.status_code == 404 and attempt.json() == LOST
    store = world.repo._runtime.global_ask_store
    assert store.conversation_share_state(cid, world.alice.id)["share_token"] == ""


def case_a_lost_library_older_than_the_page_refuses_nothing(world: World) -> None:
    """Turn 10 is older than the page's window (21..520): it is not published,
    so it vetoes neither the count nor the share, and the page opens."""
    turns = MAX_TURNS + 60
    cid, _lost = _long_world(world, turns, lost_at=(10,))
    boundary = job_of(cid, MAX_TURNS + 20)
    assert share_counts(world, cid, through_id=boundary).status_code == 200
    assert share(world, cid, boundary).status_code == 200
    _assert_counts_describe_the_page(world, cid, stored_counts(world, cid).json())


def case_a_lost_library_after_the_boundary_vetoes_only_the_update(world: World) -> None:
    turns = MAX_TURNS + 60
    cid, _lost = _long_world(world, turns, lost_at=(MAX_TURNS + 40,))
    boundary = job_of(cid, MAX_TURNS + 20)
    assert share(world, cid, boundary).status_code == 200
    # The dialog's update preview reaches the lost turn: no number is given.
    assert stored_counts(world, cid).status_code == 404
    assert share(world, cid, job_of(cid, turns - 1)).status_code == 404
    # The link itself, and the bounded view of it, are fine.
    at = stored_counts(world, cid, through_id=boundary)
    assert at.status_code == 200
    _assert_counts_describe_the_page(world, cid, at.json(), bounded=True)


CASES: dict[str, Callable[[World], None]] = {
    name.removeprefix("case_"): case for name, case in globals().items()
    if name.startswith("case_") and callable(case)
}


def run(world: World, name: str) -> Any:
    return CASES[name](world)
