"""Physical attribution of finished global Ask jobs (SQLite v98; PostgreSQL
twin: ``tests/postgres/test_global_ask_attribution_pg.py``; shared cases in
``tests/global_attribution_cases.py``).

The completion write and the offline backfill store the libraries a done job
is attributed to in ``global_ask_job_notebooks``; the notebook analytics panel
counts the global answers attributed to the notebook from it, and from the
payload predicate for the done jobs not yet backfilled. These tests pin the
stored rows to ``attribution_sql`` row by row, the count to the payload-only
reference before, during and after the backfill, and the read plans.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.domain.global_ask_attribution import job_attribution
from app.models.global_ask import GlobalAskJob
from app.repositories.global_ask_store import GlobalAskStore
from app.repositories.sqlite.query_store import global_answers_count_sql
from app.services.sqlite_repository import SQLiteRepository
from tests import global_attribution_cases as cases


@pytest.fixture
def repo(tmp_path):
    settings = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 't.db'}",
                        storage_dir=str(tmp_path / "s"))
    repository = SQLiteRepository(settings)
    with repository._write() as db:
        cases.seed_base(db, "?")
    yield repository
    repository.close()


@pytest.fixture
def legacy(repo):
    """A database upgraded to v98 with every case written before it."""
    with repo._write() as db:
        cases.seed_legacy_jobs(db, "?")
    return repo


def _analytics(repo, notebook_id):
    return repo._runtime.queries.notebook_analytics(notebook_id)


def _reference(repo, notebook_id):
    with repo._runtime.database.connect() as db:
        return cases.live_done_count_via_payload(db, notebook_id, dialect="sqlite", q="?")


def test_completion_writes_the_attribution_of_done_jobs_only(repo):
    cases.create_and_finish(repo._runtime.global_ask_store)
    with repo._runtime.database.connect() as db:
        rows = cases.stored_rows(db)
        flags = cases.attributed_flags(db)
        cases.assert_rows_match_attribution_sql(db, dialect="sqlite", q="?")
    assert rows == cases.expected_rows_sorted()
    done = {job for job, status in cases.statuses().items() if status == "done"}
    assert {job for job, flag in flags.items() if flag} == done - {cases.UNREADABLE_JOB[0]}


def test_completion_rows_are_job_attribution_of_the_saved_job(repo):
    """The write path stores exactly ``job_attribution`` of the job it saves
    (the same call the completion hook makes), in one statement batch with
    the status change."""
    store = repo._runtime.global_ask_store
    job = GlobalAskJob.model_validate_json(cases.all_payloads()["gaj-05"])
    job.status = "running"
    store.create(job, cases.USER, None, "{}", "web", new_conversation=False)
    job.status = "done"
    assert store.save(job, cases.USER)
    assert store.save(job, cases.USER) is False  # done is final; no second write
    with repo._runtime.database.connect() as db:
        assert cases.stored_rows(db)["gaj-05"] == sorted(job_attribution(job))


@pytest.mark.parametrize("notebook_id", sorted(cases.EXPECTED_GLOBAL_ANSWERS))
def test_panel_counts_global_answers_before_and_after_the_backfill(legacy, notebook_id):
    before = cases.answers(_analytics(legacy, notebook_id))
    assert before == cases.expected_answers(notebook_id)
    assert before[1] == _reference(legacy, notebook_id)
    result = cases.backfill(legacy.maintenance)
    assert (result.written, result.unreadable, result.pending_after) == (11, 1, 1)
    assert cases.answers(_analytics(legacy, notebook_id)) == before


def test_half_backfilled_database_counts_the_same(legacy):
    """Mixed rows (some attributed, some not): the two arms are disjoint."""
    cases.backfill(legacy.maintenance, limit=6, batch_size=4)
    with legacy._runtime.database.connect() as db:
        flags = cases.attributed_flags(db)
    assert 0 < sum(flags.values()) < len(flags)
    for notebook_id in cases.EXPECTED_GLOBAL_ANSWERS:
        assert cases.answers(_analytics(legacy, notebook_id)) == cases.expected_answers(notebook_id)


def test_backfilled_rows_match_the_payload_predicate_row_by_row(legacy):
    cases.backfill(legacy.maintenance)
    with legacy._runtime.database.connect() as db:
        cases.assert_rows_match_attribution_sql(db, dialect="sqlite", q="?")
        assert cases.stored_rows(db) == cases.expected_rows_sorted()


def test_backfill_is_resumable_idempotent_and_reports_unreadable(legacy, tmp_path):
    maintenance = legacy.maintenance
    dry = cases.backfill(maintenance, dry_run=True)
    assert (dry.written, dry.pending_after) == (11, 12)
    reported = []
    first = cases.backfill(maintenance, limit=5, batch_size=2, on_unreadable=reported.append)
    assert (first.scanned, first.written, first.last_id) == (5, 5, "gaj-05")
    resumed = cases.backfill(maintenance, after_id=first.last_id, on_unreadable=reported.append)
    assert (resumed.written, resumed.unreadable, resumed.pending_after) == (6, 1, 1)
    assert reported == [cases.UNREADABLE_JOB[0]]
    again = cases.backfill(maintenance)
    assert (again.scanned, again.written, again.unreadable) == (1, 0, 1)
    # A row attributed between read and write is left alone, not re-written.
    assert maintenance.write_global_ask_attributions([("gaj-01", ["nb-gc"])]) == 0
    with legacy._runtime.database.connect() as db:
        assert cases.stored_rows(db)["gaj-01"] == ["nb-ga", "nb-gb"]


def _point_cli_at(monkeypatch, tmp_path):
    """The CLI reads ``Settings()`` from the environment, like in production;
    the admin password it is handed differs from the stored one, so a seed
    step would visibly rewrite it."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("SILICON_NOTEBOOK_ADMIN_PASSWORD", "a-different-admin-password")


