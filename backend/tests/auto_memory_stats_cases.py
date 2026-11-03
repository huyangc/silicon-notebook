"""Backend-neutral cases for ``scripts/auto_memory_stats.py`` (auto user-memory
plan T12). SQLite: ``tests/test_auto_memory_stats.py``; PostgreSQL twin:
``tests/postgres/test_auto_memory_stats_pg.py``.

Each case takes a :class:`Stats`: ``seed_env`` (the shared
``memory_suggest_store_cases.Env`` write/read helpers) and ``collect`` (the
script's ``collect`` bound to a read-only connection of the backend under
test). The seeded story is one fixed scenario; every number the cases assert is
derived in :func:`seed_scenario`'s comments.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta, timezone
from typing import Any, Callable

from tests.memory_suggest_store_cases import NB, NB2, OTHER, OWNER, T0, Env, at

#: ``now`` of every stats call: 60 days after T0.
NOW = T0 + timedelta(days=60)

#: Strings that must never reach the report: every content column of the seed
#: carries one (titles, bodies, tags, questions, answer text, quoted spans,
#: a free-text failure reason).
SENTINELS = (
    "SENTINEL-TITLE", "SENTINEL-BODY", "SENTINEL-EDITED", "SENTINEL-TAG",
    "SENTINEL-QUESTION", "SENTINEL-ANSWER", "SENTINEL-SPAN", "SENTINEL-REASON",
)
#: Keys no report node may carry (design 6.4: counts and codes only).
FORBIDDEN_KEYS = frozenset({
    "title", "content", "content_md", "tags", "tags_json", "question", "answer",
    "text", "quoted_span", "label", "memory_id", "id", "payload",
})

MEMORY_IDS = (
    "mem-c1", "mem-c2", "mem-c3", "mem-r1", "mem-r2", "mem-r3", "mem-r4",
    "mem-r5", "mem-m1", "mem-nb2",
)


@dataclass
class Stats:
    env: Env
    collect: Callable[..., dict[str, Any]]


def _day(days: float) -> str:
    return (T0 + timedelta(days=days)).astimezone(timezone.utc).isoformat()


def _memory(
    env: Env, memory_id: str, status: str, signal: str | None, *,
    notebook_id: str = NB, created_day: float = 0.0,
) -> None:
    env.run(
        "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
        "content_md,tags_json,suggested_signal,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            memory_id, notebook_id, OWNER, "ask_answer", status,
            "SENTINEL-TITLE", "SENTINEL-BODY", env.json(["SENTINEL-TAG"]), signal,
            _day(created_day), _day(created_day),
        ),
    )


def _revision(
    env: Env, memory_id: str, revision: int, status: str, reason: str, day: float,
    *, body: str = "SENTINEL-BODY",
) -> None:
    env.run(
        "INSERT INTO memory_revisions(id,memory_id,revision,title,content_md,tags_json,"
        "status,promotion_state,changed_by,change_reason,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"rev-{memory_id}-{revision}", memory_id, revision, "SENTINEL-TITLE", body,
            env.json(["SENTINEL-TAG"]), status, "none", OWNER, reason, _day(day),
        ),
    )


def _job(
    env: Env, notebook_id: str, owner: str, *, status: str = "idle", outcome: str = "",
    reason: str = "", runs: int = 0, written: int = 0, day: float = 0.0,
) -> None:
    env.run(
        "INSERT INTO memory_suggest_jobs(notebook_id,owner_id,status,cursor_created_at,"
        "window_started_at,runs,candidates_written,last_outcome,failure_reason,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            notebook_id, owner, status, _day(0), _day(0), runs, written, outcome,
            reason, _day(0), _day(day),
        ),
    )


def _answer(
    env: Env, answer_id: str, notebook_id: str, citations: list[dict], day: float,
) -> None:
    payload = {
        "answer": "SENTINEL-ANSWER",
        "question": "SENTINEL-QUESTION",
        "citations": citations,
    }
    env.run(
        "INSERT INTO answers(id,notebook_id,question,payload,created_at,conversation_id) "
        "VALUES (?,?,?,?,?,?)",
        (answer_id, notebook_id, "SENTINEL-QUESTION", env.json(payload), _day(day), None),
    )


def _card(memory_id: str = "") -> dict:
    card = {"label": "SENTINEL-TITLE", "source_id": "", "quoted_span": "SENTINEL-SPAN"}
    if memory_id:
        card["memory_id"] = memory_id
    return card


def seed_scenario(env: Env) -> None:
    """The fixed story (days are offsets from T0; ``NOW`` = day 60).

    clarified (NB): c1 adopted as is on day 1, never deprecated; c2 adopted
    EDITED on day 1 (revision 2 differs in body), deprecated on day 10; c3
    ignored (rejected, no ``expired`` revision).
    repeated (NB): r1 pending; r2 expired; r3 adopted as is on day 2 and
    deprecated on day 40 (38 days later: outside the window); r4 adopted as is
    on day 55 (too young to count); r5 edited while still a candidate
    (revision 2) then confirmed unchanged on day 3 (differs from revision 1:
    edited). repeated (NB2): one pending row created on day 30.
    m1: a manual memory (no signal), only ever cited.
    """
    env.seed()
    for suggestion, status, signal in (
        ("mem-c1", "confirmed", "clarified"),
        ("mem-c2", "deprecated", "clarified"),
        ("mem-c3", "rejected", "clarified"),
        ("mem-r1", "candidate", "repeated"),
        ("mem-r2", "rejected", "repeated"),
        ("mem-r3", "deprecated", "repeated"),
        ("mem-r4", "confirmed", "repeated"),
        ("mem-r5", "confirmed", "repeated"),
    ):
        _memory(env, suggestion, status, signal)
    _memory(env, "mem-m1", "confirmed", None)
    _memory(env, "mem-nb2", "candidate", "repeated", notebook_id=NB2, created_day=30)

    for memory_id in ("mem-c1", "mem-c2", "mem-c3", "mem-r1", "mem-r2", "mem-r3",
                      "mem-r4", "mem-r5"):
        _revision(env, memory_id, 1, "candidate", "suggested", 0)
    _revision(env, "mem-nb2", 1, "candidate", "suggested", 30)
    _revision(env, "mem-c1", 2, "confirmed", "confirmed", 1)
    _revision(env, "mem-c2", 2, "confirmed", "confirmed", 1, body="SENTINEL-EDITED")
    _revision(env, "mem-c2", 3, "deprecated", "deprecated", 10, body="SENTINEL-EDITED")
    _revision(env, "mem-c3", 2, "rejected", "rejected", 2)
    _revision(env, "mem-r2", 2, "rejected", "expired", 31)
    _revision(env, "mem-r3", 2, "confirmed", "confirmed", 2)
    _revision(env, "mem-r3", 3, "deprecated", "deprecated", 40)
    _revision(env, "mem-r4", 2, "confirmed", "confirmed", 55)
    _revision(env, "mem-r5", 2, "candidate", "updated", 2, body="SENTINEL-EDITED")
    _revision(env, "mem-r5", 3, "confirmed", "confirmed", 3, body="SENTINEL-EDITED")

    # jobs: NB/OWNER done (3 runs, 2 written); NB/OTHER skipped (a code);
    # NB2/OWNER failed with a free-text reason (prints as ``other``); one
    # running job that never finished a run.
    env.run(
        "INSERT INTO notebooks(id,name,created_by,status,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        ("nb-ms-3", "nb-ms-3", OWNER, "ready", at(0), at(0)),
    )
    _job(env, NB, OWNER, outcome="done", runs=3, written=2, day=50)
    _job(env, NB, OTHER, outcome="skipped", reason="user_disabled", runs=1, day=5)
    _job(env, NB2, OWNER, outcome="failed", reason="SENTINEL-REASON text", runs=2, day=40)
    _job(env, "nb-ms-3", OWNER, status="running", day=0)

    # answers: a1 cites c1, m1, r3; a2 cites c1 only; a3 has a Memory-less card.
    _answer(env, "ans-1", NB, [_card("mem-c1"), _card("mem-m1"), _card("mem-r3")], 20)
    _answer(env, "ans-2", NB, [_card("mem-c1")], 45)
    _answer(env, "ans-3", NB, [_card()], 46)


# --------------------------------------------------------------------- cases
def assert_counts_by_signal_and_status(stats: Stats) -> None:
    seed_scenario(stats.env)
    suggestions = stats.collect(now=NOW)["suggestions"]

    assert suggestions["by_signal_status"] == {
        "clarified": {"candidate": 0, "confirmed": 1, "rejected": 1, "deprecated": 1},
        # r1 + the NB2 row are pending; r4 + r5 confirmed; r2 rejected; r3 deprecated
        "repeated": {"candidate": 2, "confirmed": 2, "rejected": 1, "deprecated": 1},
        "corrected": {"candidate": 0, "confirmed": 0, "rejected": 0, "deprecated": 0},
        "other": {"candidate": 0, "confirmed": 0, "rejected": 0, "deprecated": 0},
    }


def assert_corrected_real_rows_are_counted(stats: Stats) -> None:
    seed_scenario(stats.env)
    env = stats.env
    for memory_id, status in (
        ("corr-pending", "candidate"), ("corr-adopted", "confirmed"),
        ("corr-edited", "deprecated"), ("corr-ignored", "rejected"),
        ("corr-expired", "rejected"),
    ):
        _memory(env, memory_id, status, "corrected")
        _revision(env, memory_id, 1, "candidate", "suggested", 0)
    _revision(env, "corr-adopted", 2, "confirmed", "confirmed", 1)
    _revision(env, "corr-edited", 2, "confirmed", "confirmed", 2, body="SENTINEL-EDITED")
    _revision(env, "corr-edited", 3, "deprecated", "deprecated", 10, body="SENTINEL-EDITED")
    _revision(env, "corr-ignored", 2, "rejected", "rejected", 3)
    _revision(env, "corr-expired", 2, "rejected", "expired", 31)
    _answer(env, "ans-corrected", NB,
            [_card("corr-adopted"), _card("corr-edited"), _card("corr-adopted")], 45)

    report = stats.collect(now=NOW)
    assert report["suggestions"]["by_signal_status"]["corrected"] == {
        "candidate": 1, "confirmed": 1, "rejected": 2, "deprecated": 1,
    }
    assert report["suggestions"]["funnel"]["corrected"] == {
        "proposed": 5, "pending": 1, "adopted_as_is": 1, "adopted_edited": 1,
        "adopted_unclassified": 0, "ignored": 1, "expired": 1, "adoption_rate": 0.5,
    }
    assert report["suggestions"]["funnel"]["all"]["proposed"] == 14
    assert report["suggestions"]["deprecation_30d"]["corrected"] == {
        "adopted": 2, "matured": 2, "deprecated_within_30d": 1, "rate": 0.5,
    }
    assert report["citations"]["corrected"] == {"cards": 3, "answers": 1}
    assert report["suggestions"]["funnel"]["other"]["proposed"] == 0


def assert_funnel(stats: Stats) -> None:
    seed_scenario(stats.env)
    funnel = stats.collect(now=NOW)["suggestions"]["funnel"]

    assert funnel["clarified"] == {
        "proposed": 3, "pending": 0, "adopted_as_is": 1, "adopted_edited": 1,
        "adopted_unclassified": 0, "ignored": 1, "expired": 0,
        "adoption_rate": round(2 / 3, 4),
    }
    assert funnel["repeated"] == {
        "proposed": 6, "pending": 2, "adopted_as_is": 2, "adopted_edited": 1,
        "adopted_unclassified": 0, "ignored": 0, "expired": 1,
        "adoption_rate": round(3 / 4, 4),
    }
    assert funnel["all"]["proposed"] == 9
    assert funnel["all"]["adopted_as_is"] == 3
    assert funnel["all"]["adopted_edited"] == 2
    assert funnel["all"]["adoption_rate"] == round(5 / 7, 4)
    assert funnel["other"]["proposed"] == 0 and funnel["other"]["adoption_rate"] is None


def assert_deprecation_window(stats: Stats) -> None:
    seed_scenario(stats.env)
    window = stats.collect(now=NOW)["suggestions"]["deprecation_30d"]

    # clarified: c1, c2 adopted day 1 (matured); c2 deprecated on day 10
    assert window["clarified"] == {
        "adopted": 2, "matured": 2, "deprecated_within_30d": 1, "rate": 0.5,
    }
    # repeated: r3 (day 2), r4 (day 55: too young), r5 (day 3); r3 was
    # deprecated 38 days after its adoption, outside the window
    assert window["repeated"] == {
        "adopted": 3, "matured": 2, "deprecated_within_30d": 0, "rate": 0.0,
    }
    assert window["all"] == {
        "adopted": 5, "matured": 4, "deprecated_within_30d": 1, "rate": 0.25,
    }
    # an earlier ``now`` changes who has matured (r3 and r5 are not 30 days old)
    early = stats.collect(now=T0 + timedelta(days=20))["suggestions"]["deprecation_30d"]
    assert early["all"]["matured"] == 0 and early["all"]["rate"] is None


def assert_job_outcomes(stats: Stats) -> None:
    seed_scenario(stats.env)
    jobs = stats.collect(now=NOW)["jobs"]

    assert jobs["jobs"] == 4
    assert jobs["running"] == 1
    assert jobs["runs"] == 6
    assert jobs["candidates_written"] == 2
    by_outcome = jobs["by_outcome"]
    assert by_outcome["done"] == 1
    assert by_outcome["skipped"] == 1
    assert by_outcome["failed"] == 1
    assert by_outcome["never_run"] == 1
    assert by_outcome["backlog"] == 0
    assert jobs["failure_reasons"] == {
        "failed": {"other": 1},
        "skipped": {"user_disabled": 1},
    }


def assert_citation_counts(stats: Stats) -> None:
    seed_scenario(stats.env)
    citations = stats.collect(now=NOW)["citations"]

    assert citations["clarified"] == {"cards": 2, "answers": 2}
    assert citations["repeated"] == {"cards": 1, "answers": 1}
    assert citations["manual"] == {"cards": 1, "answers": 1}
    assert citations["other"] == {"cards": 0, "answers": 0}


def assert_filters(stats: Stats) -> None:
    seed_scenario(stats.env)
    nb2 = stats.collect(now=NOW, notebook_id=NB2)
    assert nb2["suggestions"]["funnel"]["all"]["proposed"] == 1
    assert nb2["suggestions"]["funnel"]["repeated"]["pending"] == 1
    assert nb2["jobs"]["jobs"] == 1
    assert nb2["citations"]["clarified"] == {"cards": 0, "answers": 0}

    since = stats.collect(now=NOW, since=T0 + timedelta(days=20))
    # suggestions created on or after day 20: only the NB2 row
    assert since["suggestions"]["funnel"]["all"]["proposed"] == 1
    # jobs last updated on or after day 20: NB/OWNER (day 50) and NB2/OWNER (day 40)
    assert since["jobs"]["jobs"] == 2
    # answers on or after day 20: all three (days 20, 45, 46)
    assert since["citations"]["clarified"] == {"cards": 2, "answers": 2}
    later = stats.collect(now=NOW, since=T0 + timedelta(days=45, hours=12))
    assert later["citations"]["clarified"] == {"cards": 0, "answers": 0}


def _walk(node: Any, path: str = "") -> list[tuple[str, str]]:
    """Every (path, key) pair and (path, string value) pair of the report."""
    found: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            found.append((path, str(key)))
            found.extend(_walk(value, f"{path}/{key}"))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk(item, path))
    elif isinstance(node, str):
        found.append((path, node))
    return found


def assert_report_is_content_free(stats: Stats) -> None:
    seed_scenario(stats.env)
    report = stats.collect(now=NOW)
    rendered = json.dumps(report, ensure_ascii=False)

    for sentinel in (*SENTINELS, *MEMORY_IDS, "ans-1", "ans-2", "ans-3"):
        assert sentinel not in rendered, sentinel
    keys = {key for _path, key in _walk(report)}
    assert not (keys & FORBIDDEN_KEYS), keys & FORBIDDEN_KEYS
    # the free-text failure reason was folded into the closed ``other`` bucket
    assert "SENTINEL-REASON" not in rendered
