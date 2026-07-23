from __future__ import annotations

import copy
import importlib.util
import math
import os
import sqlite3
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.core import diagnostics_runtime as diagnostics
from app.core.config import Settings
from app.repositories.sqlite.database import SqliteDatabase


REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
MIB = 1024 * 1024


def repo_frame(path: str, line: int, function: str) -> str:
    return f"<repo>/{path}:{line} in {function}"


def load_script(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


DELETE_INCIDENT = {
    "captured_at": "2026-07-21T12:00:00+00:00",
    "runtime": {
        "active_requests": [{
            "request_id": "req-private",
            "method": "DELETE",
            "path": "/api/notebooks/{id}",
            "notebook_id": "nb-private",
            "phase": "notebook_delete.db",
            "thread_id": 101,
            "duration_ms": 12000,
        }],
        "write_lock": {
            "holder": {
                "thread_id": 101,
                "operation": "notebook.delete",
                "request_id": "req-private",
                "duration_ms": 11000,
            },
            "waiters": [{
                "thread_id": 202,
                "operation": "sqlite.write",
                "duration_ms": 9000,
            }],
        },
        "active_sql": [{
            "thread_id": 101,
            "verb": "DELETE",
            "table": "notebooks",
            "fingerprint": "abc123def456",
            "duration_ms": 10000,
        }],
        "active_jobs": [],
        "concurrency": {},
    },
    "stacks": {
        "65": [repo_frame("backend/app/repositories/sqlite/database.py", 120, "write")]
    },
    "db": {
        "evidence_complete": True,
        "missing_fk_indexes": [
            {"table": "legacy_children", "columns": ["notebook_id"]}
        ],
        "delete_plan": [{"detail": "SCAN legacy_children"}],
        "relevant_scans": [
            {"table": "legacy_children", "detail": "SCAN legacy_children"}
        ],
        "largest_tables": [],
        "files": {"database_bytes": 1000, "wal_bytes": 200},
        "degraded": [],
    },
    "process": {"pid": 4242, "cpu_percent": 2.0, "d_state_threads": 0},
    "health": {"backend": "timeout", "frontend": "ok"},
    "logs": {"malformed": 0, "slow_requests": []},
    "degraded": [],
}


def finding_for(evidence, code):
    rules = load_script("diag_rules")
    return next(item for item in rules.diagnose(evidence, limit=20) if item.code == code)


def test_sqlite_delete_ranks_holder_waiter_stack_and_scan_first():
    rules = load_script("diag_rules")
    findings = rules.diagnose(copy.deepcopy(DELETE_INCIDENT))

    assert findings[0].code == "sqlite_write_blocking"
    assert findings[0].score == 100
    assert findings[0].confidence == "high"
    chain = " ".join(findings[0].evidence)
    assert "holder" in chain
    assert "waiter" in chain
    assert "SCAN legacy_children" in chain
    assert "missing index" in findings[0].next_action.lower()
    assert "legacy_children" in findings[0].next_action
    assert "nb-private" not in repr(findings)
    assert "req-private" not in repr(findings)


@pytest.mark.parametrize(
    ("code", "evidence", "score"),
    [
        (
            "slow_sql_cascade",
            {
                "runtime": {"active_sql": [{"duration_ms": 6000, "table": "legacy_children"}]},
                "db": {
                    "evidence_complete": True,
                    "delete_plan": [{"detail": "SCAN legacy_children"}],
                    "missing_fk_indexes": [{"table": "legacy_children", "columns": ["notebook_id"]}],
                    "largest_tables": [{"name": "legacy_children", "bytes": 100 * MIB}],
                    "degraded": [],
                },
            },
            100,
        ),
        (
            "external_model_wait",
            {
                "runtime": {"concurrency": {"llm": {"active": 1, "maximum": 4, "waiting": 0}}},
                "stacks": {"a": ["<library:httpx>:44 in read"]},
                "logs": {"model_recent_error_or_slow": True},
            },
            85,
        ),
        (
            "pool_saturation",
            {
                "runtime": {"concurrency": {"embedding": {"active": 2, "maximum": 2, "waiting": 2}}},
            },
            90,
        ),
        (
            "event_loop_stall",
            {
                "process": {"pid": 55, "cpu_percent": 5},
                "health": {"backend": "timeout", "frontend": "ok"},
                "main_thread_id": 101,
                "stack_samples": [
                    {"65": [repo_frame("backend/app/repositories/sqlite/database.py", 120, "write")]},
                    {"65": [repo_frame("backend/app/repositories/sqlite/database.py", 120, "write")]},
                ],
            },
            90,
        ),
        (
            "filesystem_cleanup",
            {
                "runtime": {
                    "active_requests": [{"phase": "notebook_delete.files"}],
                    "write_lock": {"holder": None, "waiters": []},
                },
                "stacks": {"a": ["<library:shutil>:88 in rmtree"]},
            },
            95,
        ),
        (
            "wal_pressure",
            {
                "runtime": {"active_sql": [{"mode": "read", "duration_ms": 6000}]},
                "db": {
                    "evidence_complete": True,
                    "files": {"wal_bytes": 256 * MIB},
                    "wal_growth_bytes": 64 * MIB,
                    "degraded": [],
                },
            },
            85,
        ),
        (
            "cpu_loop",
            {
                "process": {"pid": 55, "cpu_percent": 80},
                "stack_samples": [
                    {"a": [repo_frame("backend/app/services/kg/work.py", 20, "spin")]},
                    {"a": [repo_frame("backend/app/services/kg/work.py", 20, "spin")]},
                ],
            },
            85,
        ),
        (
            "io_wait",
            {
                "process": {
                    "pid": 55,
                    "cpu_percent": 3,
                    "read_bytes_delta": 1,
                    "write_bytes_delta": 0,
                    "d_state_threads": 1,
                },
            },
            85,
        ),
        (
            "service_split",
            {"health": {"backend": "ok", "frontend": "refused"}},
            60,
        ),
    ],
)
def test_exact_rule_scores(code, evidence, score):
    finding = finding_for(evidence, code)
    assert finding.score == score
    assert finding.confidence == ("high" if score >= 80 else "medium")


def test_conflicts_races_and_insufficient_evidence_do_not_overclaim():
    rules = load_script("diag_rules")
    raced = copy.deepcopy(DELETE_INCIDENT)
    raced["db"].update({"status": "degraded", "evidence_complete": False})
    raced["db"]["degraded"] = [{"probe": "source", "category": "source_changed"}]
    stale = copy.deepcopy(raced)
    stale["runtime_fresh"] = False

    raced_finding = next(
        item for item in rules.diagnose(raced, limit=20)
        if item.code == "sqlite_write_blocking"
    )
    assert raced_finding.score == 85
    assert "SCAN" not in " ".join(raced_finding.evidence)
    assert all(
        item.code not in {"sqlite_write_blocking", "slow_sql_cascade"}
        for item in rules.diagnose(stale, limit=20)
    )
    assert rules.diagnose({}) == []

    weak = rules.diagnose({
        "runtime": {"write_lock": {"holder": {"duration_ms": 6000}, "waiters": []}}
    })
    assert weak[0].confidence == "low"
    assert "root cause" not in weak[0].title.lower()
    assert "root cause" not in weak[0].next_action.lower()


def test_stale_runtime_does_not_create_high_confidence_live_findings():
    rules = load_script("diag_rules")
    evidence = {
        "runtime_fresh": False,
        "runtime": {
            "active_requests": [{"phase": "notebook_delete.files"}],
            "write_lock": {"holder": None, "waiters": []},
            "concurrency": {
                "llm": {"active": 2, "maximum": 2, "waiting": 2}
            },
        },
        "stacks": {
            "a": [
                "<library:httpx>:44 in read",
                "<library:shutil>:88 in rmtree",
            ]
        },
    }
    findings = {item.code: item for item in rules.diagnose(evidence, limit=20)}
    assert "pool_saturation" not in findings
    assert findings["external_model_wait"].score == 50
    assert findings["filesystem_cleanup"].score == 40
    report = rules.render_incident(evidence, list(findings.values()))
    assert "notebook_delete.files" not in report
    assert "active=2" not in report


def test_split_health_requires_two_completed_probes():
    rules = load_script("diag_rules")
    assert all(
        item.code != "service_split"
        for item in rules.diagnose({"health": {"frontend": "ok"}}, limit=20)
    )


def test_slow_sql_does_not_fuse_an_unrelated_delete_plan():
    finding = finding_for(
        {
            "runtime": {
                "active_sql": [
                    {"duration_ms": 6000, "verb": "SELECT", "table": "users"}
                ]
            },
            "db": {
                "evidence_complete": True,
                "delete_plan": [{"detail": "SCAN legacy_children"}],
                "missing_fk_indexes": [
                    {"table": "legacy_children", "columns": ["notebook_id"]}
                ],
                "largest_tables": [],
                "degraded": [],
            },
        },
        "slow_sql_cascade",
    )
    assert finding.score == 40
    assert "legacy_children" not in " ".join(finding.evidence)


def test_repeated_frames_stay_on_one_thread_and_select_relevant_frame():
    rules = load_script("diag_rules")
    event = finding_for(
        {
            "process": {"pid": 5, "cpu_percent": 5},
            "health": {"backend": "timeout", "frontend": "ok"},
            "main_thread_id": 101,
            "stack_samples": [
                {"65": [
                    repo_frame("backend/app/aaa.py", 1, "idle"),
                    repo_frame("backend/app/repositories/sqlite/database.py", 2, "write"),
                ]},
                {"65": [
                    repo_frame("backend/app/aaa.py", 1, "idle"),
                    repo_frame("backend/app/repositories/sqlite/database.py", 2, "write"),
                ]},
            ],
        },
        "event_loop_stall",
    )
    assert event.score == 90
    assert "repositories/sqlite" in " ".join(event.evidence)

    cpu = finding_for(
        {
            "process": {"pid": 5, "cpu_percent": 80},
            "stack_samples": [
                {"a": [repo_frame("backend/app/work.py", 2, "spin")]},
                {"b": [repo_frame("backend/app/work.py", 2, "spin")]},
            ],
        },
        "cpu_loop",
    )
    assert cpu.score == 45


def test_ranking_is_score_then_code_and_defaults_to_three():
    rules = load_script("diag_rules")
    evidence = copy.deepcopy(DELETE_INCIDENT)
    evidence["process"].update({
        "cpu_percent": 90,
        "read_bytes_delta": 1,
        "d_state_threads": 1,
    })
    evidence["stack_samples"] = [evidence["stacks"], evidence["stacks"]]
    evidence["runtime"]["concurrency"] = {
        "llm": {"active": 1, "maximum": 1, "waiting": 1}
    }
    first = rules.diagnose(evidence)
    second = rules.diagnose(copy.deepcopy(evidence))
    assert len(first) == 3
    assert first == second
    assert [(row.score, row.code) for row in first] == sorted(
        [(row.score, row.code) for row in first], key=lambda row: (-row[0], row[1])
    )


def test_report_is_one_stable_private_utf8_block_with_hard_byte_cap():
    rules = load_script("diag_rules")
    incident = copy.deepcopy(DELETE_INCIDENT)
    incident["unused"] = {
        "prompt": "SENSITIVE-PROMPT",
        "authorization": "Bearer secret",
        "path": "/Users/private-owner/customer/notebook.pdf",
        "sql": "DELETE FROM notebooks WHERE id='nb-private'",
    }
    incident["runtime"]["active_requests"][0]["path"] = "/api/SENSITIVE-PROMPT"
    incident["db"]["delete_plan"] *= 5000
    incident["db"]["missing_fk_indexes"] *= 5000
    findings = rules.diagnose(incident)

    first = rules.render_incident(incident, findings, limit_bytes=32768)
    second = rules.render_incident(copy.deepcopy(incident), findings, limit_bytes=32768)

    assert first == second
    assert first.endswith("\n")
    assert len(first.encode("utf-8")) <= 32768
    assert first.count("silicon-notebook DFX incident report") == 1
    for heading in (
        "Confidence-ranked diagnoses",
        "Observations",
        "Missing/degraded evidence",
        "Safe next commands/actions",
    ):
        assert heading in first
    for secret in (
        "nb-private",
        "req-private",
        "SENSITIVE-PROMPT",
        "Bearer secret",
        "/Users/private-owner",
        "DELETE FROM",
    ):
        assert secret not in first
    assert "notebook#1" in first
    assert "request#1" in first


def test_process_unavailable_is_degraded_not_a_diagnosis():
    rules = load_script("diag_rules")
    evidence = {
        "captured_at": "2026-07-21T12:00:00+00:00",
        "pid_resolution": {"status": "ambiguous", "candidate_count": 2},
        "process": {"status": "unavailable"},
        "degraded": [{"source": "pid", "category": "ambiguous"}],
    }
    findings = rules.diagnose(evidence)
    report = rules.render_incident(evidence, findings)
    assert findings == []
    assert "process evidence unavailable" in report.lower()
    assert "--pid" in report


def test_tiny_report_limit_still_retains_mandatory_sections():
    rules = load_script("diag_rules")
    evidence = copy.deepcopy(DELETE_INCIDENT)
    findings = rules.diagnose(evidence)
    report = rules.render_incident(evidence, findings, limit_bytes=512)
    assert len(report.encode("utf-8")) <= 512
    assert report.startswith("silicon-notebook DFX incident report\nCaptured:")
    assert "Confidence-ranked diagnoses" in report
    assert findings[0].title in report
    assert "Missing/degraded evidence" in report


def test_renderer_does_not_trust_caller_supplied_finding_text():
    rules = load_script("diag_rules")
    malicious = rules.Finding(
        code="sqlite_write_blocking",
        title="SENSITIVE-PROMPT",
        score=100,
        confidence="high",
        evidence=("Bearer secret", "/Users/private-owner/file", "DELETE FROM private"),
        next_action="print token=secret",
    )
    report = rules.render_incident(
        {"captured_at": "2026-07-21T12:00:00+00:00"},
        [malicious],
    )
    for secret in (
        "SENSITIVE-PROMPT",
        "Bearer secret",
        "/Users/private-owner",
        "DELETE FROM",
        "token=secret",
    ):
        assert secret not in report


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.005)
    raise AssertionError("timed out waiting for diagnostic state")