def _admin_rows(repo):
    with repo._runtime.database.connect() as db:
        return [tuple(row) for row in db.execute(
            "SELECT id, password_hash, updated_at FROM users WHERE role='admin' ORDER BY id"
        ).fetchall()]


def test_backfill_cli_exit_status_and_report(legacy, tmp_path, monkeypatch, capsys):
    from app.services import global_ask_attribution_backfill as module

    _point_cli_at(monkeypatch, tmp_path)
    report = tmp_path / "r" / "bad.jsonl"
    assert module.main(["--report", str(report), "--batch-size", "3"]) == 1
    assert [json.loads(line) for line in report.read_text().splitlines()] == [
        {"job_id": cases.UNREADABLE_JOB[0], "reason": "payload_unreadable"}
    ]
    assert "全局问题" not in capsys.readouterr().out
    with pytest.raises(SystemExit):
        module.main(["--batch-size", "0"])


def test_backfill_cli_neither_seeds_nor_migrates(legacy, tmp_path, monkeypatch):
    """The CLI does not own the schema: composing it rewrites no admin
    credential (the seed step's unconditional UPDATE would, with the
    different password the environment hands it here), and against a
    database still at v97 it refuses (exit 3) without migrating it."""
    from app.services import global_ask_attribution_backfill as module
    from tests.sqlite_migration_testkit import rollback_v98

    _point_cli_at(monkeypatch, tmp_path)
    admins = _admin_rows(legacy)
    assert admins
    assert module.main(["--dry-run"]) == 1  # the unreadable case; nothing written
    assert _admin_rows(legacy) == admins
    with legacy._runtime.database.write() as db:
        rollback_v98(db)
        db.execute("PRAGMA user_version = 97")
    assert module.main([]) == module.EXIT_SCHEMA_NOT_READY
    with legacy._runtime.database.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 97
        assert db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='global_ask_job_notebooks'"
        ).fetchone()[0] == 0
    assert _admin_rows(legacy) == admins


def test_post_readiness_pass_attributes_everything_once(legacy, caplog):
    from app.services import global_ask_attribution_backfill as module

    submitted = []

    def submit(fn, *args, name):
        submitted.append(name)
        return fn(*args)

    with caplog.at_level("INFO", logger="silicon_notebook.global_ask_attribution"):
        assert module.schedule_attribution_backfill(legacy, is_ready=lambda: False, submit=submit) is None
        assert submitted == []
        result = module.schedule_attribution_backfill(legacy, is_ready=lambda: True, submit=submit)
    assert submitted == [module.JOB_NAME]
    assert (result.written, result.unreadable, result.pending_after) == (11, 1, 1)
    assert "12 done job(s) with attributed=0" in caplog.text
    assert "全局问题" not in caplog.text
    with legacy._runtime.database.connect() as db:
        assert cases.stored_rows(db) == cases.expected_rows_sorted()
    # Only the unreadable job is left: a later start schedules a pass that
    # writes nothing, and a database with nothing pending schedules none.
    again = module.schedule_attribution_backfill(legacy, is_ready=lambda: True, submit=submit)
    assert again.written == 0
    with legacy._runtime.database.write() as db:
        db.execute("DELETE FROM global_ask_jobs WHERE id=?", (cases.UNREADABLE_JOB[0],))
    assert module.schedule_attribution_backfill(legacy, is_ready=lambda: True, submit=submit) is None
    assert submitted == [module.JOB_NAME, module.JOB_NAME]


