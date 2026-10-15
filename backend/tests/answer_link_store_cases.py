"""Shared behaviour of the ``answer_links`` store (SQLite v94 / PostgreSQL
0074), run by ``test_answer_link_store.py`` and its PostgreSQL twin against a
store whose wall clock and sweep (monotonic) clock the case controls."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Callable

TTL = 3600
GRACE = 600


class Clock:
    def __init__(self, value: str = "2026-10-15T00:00:00+00:00") -> None:
        self.value = value

    def __call__(self) -> str:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value = (
            datetime.fromisoformat(self.value) + timedelta(seconds=seconds)
        ).isoformat()


class Monotonic:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


class Seed:
    """Inserts the parent rows the sweep looks at. ``run(sql, params)`` takes
    ``?`` placeholders; the PostgreSQL twin rewrites them."""

    def __init__(self, run: Callable[[str, tuple], None]) -> None:
        self.run = run
        self._base = False

    def _ensure_base(self) -> None:
        if self._base:
            return
        self._base = True
        t = "2026-10-15T00:00:00+00:00"
        self.run(
            "INSERT INTO users (id,email,display_name,role,status,created_at,updated_at) "
            "VALUES ('u1','u1@example.com','U1','user','active',?,?)", (t, t))
        self.run(
            "INSERT INTO notebooks (id,name,status,created_by,created_at,updated_at) "
            "VALUES ('nb1','nb','active','u1',?,?)", (t, t))
        self.run(
            "INSERT INTO agent_profiles (id,owner_id,name,created_at,updated_at) "
            "VALUES ('prof1','u1','p',?,?)", (t, t))
        self.run(
            "INSERT INTO global_ask_conversations (id,user_id,title,scope_json,"
            "submitted_via,created_at,updated_at) VALUES ('gc1','u1','t','{}','mcp',?,?)",
            (t, t))

    def token(self, token_id: str, *, revoked: bool = False) -> None:
        self._ensure_base()
        t = "2026-10-15T00:00:00+00:00"
        self.run(
            "INSERT INTO agent_access_tokens (id,agent_profile_id,token_hash,"
            "default_notebook_id,revoked_at,created_at) VALUES (?,?,?,?,?,?)",
            (token_id, "prof1", f"hash-{token_id}", "nb1", t if revoked else None, t))

    def notebook_job(self, job_id: str) -> None:
        self._ensure_base()
        self.run("INSERT INTO ask_jobs (id,notebook_id) VALUES (?,'nb1')", (job_id,))

    def global_job(self, job_id: str) -> None:
        self._ensure_base()
        self.run(
            "INSERT INTO global_ask_jobs (id,conversation_id,user_id,request_json,"
            "status,payload_json,created_at) VALUES (?,'gc1','u1','{}','done','{}',"
            "'2026-10-15T00:00:00+00:00')", (job_id,))

    def drop_job(self, kind: str, job_id: str) -> None:
        table = "ask_jobs" if kind == "notebook" else "global_ask_jobs"
        self.run(f"DELETE FROM {table} WHERE id = ?", (job_id,))


def _issue(store, *, kind="notebook", job="job-1", token="tok-1", ttl=TTL):
    return store.issue_answer_link(
        job_kind=kind, job_id=job, owner_id="u1", agent_token_id=token,
        ttl_seconds=ttl, sweep_grace_seconds=GRACE,
    )


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _live(seed: Seed) -> None:
    seed.token("tok-1")
    seed.notebook_job("job-1")


def assert_issue_then_read_back(store, clock: Clock, seed: Seed) -> None:
    _live(seed)
    issued = _issue(store)
    assert issued["reused"] is False
    assert issued["token"].startswith("alnk-")
    assert len(issued["token"]) == len("alnk-") + 43
    row = store.answer_link(issued["token"])
    assert row is not None
    assert {k: row[k] for k in ("token", "job_kind", "job_id", "owner_id", "agent_token_id")} == {
        "token": issued["token"], "job_kind": "notebook", "job_id": "job-1",
        "owner_id": "u1", "agent_token_id": "tok-1",
    }
    assert row["expired"] is False
    assert _at(row["created_at"]) == _at(clock.value)
    assert _at(row["expires_at"]) == _at(clock.value) + timedelta(seconds=TTL)
    assert _at(issued["expires_at"]) == _at(row["expires_at"])
    assert store.answer_link("alnk-unknown") is None


def assert_reissue_same_key_reuses_unexpired(store, clock: Clock, seed: Seed) -> None:
    _live(seed)
    first = _issue(store, ttl=TTL)
    clock.advance(10)
    again = _issue(store, ttl=TTL * 24)
    assert again["reused"] is True
    assert again["token"] == first["token"]
    assert again["expires_at"] == first["expires_at"]  # the old expiry, reported as is


def assert_reissue_after_expiry_replaces(store, clock: Clock, seed: Seed) -> None:
    _live(seed)
    first = _issue(store)
    clock.advance(TTL)  # exactly at the boundary: expired
    second = _issue(store)
    assert second["reused"] is False
    assert second["token"] != first["token"]
    assert store.answer_link(first["token"]) is None
    row = store.answer_link(second["token"])
    assert row is not None and row["expired"] is False


def assert_different_agent_token_gets_its_own_link(store, clock: Clock, seed: Seed) -> None:
    _live(seed)
    seed.token("tok-2")
    a = _issue(store, token="tok-1")
    b = _issue(store, token="tok-2")
    assert b["reused"] is False and a["token"] != b["token"]
    assert store.answer_link(a["token"])["agent_token_id"] == "tok-1"
    assert store.answer_link(b["token"])["agent_token_id"] == "tok-2"
    other_job = _issue(store, job="job-2", token="tok-1")
    assert other_job["token"] not in (a["token"], b["token"])


def assert_expired_flag_at_exact_boundary(store, clock: Clock, seed: Seed) -> None:
    _live(seed)
    issued = _issue(store)
    clock.advance(TTL - 1)
    assert store.answer_link(issued["token"])["expired"] is False
    clock.advance(1)  # now == expires_at
    assert store.answer_link(issued["token"])["expired"] is True
    clock.advance(1)
    assert store.answer_link(issued["token"])["expired"] is True


def assert_sweep_drops_past_grace_keeps_within_grace(store, clock: Clock, seed: Seed) -> None:
    _live(seed)
    issued = _issue(store)
    clock.advance(TTL + GRACE)  # expired exactly `grace` ago: still kept
    assert store.sweep_answer_links(grace_seconds=GRACE) == 0
    assert store.answer_link(issued["token"]) is not None
    clock.advance(1)
    assert store.sweep_answer_links(grace_seconds=GRACE) == 1
    assert store.answer_link(issued["token"]) is None


def assert_sweep_drops_revoked_or_missing_token_rows(store, clock: Clock, seed: Seed) -> None:
    seed.notebook_job("job-1")
    seed.token("tok-live")
    seed.token("tok-revoked", revoked=True)
    live = _issue(store, token="tok-live")
    revoked = _issue(store, token="tok-revoked")
    missing = _issue(store, token="tok-never-existed")
    assert store.sweep_answer_links(grace_seconds=GRACE) == 2
    assert store.answer_link(live["token"]) is not None
    assert store.answer_link(revoked["token"]) is None
    assert store.answer_link(missing["token"]) is None


def assert_sweep_drops_rows_whose_job_is_gone(store, clock: Clock, seed: Seed) -> None:
    seed.token("tok-1")
    seed.notebook_job("job-nb")
    seed.global_job("job-gl")
    seed.notebook_job("job-nb-keep")
    seed.global_job("job-gl-keep")
    nb = _issue(store, kind="notebook", job="job-nb")
    gl = _issue(store, kind="global", job="job-gl")
    nb_keep = _issue(store, kind="notebook", job="job-nb-keep")
    gl_keep = _issue(store, kind="global", job="job-gl-keep")
    # Same id in the other table must not keep a row alive.
    seed.global_job("job-nb-mixup")
    mixed = _issue(store, kind="notebook", job="job-nb-mixup")
    assert store.sweep_answer_links(grace_seconds=GRACE) == 1  # only `mixed`
    assert store.answer_link(mixed["token"]) is None
    seed.drop_job("notebook", "job-nb")
    seed.drop_job("global", "job-gl")
    assert store.sweep_answer_links(grace_seconds=GRACE) == 2
    assert store.answer_link(nb["token"]) is None
    assert store.answer_link(gl["token"]) is None
    assert store.answer_link(nb_keep["token"]) is not None
    assert store.answer_link(gl_keep["token"]) is not None


def assert_issue_sweeps_at_most_once_per_interval(
    store, clock: Clock, mono: Monotonic, seed: Seed
) -> None:
    seed.token("tok-1")
    seed.notebook_job("job-1")
    garbage = _issue(store, job="job-gone")  # job missing: garbage once swept
    _issue(store, job="job-1")
    assert store.answer_link(garbage["token"]) is not None  # throttled: not swept
    mono.value += 3599
    _issue(store, job="job-1")
    assert store.answer_link(garbage["token"]) is not None
    mono.value += 1
    _issue(store, job="job-1")
    assert store.answer_link(garbage["token"]) is None  # interval elapsed: swept


def assert_concurrent_same_key_issues_one_link(
    store, clock: Clock, seed: Seed, count_rows: Callable[[], int]
) -> None:
    _live(seed)
    barrier = threading.Barrier(2)
    results: list = []
    errors: list = []

    def worker() -> None:
        try:
            barrier.wait(timeout=10)
            results.append(_issue(store))
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors, errors
    assert len(results) == 2
    assert results[0]["token"] == results[1]["token"]
    assert sorted(r["reused"] for r in results) == [False, True]
    assert count_rows() == 1