def test_live_delete_lock_holder_waiter_report(tmp_path):
    rules = load_script("diag_rules")
    diag_db = load_script("diag_db")
    if not hasattr(os, "O_NOATIME"):
        diag_db._NOATIME_AVAILABLE = True
        diag_db._NOATIME_FLAG = 0
        diag_db._ALLOW_UNSAFE_WORKSPACE_PATH_FOR_TESTS = True

    db_path = tmp_path / "live.sqlite"
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        storage_dir=str(tmp_path / "storage"),
    )
    db = SqliteDatabase(settings, tmp_path)
    with db.write(operation="setup") as connection:
        connection.executescript(
            """
            CREATE TABLE notebooks (id TEXT PRIMARY KEY);
            CREATE TABLE legacy_children (
                id TEXT PRIMARY KEY,
                notebook_id TEXT REFERENCES notebooks(id) ON DELETE CASCADE
            );
            INSERT INTO notebooks(id) VALUES ('nb-live-private');
            INSERT INTO legacy_children(id, notebook_id)
                VALUES ('child-private', 'nb-live-private');
            """
        )

    runtime = diagnostics.DiagnosticsRuntime(
        tmp_path / "diagnostics",
        readiness_provider=lambda: {},
        concurrency_provider=lambda: {},
        enable_signal=False,
    )
    holder_in_function = threading.Event()
    release_holder = threading.Event()
    waiter_acquired = threading.Event()
    failures = []

    def hold_delete():
        try:
            with db.write(operation="notebook.delete") as connection:
                def diag_hold():
                    holder_in_function.set()
                    release_holder.wait(timeout=5)
                    return 0

                connection.create_function("diag_hold", 0, diag_hold)
                connection.execute(
                    "DELETE FROM notebooks WHERE diag_hold() = 1"
                )
        except BaseException as exc:
            failures.append(exc)

    def wait_for_write():
        try:
            with db.write(operation="sqlite.write"):
                waiter_acquired.set()
        except BaseException as exc:
            failures.append(exc)

    runtime.start()
    holder = threading.Thread(target=hold_delete)
    waiter = threading.Thread(target=wait_for_write)
    try:
        with diagnostics.install_runtime(runtime):
            holder.start()
            assert holder_in_function.wait(timeout=2)
            waiter.start()
            snapshot = _wait_until(
                lambda: (value if value["holder"] and value["waiters"] else None)
                if (value := runtime.snapshot()["write_lock"])
                else None
            )
            assert waiter_acquired.is_set() is False
            frames = sys._current_frames()
            extracted = traceback.extract_stack(frames[holder.ident])
            stack_lines = [
                f"<repo>/{Path(frame.filename).resolve().relative_to(REPO).as_posix()}:"
                f"{getattr(frame, 'lineno')} in {frame.name}"
                for frame in extracted
                if Path(frame.filename).resolve().is_relative_to(REPO)
            ]
            evidence = {
                "captured_at": "2026-07-21T12:00:00+00:00",
                "runtime": runtime.snapshot(),
                "stacks": {format(holder.ident, "x"): stack_lines},
                "db": diag_db.collect_db_evidence(
                    db_path, notebook_id="nb-live-private", deadline_seconds=4
                ),
                "process": {"pid": os.getpid(), "cpu_percent": 1, "d_state_threads": 0},
                "logs": {},
                "degraded": [],
            }
            findings = rules.diagnose(evidence)
            report = rules.render_incident(evidence, findings)
    finally:
        release_holder.set()
        holder.join(timeout=2)
        waiter.join(timeout=2)
        runtime.stop()

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM notebooks WHERE id = ?", ("nb-live-private",)
        ).fetchone()[0] == 1
    assert failures == []
    assert findings[0].code == "sqlite_write_blocking"
    assert "legacy_children" in report
    assert "SCAN" in report
    assert "nb-live-private" not in report
    assert len(report.encode("utf-8")) <= 32768
    assert holder.is_alive() is False
    assert waiter.is_alive() is False