def test_post_readiness_scheduling_never_raises(legacy):
    from app.services import global_ask_attribution_backfill as module

    class _Broken:
        @property
        def maintenance(self):
            raise RuntimeError("boom")

    assert module.schedule_attribution_backfill(_Broken(), is_ready=lambda: True) is None


def test_startup_wires_the_pass_after_readiness():
    """``run_startup`` calls the scheduler in the post-``mark_ready`` block,
    beside the memory orphan sweep."""
    import inspect

    from app.services import startup_warmup

    source = inspect.getsource(startup_warmup.run_startup)
    assert source.index("_mark_lifecycle_ready(lease, repo)") < source.index(
        "_backfill_global_ask_attribution(repo)"
    )


def test_expired_and_unfinished_jobs_count_nowhere_in_either_arm(legacy):
    for attributed in (False, True):
        if attributed:
            cases.backfill(legacy.maintenance)
        assert cases.answers(_analytics(legacy, "nb-gb"))[1] == 4
    with legacy._runtime.database.connect() as db:
        rows = cases.stored_rows(db)
    for job_id in ("gaj-10", "gaj-11", "gaj-12", "gaj-13"):
        assert job_id not in rows


def test_purge_and_conversation_delete_cascade_to_the_rows(legacy):
    cases.backfill(legacy.maintenance)
    with legacy._write() as db:
        assert GlobalAskStore.purge_expired_turns(db, "2026-06-01T00:00:00+00:00", marker="?") == 1
    with legacy._runtime.database.connect() as db:
        rows = cases.stored_rows(db)
    assert "gaj-14" not in rows and "gaj-01" in rows
    assert legacy._runtime.global_ask_store.delete(cases.CONVERSATION, cases.USER)
    with legacy._runtime.database.connect() as db:
        assert cases.stored_rows(db) == {}


def test_answers_count_reads_the_two_indexes(legacy):
    """The attributed arm seeks ``idx_global_ask_job_notebooks_notebook``; the
    unattributed arm reads the partial ``idx_global_ask_jobs_unattributed``
    (SQLite uses it only because the WHERE repeats its predicate)."""
    statement, binds = global_answers_count_sql()
    with legacy._runtime.database.connect() as db:
        plan = " | ".join(
            row[3] for row in db.execute("EXPLAIN QUERY PLAN " + statement, ("nb-ga",) * binds)
        )
        predicate = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='idx_global_ask_jobs_unattributed'"
        ).fetchone()[0]
    steps = plan.split(" | ")
    assert "SEARCH a USING COVERING INDEX idx_global_ask_job_notebooks_notebook (notebook_id=?)" in steps
    assert "SEARCH j USING INDEX sqlite_autoindex_global_ask_jobs_1 (id=?)" in steps, plan
    # The unattributed arm walks the partial index only (no table scan).
    assert "SCAN global_ask_jobs USING INDEX idx_global_ask_jobs_unattributed" in steps, plan
    assert not [s for s in steps if s.startswith("SCAN global_ask_jobs") and "USING" not in s]
    assert predicate.endswith("WHERE status='done' AND attributed=0")
    assert "status='done' AND attributed=0" in statement


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'ga.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    from app.api import deps
    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    deps.repository.cache_clear()
    return TestClient(create_app())


def test_analytics_endpoint_counts_global_answers_without_revealing_others(client):
    body = client.post("/api/auth/register", json={"username": "d00000081", "password": "pw"}).json()
    owner = {"Authorization": f"Bearer {body['token']}"}
    other = client.post("/api/auth/register", json={"username": "d00000082", "password": "pw"}).json()
    other_headers = {"Authorization": f"Bearer {other['token']}"}
    mine = client.post("/api/notebooks", headers=owner, json={"name": "我的库"}).json()
    theirs = client.post("/api/notebooks", headers=other_headers, json={"name": "别人的秘密库"}).json()
    from app.api.deps import repository

    store = repository()._runtime.global_ask_store
    with repository()._write() as db:
        db.execute(
            "INSERT INTO global_ask_conversations(id,user_id,title,scope_json,submitted_via,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (cases.CONVERSATION, other["user"]["id"], "", "{}", "web", cases.NOW, cases.NOW))
    job = GlobalAskJob.model_validate_json(json.dumps(cases.payload(
        "gask-y", "running", [mine["id"], theirs["id"]], [mine["id"], theirs["id"]], [], [])))
    store.create(job, other["user"]["id"], None, "{}", "web", new_conversation=False)
    job.status = "done"
    assert store.save(job, other["user"]["id"])

    response = client.get(f"/api/notebooks/{mine['id']}/analytics", headers=owner)
    assert response.status_code == 200
    assert (response.json()["answers_total"], response.json()["answers_global"]) == (1, 1)
    for leaked in (theirs["id"], "别人的秘密库", "gask-y", "全局问题"):
        assert leaked not in response.text
