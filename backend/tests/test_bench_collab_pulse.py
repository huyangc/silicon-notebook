"""Hermetic checks of the benchmark tool, never evidence of API performance."""

import asyncio
import builtins
import importlib.util
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "bench_collab_pulse.py"
SPEC = importlib.util.spec_from_file_location("bench_collab_pulse", SCRIPT)
bench = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bench)


def test_phase_distribution_does_not_turn_300_tabs_into_a_burst():
    phases = [bench.tab_phase(index, 300, 15) for index in range(300)]
    assert phases[0] == 0
    assert phases[-1] < 15
    assert len(set(phases)) == 300
    assert [sum(second <= value < second + 1 for value in phases) for second in range(15)] == [20] * 15


def test_tab_honors_response_backoff_and_schedules_after_completion():
    now = [0.0]
    requested = []
    returned = iter([2, 5, 30])

    async def sleep(seconds):
        now[0] += seconds

    async def pulse(lag):
        requested.append((now[0], lag))
        now[0] += 0.25
        return next(returned)

    asyncio.run(bench.scheduled_tab(1, 2, 2, 10, pulse, clock=lambda: now[0], sleep=sleep))
    assert requested == [(1.0, 0), (3.25, 0), (8.5, 0)]


@pytest.mark.parametrize("response", [0, -1, True, float("inf"), float("nan"), "15"])
def test_tab_refuses_broken_intervals_without_retry(response):
    calls = []

    async def pulse(_lag):
        calls.append(1)
        return response

    with pytest.raises(bench.BenchmarkRefused):
        asyncio.run(bench.scheduled_tab(0, 1, 15, 30, pulse))
    assert calls == [1]


def test_nearest_rank_percentiles_include_a_small_tail():
    assert bench.distribution([9, 1, 2, 4, 3]) == {"count": 5, "p50_ms": 3, "p95_ms": 9, "p99_ms": 9}
    assert bench.distribution([]) == {"count": 0, "p50_ms": None, "p95_ms": None, "p99_ms": None}


@pytest.mark.parametrize("arguments", [[], ["--help"]])
def test_default_and_help_never_import_app_or_open_services(monkeypatch, capsys, arguments):
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        assert name.split(".")[0] not in {"app", "psycopg", "httpx"}
        return original_import(name, *args, **kwargs)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("dry/help must not execute external work")

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(bench.subprocess, "run", forbidden)
    monkeypatch.setattr(bench.subprocess, "check_output", forbidden)
    if arguments:
        with pytest.raises(SystemExit) as stopped:
            bench.main(arguments)
        assert stopped.value.code == 0
    else:
        assert bench.main(arguments) == 0
        receipt = json.loads(capsys.readouterr().out)
        assert receipt["status"] == "prepared"
        assert receipt["measured"] is False
        assert "head" not in receipt


@pytest.mark.parametrize("arguments", [["--tabs", "0"], ["--cycles", "1"], ["--read-rate", "nan"]])
def test_invalid_protocol_parameters_are_rejected(arguments):
    with pytest.raises(SystemExit) as stopped:
        bench.main(arguments)
    assert stopped.value.code == 2


def test_synthetic_public_readers_have_real_read_access_without_polling(tmp_path, monkeypatch):
    from app.core.config import Settings
    from app.services.sqlite_repository import SQLiteRepository

    repository = SQLiteRepository(Settings(database_url=f"sqlite:///{tmp_path / 'benchmark.db'}",
                                            storage_dir=str(tmp_path / "storage"),
                                            event_log_enabled=False, llm_log_enabled=False))
    try:
        database = repository._runtime.database
        write = database.write

        class SeedConnection:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, sql, params=()):
                return self.connection.execute(sql.replace("%s", "?"), params)

            def __getattr__(self, name):
                return getattr(self.connection, name)

        @contextmanager
        def seed_write():
            with write() as connection:
                yield SeedConnection(connection)

        # The seed's dialect difference is placeholders only; run its real
        # records through the ordinary SQLite authorization and pulse gates.
        with monkeypatch.context() as patch:
            patch.setattr(database, "write", seed_write)
            readers, writer, _tokens = bench.seed(repository, 1)
        access = repository._runtime.sharing_store
        for notebook in ("pulse-public", "pulse-everyone"):
            assert access.notebook_access_level(notebook, readers[0]).level == "read"
            assert access.collab_pulse_wanted(notebook, readers[0]) is False
            assert access.collab_pulse_wanted(notebook, writer) is True
            assert access.collab_pulse_wanted(notebook, "user-local") is True
    finally:
        repository.close()


@pytest.mark.parametrize("extra_business_read", ["none", "auth", "level", "other"])
@pytest.mark.parametrize("pool_health_check", [False, True])
def test_sql_probe_reports_client_resets_without_hiding_extra_business_reads(monkeypatch, extra_business_read, pool_health_check):
    import psycopg
    from psycopg_pool import ConnectionPool

    monkeypatch.setattr(psycopg.Connection, "execute", lambda *_args, **_kwargs: None)
    database = SimpleNamespace(_restore_client_defaults=lambda: psycopg.Connection.execute(None, "SELECT client_reset"))
    connection = SimpleNamespace(autocommit=True, execute=lambda query: psycopg.Connection.execute(None, query))
    # Use the actual driver's callback, at the callback seat used by getconn.
    # It issues an empty health-check query before either business read.
    database._pool = SimpleNamespace(_check=ConnectionPool.check_connection if pool_health_check else lambda _conn: None)

    def unexpected_read(scope):
        if extra_business_read == scope:
            psycopg.Connection.execute(None, "SELECT unexpected")

    def auth(*_args):
        database._pool._check(connection)
        database._restore_client_defaults()
        psycopg.Connection.execute(None, "SELECT session")
        psycopg.Connection.execute(None, "SELECT user")
        unexpected_read("auth")

    def access(*_args):
        database._pool._check(connection)
        database._restore_client_defaults()
        psycopg.Connection.execute(None, "SELECT authority")
        unexpected_read("level")

    runtime = SimpleNamespace(database=database, identity=SimpleNamespace(resolve_session=auth),
                              sharing=SimpleNamespace(notebook_access_level=access))

    class Client:
        async def post(self, *_args, **_kwargs):
            runtime.identity.resolve_session()
            runtime.sharing.notebook_access_level()
            unexpected_read("other")
            return SimpleNamespace(status_code=200)

    probe = bench.sql_probe(Client(), SimpleNamespace(_runtime=runtime), "synthetic", "synthetic", "epoch:0")
    if extra_business_read != "none":
        with pytest.raises(bench.BenchmarkRefused):
            asyncio.run(probe)
    else:
        result = asyncio.run(probe)
        assert result["sql_counts"] == {"auth": 2, "level": 1}
        assert result["client_reset_sql"] == 2
        assert result["extra_sql"] == 0
        if pool_health_check:
            assert result["pool_check_sql"] == 2