@pytest.mark.parametrize("requested_deadline", (10.0, float("nan")), ids=("maximum", "nan"))
def test_collect_incident_passes_one_deadline_and_bounds_remaining_work(
    tmp_path, monkeypatch, requested_deadline
):
    incident = load_script("diag_incident")
    deadlines = []
    db_deadlines = []

    def resolve(*args, **kwargs):
        deadlines.append(kwargs["deadline"])
        return {"status": "missing", "pid": None, "source": "auto", "candidates": []}

    def probe(*args, **kwargs):
        deadlines.append(kwargs["deadline"])
        return {"role": args[0], "status": None, "elapsed_ms": 0, "result": "refused"}

    class Records:
        records = ()
        stats = type("Stats", (), {
            "malformed": 0,
            "truncated": False,
            "retained": 0,
        })()

    def read(*args, **kwargs):
        deadlines.append(kwargs["deadline"])
        return Records()

    def collect_db(*args, **kwargs):
        db_deadlines.append(kwargs["deadline_seconds"])
        return {"evidence_complete": False, "degraded": [{"category": "missing"}]}

    monkeypatch.setattr(incident, "resolve_backend_pid", resolve)
    monkeypatch.setattr(incident, "probe_http", probe)
    monkeypatch.setattr(incident, "read_channel", read)
    monkeypatch.setattr(incident, "collect_db_evidence", collect_db)

    started = time.monotonic()
    report = incident.collect_incident(
        root=tmp_path,
        deadline_seconds=requested_deadline,
        clock=lambda: 100.0,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 10
    assert len(deadlines) == 6  # pid, two probes, and three log channels
    assert len(set(deadlines)) == 1
    assert all(math.isfinite(value) for value in deadlines)
    assert math.isfinite(db_deadlines[0])
    assert 0 < db_deadlines[0] <= 1
    assert len(report.encode("utf-8")) <= 32768


def test_collect_incident_derives_repeated_stack_io_and_wal_samples(tmp_path, monkeypatch):
    incident = load_script("diag_incident")
    local = tmp_path / ".local"
    local.mkdir()
    (local / "silicon_notebook.db-wal").write_bytes(b"x")
    captures = []
    samples = []
    diagnosed = []

    monkeypatch.setattr(
        incident,
        "resolve_backend_pid",
        lambda *args, **kwargs: {
            "status": "ok",
            "pid": 12,
            "source": "operator",
            "identity": {"pid": 12, "starttime_ticks": 9},
            "candidates": [],
        },
    )
    monkeypatch.setattr(
        incident,
        "load_runtime_snapshot",
        lambda *args, **kwargs: {
            "status": "ok",
            "fresh": True,
            "age_seconds": 0,
                "snapshot": {
                    "state_revision": 7,
                "active_requests": [],
                "active_sql": [],
                "active_jobs": [],
                "write_lock": {"holder": None, "waiters": []},
                "concurrency": {},
            },
        },
    )

    def capture(*args, **kwargs):
        captures.append(1)
        return {"status": "ok", "dump": "Current thread 0x65:\n"}

    monkeypatch.setattr(incident, "capture_thread_dump", capture)
    monkeypatch.setattr(
        incident,
        "parse_thread_dump",
        lambda *args, **kwargs: {
            "65": [repo_frame("backend/app/work.py", 2, "spin")]
        },
    )

    def sample(*args, **kwargs):
        samples.append(1)
        return {
            "pid": 12,
            "cpu_percent": 90,
            "read_bytes": len(samples) * 10,
            "write_bytes": len(samples) * 20,
            "d_state_threads": 0,
        }

    monkeypatch.setattr(incident, "sample_process", sample)
    def probe(role, *args, **kwargs):
        if role == "backend":
            raise OSError("private raw error")
        return {"role": role, "result": "ok", "status": 200, "elapsed_ms": 1}

    monkeypatch.setattr(incident, "probe_http", probe)
    monkeypatch.setattr(
        incident,
        "read_channel",
        lambda *args, **kwargs: type("Records", (), {
            "records": (),
            "stats": type("Stats", (), {
                "malformed": 0, "truncated": False, "retained": 0,
            })(),
        })(),
    )
    monkeypatch.setattr(
        incident,
        "collect_db_evidence",
        lambda *args, **kwargs: {
            "evidence_complete": True,
            "files": {"database_bytes": 10, "wal_bytes": 64 * MIB + 1},
            "degraded": [],
        },
    )
    monkeypatch.setattr(
        incident,
        "diagnose",
        lambda evidence: diagnosed.append(copy.deepcopy(evidence)) or [],
    )

    incident.collect_incident(root=tmp_path, local=local, pid=12)
    captured = diagnosed[0]
    assert len(captures) == 2
    assert len(samples) == 2
    assert captured["main_thread_id"] == 101
    assert len(captured["stack_samples"]) == 2
    assert captured["process"]["read_bytes_delta"] == 10
    assert captured["process"]["write_bytes_delta"] == 20
    assert captured["db"]["wal_growth_bytes"] == 64 * MIB
    assert captured["health"] == {"backend": "unknown", "frontend": "ok"}
    assert all(
        finding.code != "service_split"
        for finding in load_script("diag_rules").diagnose(captured, limit=20)
    )


def test_collect_incident_discards_runtime_that_races_during_capture(tmp_path, monkeypatch):
    incident = load_script("diag_incident")
    seen = []
    revisions = iter((1, 2))
    monkeypatch.setattr(
        incident,
        "resolve_backend_pid",
        lambda *args, **kwargs: {
            "status": "ok",
            "pid": 12,
            "source": "operator",
            "identity": {"pid": 12, "starttime_ticks": 9},
            "candidates": [],
        },
    )

    def load(*args, **kwargs):
        revision = next(revisions)
        return {
            "status": "ok",
            "fresh": True,
            "age_seconds": 0,
            "snapshot": {
                "state_revision": revision,
                "active_requests": [{
                    "method": "DELETE",
                    "phase": "notebook_delete.db",
                    "notebook_id": "nb-private",
                }],
                "active_sql": [],
                "active_jobs": [],
                "write_lock": {"holder": {"thread_id": 1}, "waiters": []},
                "concurrency": {},
            },
        }

    monkeypatch.setattr(incident, "load_runtime_snapshot", load)
    monkeypatch.setattr(
        incident, "capture_thread_dump", lambda *args, **kwargs: {"status": "ok", "dump": ""}
    )
    monkeypatch.setattr(
        incident, "sample_process", lambda *args, **kwargs: {"status": "unavailable"}
    )
    monkeypatch.setattr(
        incident,
        "probe_http",
        lambda role, *args, **kwargs: {"role": role, "result": "ok"},
    )
    monkeypatch.setattr(
        incident,
        "read_channel",
        lambda *args, **kwargs: type("Records", (), {
            "records": (),
            "stats": type("Stats", (), {
                "malformed": 0, "truncated": False, "retained": 0,
            })(),
        })(),
    )
    monkeypatch.setattr(
        incident,
        "collect_db_evidence",
        lambda *args, **kwargs: {"evidence_complete": True, "files": {}, "degraded": []},
    )
    monkeypatch.setattr(
        incident, "diagnose", lambda evidence: seen.append(copy.deepcopy(evidence)) or []
    )

    incident.collect_incident(root=tmp_path, pid=12)
    assert seen[0]["runtime"] == {}
    assert seen[0]["runtime_fresh"] is False
    assert {row["category"] for row in seen[0]["degraded"]} >= {"raced"}


def test_cli_defaults_and_safe_env_parser(tmp_path):
    incident = load_script("diag_incident")
    env = tmp_path / ".env"
    env.write_text(
        "PORT=8123\nFRONTEND_PORT=3456\nBACKEND_HOST=127.0.0.1\n"
        "OPENAI_COMPAT_API_KEY=SENSITIVE-PROMPT\n",
        encoding="utf-8",
    )
    assert incident.read_safe_env(env) == {
        "PORT": "8123",
        "FRONTEND_PORT": "3456",
        "BACKEND_HOST": "127.0.0.1",
    }
    parser = incident.build_parser()
    args = parser.parse_args([])
    assert vars(args) == {
        "root": ".",
        "local": "",
        "pid": None,
        "notebook": "",
        "since": 2.0,
        "output_limit_kib": 32,
        "verbose": False,
    }

    oversized = tmp_path / "oversized.env"
    oversized.write_bytes(b"#" + b"x" * (65 * 1024) + b"\nPORT=9999\n")
    assert incident.read_safe_env(oversized) == {}
    assert incident._loopback_host("::1") == "[::1]"
    assert incident._loopback_host("[::1]") == "[::1]"


@pytest.mark.parametrize("attack", ("fifo", "symlink", "device", "oversize", "permission", "deadline"))
def test_safe_env_open_is_nonblocking_descriptor_safe_and_fixed_category_only(
    tmp_path, monkeypatch, attack
):
    incident = load_script("diag_incident")
    env = tmp_path / ".env"
    victim = tmp_path / "PRIVATE-ENV-VICTIM"
    victim.write_text("PORT=8123\nSENSITIVE-ENV-CONTENT\n", encoding="utf-8")
    categories: list[str] = []
    deadline = time.monotonic() + 1
    original_open = incident.os.open

    if attack == "fifo":
        os.mkfifo(env, mode=0o600)
    elif attack == "symlink":
        env.symlink_to(victim)
    elif attack == "device":
        env.write_bytes(b"")
        device = open(os.devnull, "rb")

        def open_device(path, flags, *args, **kwargs):
            if os.fspath(path) == os.fspath(env):
                return os.dup(device.fileno())
            return original_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(incident.os, "open", open_device)
    elif attack == "oversize":
        env.write_bytes(b"#" + b"x" * (64 * 1024) + b"\nPORT=8123\n")
    elif attack == "permission":
        env.write_bytes(b"PORT=8123\n")

        def deny(path, flags, *args, **kwargs):
            if os.fspath(path) == os.fspath(env):
                raise PermissionError("SENSITIVE-PERMISSION-ERROR")
            return original_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(incident.os, "open", deny)
    else:
        env.write_bytes(b"PORT=8123\n")
        deadline = time.monotonic() - 1

    started = time.monotonic()
    try:
        parsed = incident.read_safe_env(
            env,
            deadline=deadline,
            on_degraded=categories.append,
        )
    finally:
        if attack == "device":
            device.close()
    elapsed = time.monotonic() - started

    assert elapsed < 0.25
    assert parsed == {}
    assert len(categories) == 1
    assert categories[0] in {
        "unsafe_type", "unsafe_link", "oversize", "permission", "deadline"
    }
    assert "PRIVATE" not in categories[0]
    assert "SENSITIVE" not in categories[0]


def test_collect_incident_passes_shared_deadline_to_both_env_reads(tmp_path, monkeypatch):
    incident = load_script("diag_incident")
    seen: list[float] = []

    def read_env(_path, *, deadline, clock, on_degraded):
        seen.append(deadline)
        return {}

    monkeypatch.setattr(incident, "read_safe_env", read_env)
    monkeypatch.setattr(
        incident,
        "resolve_backend_pid",
        lambda *_args, **_kwargs: {
            "status": "missing", "pid": None, "source": "auto", "candidates": []
        },
    )
    monkeypatch.setattr(
        incident,
        "probe_http",
        lambda role, *_args, **_kwargs: {"role": role, "result": "refused"},
    )
    monkeypatch.setattr(
        incident,
        "read_channel",
        lambda *_args, **_kwargs: type("Records", (), {
            "records": (),
            "stats": type("Stats", (), {"malformed": 0, "truncated": False, "retained": 0})(),
        })(),
    )
    monkeypatch.setattr(
        incident,
        "collect_db_evidence",
        lambda *_args, **_kwargs: {"evidence_complete": False, "degraded": []},
    )

    incident.collect_incident(root=tmp_path, clock=lambda: 100.0)

    assert seen == [110.0, 110.0]


def test_incident_scripts_are_stdlib_only_and_do_not_import_app():
    source = (SCRIPTS / "diag_incident.py").read_text(encoding="utf-8")
    rules_source = (SCRIPTS / "diag_rules.py").read_text(encoding="utf-8")
    assert "from app" not in source
    assert "import app" not in source
    assert "from app" not in rules_source
    assert "import app" not in rules_source
