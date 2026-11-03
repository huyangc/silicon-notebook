#!/usr/bin/env python3
"""Opt-in, model-free collaboration benchmark against an owned temporary PG DB.

Import/help/dry-run use only the standard library. This is a measurement tool,
not a gate: a prepared receipt or a unit test is never performance evidence.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import logging
import math
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from contextlib import ExitStack
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

PHASE = ContextVar("collab_benchmark_phase", default="setup")
SQL_TRACE = ContextVar("collab_benchmark_sql", default=None)
SQL_OWNER = ContextVar("collab_benchmark_sql_owner", default="other")


class BenchmarkRefused(Exception):
    """Fixed, content-free failure; never expose application exception text."""


def distribution(values):
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "p50_ms": None, "p95_ms": None, "p99_ms": None}
    return {"count": len(ordered), **{
        f"p{percentile}_ms": ordered[math.ceil(len(ordered) * percentile / 100) - 1]
        for percentile in (50, 95, 99)
    }}


def tab_phase(index, tabs, interval):
    """Spread first arrivals across one interval, rather than a 300-request burst."""
    return index * interval / tabs


async def scheduled_tab(index, tabs, interval, duration, pulse, *, clock=time.monotonic,
                        sleep=asyncio.sleep):
    """One visible tab: fixed phase, then response-driven, completion-to-next delay."""
    started = clock()
    due = started + tab_phase(index, tabs, interval)
    while due < started + duration:
        await sleep(max(0, due - clock()))
        if clock() >= started + duration:
            break
        next_interval = await pulse(max(0, clock() - due) * 1000)
        if type(next_interval) not in (int, float) or not math.isfinite(next_interval) or next_interval <= 0:
            raise BenchmarkRefused()
        due = clock() + next_interval


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--execute", action="store_true", help="create a unique disposable local PG DB and measure")
    result.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1], help="final integrated checkout to load")
    result.add_argument("--tabs", type=int, default=300, help="benchmark population, not a production limit")
    result.add_argument("--cycles", type=int, default=3, help="group load duration in default pulse intervals (minimum 2)")
    result.add_argument("--read-rate", type=float, default=5, help="other GET requests per second, in both comparison phases")
    result.add_argument("--output", type=Path, help="write content-free JSON receipt here instead of stdout")
    return result


def owned_command(program, username, database):
    command = [program, "-h", "localhost", "-U", username]
    if program == "dropdb":
        command.append("--force")
    try:
        return subprocess.run(command + [database], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, check=False).returncode == 0
    except OSError:
        return False


def seed(repo, tabs):
    """Synthetic fixtures only; authorizations are evaluated by the real API."""
    runtime = repo._runtime
    now = datetime.now(timezone.utc)
    users = [f"pulse-reader-{index}" for index in range(tabs)]
    writer = "pulse-writer"
    with runtime.database.write() as connection:
        for user in users + [writer]:
            connection.execute(
                "INSERT INTO users (id,email,display_name,role,status,username,created_at,updated_at) "
                "VALUES (%s,%s,%s,'user','active',%s,%s,%s)",
                (user, f"{user}@benchmark.invalid", "Synthetic participant", user, now, now),
            )
            connection.execute(
                "INSERT INTO user_profiles (id,user_id,created_at,updated_at) VALUES (%s,%s,%s,%s)",
                (f"profile-{user}", user, now, now),
            )
        for notebook, tier in (("pulse-group", "personal"), ("pulse-public", "base"), ("pulse-everyone", "personal")):
            connection.execute(
                "INSERT INTO notebooks (id,name,created_by,status,tier,created_at,updated_at) "
                "VALUES (%s,'Synthetic benchmark','user-local','ready',%s,%s,%s)",
                (notebook, tier, now, now),
            )
        connection.execute(
            "INSERT INTO groups (id,name,kind,description,created_by,owner_id,created_at,updated_at) "
            "VALUES ('pulse-group-members','Synthetic benchmark','project','','user-local','user-local',%s,%s)",
            (now, now),
        )
        for user in users:
            connection.execute(
                "INSERT INTO group_members (group_id,user_id,role,added_at,added_by) "
                "VALUES ('pulse-group-members',%s,'member',%s,'user-local')", (user, now),
            )
        grants = [("pulse-group", "group", "pulse-group-members", "viewer"),
                  ("pulse-public", "everyone", "", "viewer"),
                  ("pulse-everyone", "everyone", "", "viewer")]
        grants += [(notebook, "user", writer, "editor") for notebook in ("pulse-public", "pulse-everyone")]
        for index, (notebook, kind, principal, role) in enumerate(grants):
            connection.execute(
                "INSERT INTO notebook_grants (id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
                "VALUES (%s,%s,%s,%s,%s,'user-local',%s)",
                (f"pulse-grant-{index}", notebook, kind, principal, role, now),
            )
    tokens = {user: runtime.identity.create_session(user) for user in users + [writer, "user-local"]}
    return users, writer, tokens


def instrument_pool(stack, database, waits):
    from unittest.mock import patch

    original = database._pool.getconn

    def measured(*args, **kwargs):
        started = time.perf_counter_ns()
        try:
            return original(*args, **kwargs)
        finally:
            waits[PHASE.get()].append((time.perf_counter_ns() - started) / 1_000_000)

    stack.enter_context(patch.object(database._pool, "getconn", measured))


async def sql_probe(client, repo, token, notebook, cursor):
    """One non-timed real pulse; attribute SQL to its actual auth/level callers."""
    from unittest.mock import patch

    import psycopg

    traces = []
    calls = Counter()
    original_execute = psycopg.Connection.execute

    def execute(connection, query, *args, **kwargs):
        target = SQL_TRACE.get()
        if target is not None:
            text = query if isinstance(query, str) else query.as_string(connection)
            target.append((SQL_OWNER.get(), hashlib.sha256(text.encode()).hexdigest()))
        return original_execute(connection, query, *args, **kwargs)

    def observed(name, original, *, count_call=True):
        def invoke(*args, **kwargs):
            if count_call:
                calls[name] += 1
            marker = SQL_OWNER.set(name)
            try:
                return original(*args, **kwargs)
            finally:
                SQL_OWNER.reset(marker)
        return invoke

    runtime = repo._runtime
    with ExitStack() as stack:
        stack.enter_context(patch.object(psycopg.Connection, "execute", execute))
        stack.enter_context(patch.object(runtime.identity, "resolve_session", observed("auth", runtime.identity.resolve_session)))
        stack.enter_context(patch.object(runtime.sharing, "notebook_access_level", observed("level", runtime.sharing.notebook_access_level)))
        # Attribute the existing per-checkout client-state reset separately.
        # Keep every driver statement visible while checking the application's
        # auth-plus-one-authority-query contract without counting pool plumbing
        # as an additional authority lookup.
        stack.enter_context(patch.object(runtime.database, "_restore_client_defaults", observed(
            "client_reset", runtime.database._restore_client_defaults, count_call=False)))
        # The configured checkout health callback executes an empty query.
        # Attribute at its actual callback seat, never by matching SQL text:
        # an unexpected read elsewhere must still fail the business budget.
        if callable(runtime.database._pool._check):
            stack.enter_context(patch.object(runtime.database._pool, "_check", observed(
                "pool_check", runtime.database._pool._check, count_call=False)))
        marker = SQL_TRACE.set(traces)
        try:
            response = await client.post(f"/api/notebooks/{notebook}/pulse", headers={"Authorization": f"Bearer {token}"},
                                         json={"since": cursor, "visible": True})
        finally:
            SQL_TRACE.reset(marker)
    counts = Counter(owner for owner, _digest in traces)
    if response.status_code != 200 or calls != {"auth": 1, "level": 1} or counts["other"] or counts["level"] != 1 or counts["auth"] != 2:
        raise BenchmarkRefused()
    return {"calls": dict(calls), "sql_counts": {owner: count for owner, count in counts.items() if owner not in ("client_reset", "pool_check") and count},
            "client_reset_sql": counts["client_reset"], "pool_check_sql": counts["pool_check"], "driver_sql_counts": dict(counts),
            "sql_digests": [digest for _, digest in traces], "extra_sql": 0}


async def measure(app, repo, options, users, writer, tokens, httpx):
    interval = repo.settings.collab_pulse_interval_seconds
    waits = defaultdict(list)
    timings = defaultdict(list)
    statuses = Counter()
    pulses = Counter()
    lateness = defaultdict(list)
    intervals = Counter()
    sem = asyncio.Semaphore(repo.settings.postgres_pool_max_size)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://benchmark.invalid") as client:
        async def request(method, path, actor, phase, **kwargs):
            marker = PHASE.set(phase)
            started = time.perf_counter_ns()
            try:
                response = await client.request(method, path, headers={"Authorization": f"Bearer {tokens[actor]}"}, **kwargs)
            finally:
                timings[phase].append((time.perf_counter_ns() - started) / 1_000_000)
                PHASE.reset(marker)
            statuses[f"{phase}:{response.status_code}"] += 1
            if response.status_code != 200:
                raise BenchmarkRefused()
            return response.json()

        async def detail(notebook, actor):
            async with sem:
                payload = await request("GET", f"/api/notebooks/{notebook}", actor, "setup")
            if type(payload.get("collab_pulse")) is not bool or not isinstance(payload.get("pulse_cursor"), str):
                raise BenchmarkRefused()
            return payload

        groups = await asyncio.gather(*(detail("pulse-group", user) for user in users))
        if not all(item["collab_pulse"] for item in groups):
            raise BenchmarkRefused()
        publics = {}
        positive = []
        for notebook in ("pulse-public", "pulse-everyone"):
            publics[notebook] = await asyncio.gather(*(detail(notebook, user) for user in users))
            for actor in ("user-local", writer):
                payload = await detail(notebook, actor)
                if not payload["collab_pulse"]:
                    raise BenchmarkRefused()
                positive.append((notebook, actor, payload))

        probes = {}
        for role, actor, payload in (("reader", users[0], groups[0]), ("owner", "user-local", await detail("pulse-group", "user-local"))):
            probes[role] = await sql_probe(client, repo, tokens[actor], "pulse-group", payload["pulse_cursor"])
        if probes["reader"]["sql_digests"] != probes["owner"]["sql_digests"]:
            raise BenchmarkRefused()

        def tab(notebook, actor, payload, phase, index, population, duration):
            cursor = payload["pulse_cursor"]

            async def pulse(lag):
                nonlocal cursor
                lateness[phase].append(lag)
                pulses[phase] += 1
                data = await request("POST", f"/api/notebooks/{notebook}/pulse", actor, phase,
                                     json={"since": cursor, "visible": True})
                if (not isinstance(data.get("cursor"), str) or type(data.get("reset")) is not bool
                        or not isinstance(data.get("changes"), list) or not isinstance(data.get("viewers"), list)
                        or type(data.get("other_reader_count")) is not int or type(data.get("next_interval_s")) is not int):
                    raise BenchmarkRefused()
                cursor = data["cursor"]
                intervals[data["next_interval_s"]] += 1
                return data["next_interval_s"]

            if not payload["collab_pulse"]:
                return asyncio.sleep(0)
            return scheduled_tab(index, population, interval, duration, pulse)

        async def background_reads(duration, phase):
            sequence = 0

            async def read(_lag):
                nonlocal sequence
                endpoint, path = (("detail", "/api/notebooks/pulse-group"), ("list", "/api/notebooks"))[sequence % 2]
                sequence += 1
                # Two normal authenticated reads on the same machine/pool.
                await request("GET", path, users[0], f"{phase}:{endpoint}")
                return 1 / options.read_rate
            await scheduled_tab(0, 1, interval, duration, read)

        with ExitStack() as stack:
            instrument_pool(stack, repo._runtime.database, waits)
            await background_reads(interval * options.cycles, "read_baseline")
            group_started = time.monotonic()
            await asyncio.gather(background_reads(interval * options.cycles, "read_loaded"), *(
                tab("pulse-group", user, payload, "group_pulse", index, options.tabs, interval * options.cycles)
                for index, (user, payload) in enumerate(zip(users, groups))
            ))
            group_elapsed = time.monotonic() - group_started
            public_tasks = [tab(notebook, user, payload, notebook, index, options.tabs, interval)
                            for notebook, payloads in publics.items()
                            for index, (user, payload) in enumerate(zip(users, payloads))]
            public_tasks += [tab(notebook, actor, payload, "public_owner_writer", index, len(positive), interval)
                             for index, (notebook, actor, payload) in enumerate(positive)]
            await asyncio.gather(*public_tasks)
            pool_stats = repo._runtime.database.pool_wait_stats()
        if any(pulses[notebook] for notebook in publics) or pulses["public_owner_writer"] < len(positive):
            raise BenchmarkRefused()
        differences = {}
        for endpoint in ("detail", "list"):
            baseline = distribution(timings[f"read_baseline:{endpoint}"])
            loaded = distribution(timings[f"read_loaded:{endpoint}"])
            if not baseline["count"] or not loaded["count"]:
                raise BenchmarkRefused()
            differences[endpoint] = {key: loaded[key] - baseline[key] for key in ("p50_ms", "p95_ms", "p99_ms")}
        waits["with_group_load"] = [value for key, samples in list(waits.items())
                                    if key == "group_pulse" or key.startswith("read_loaded:") for value in samples]
        return {
            "scenarios": {
                "group": {"tabs": options.tabs, "duration_s": group_elapsed, "pulse_requests": pulses["group_pulse"],
                          "actual_pulse_per_s": pulses["group_pulse"] / group_elapsed,
                          "nominal_pulse_per_s": options.tabs / interval},
                "public": {"tabs_each": options.tabs, "detail_false": {key: sum(not item["collab_pulse"] for item in value) for key, value in publics.items()},
                           "actual_pulse": {key: pulses[key] for key in publics}, "owner_writer_pulse": pulses["public_owner_writer"]},
            },
            "latencies": {key: distribution(value) for key, value in timings.items() if key != "setup"},
            "read_latency_delta_ms": differences,
            "pool_checkout_wait": {key: distribution(value) for key, value in waits.items()},
            "pool_stats": {key: value for key, value in pool_stats.items() if isinstance(value, (int, float))},
            "schedule_lateness": {key: distribution(value) for key, value in lateness.items()},
            "returned_intervals": dict(intervals), "statuses": dict(statuses), "sql_probe": probes,
        }


def execute(options, receipt):
    """All application imports, environment isolation and DB ownership begin here."""
    from unittest.mock import patch

    repo_path = options.repo.resolve()
    database = f"silicon_notebook_pulse_bench_{secrets.token_hex(8)}"
    username = getpass.getuser()
    created = False
    repo = None
    attempts = []
    receipt.update(status="failed", stage="load", pg_cleanup=False, external_http_attempts=0)
    previous_environment = os.environ.copy()
    previous_disable = logging.root.manager.disable
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def interrupted(_signum, _frame):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, interrupted)
    try:
        # Empty Settings sources; never inherit a production DB/model/plugin environment.
        os.environ.clear()
        os.environ.update({key: previous_environment[key] for key in ("PATH", "HOME", "TMPDIR", "SYSTEMROOT") if key in previous_environment})
        os.environ.update(SILICON_NOTEBOOK_ENV_FILE="", MODEL_SERVICES_CONFIG="", EXTENSIONS_CONFIG="", ALLOW_NO_ENV_FILE="1")
        logging.disable(logging.CRITICAL)
        sys.path.insert(0, str(repo_path / "backend"))
        import httpx
        import psycopg

        original_async_send = httpx.AsyncClient.send

        def forbidden(*_args, **_kwargs):
            attempts.append(1)
            raise BenchmarkRefused()

        async def checked_send(client, request, **kwargs):
            if isinstance(client._transport, httpx.ASGITransport) and request.url.host == "benchmark.invalid":
                return await original_async_send(client, request, **kwargs)
            return forbidden()

        with ExitStack() as stack:
            storage = stack.enter_context(tempfile.TemporaryDirectory(prefix="pulse-bench-"))
            stack.enter_context(patch.object(httpx.Client, "send", forbidden))
            stack.enter_context(patch.object(httpx.AsyncClient, "send", checked_send))
            from app.core import config

            url = f"postgresql://{quote(username, safe='')}@localhost/{database}"
            settings = config.Settings(_env_file=None, database_url=url, storage_dir=storage,
                                       diagnostics_dir=storage, event_log_enabled=False, model_services_config="", extensions_config="")
            stack.enter_context(patch.object(config, "get_settings", lambda: settings))
            receipt["config"] = {"tabs": options.tabs, "cycles": options.cycles, "read_rate": options.read_rate,
                                 "interval_s": settings.collab_pulse_interval_seconds,
                                 "pool_max": settings.postgres_pool_max_size, "pool_min": settings.postgres_pool_min_size,
                                 "slow_ms": settings.collab_pulse_pool_wait_slow_ms,
                                 "very_slow_ms": settings.collab_pulse_pool_wait_very_slow_ms}
            if not owned_command("createdb", username, database):
                raise BenchmarkRefused()
            created = True
            with psycopg.connect(url, autocommit=True) as connection:
                connection.execute("CREATE EXTENSION pg_trgm")
                connection.execute("CREATE EXTENSION btree_gin")
            from app.api import deps
            from app.bootstrap import create_application_repository

            receipt["stage"] = "seed"
            repo = create_application_repository(settings)

            def close_repository():
                nonlocal repo
                closing, repo = repo, None
                closing.close()

            stack.callback(close_repository)
            users, writer, tokens = seed(repo, options.tabs)
            stack.enter_context(patch.object(deps, "repository", lambda: repo))
            from app.core import readiness
            from app.main import app

            receipt["stage"] = "api_measurement"
            paths = {getattr(route, "path", "") for route in app.routes}
            if "/api/notebooks/{notebook_id}/pulse" not in paths:
                raise BenchmarkRefused()
            readiness.mark_ready()
            receipt.update(asyncio.run(measure(app, repo, options, users, writer, tokens, httpx)))
            if attempts:
                raise BenchmarkRefused()
            receipt["status"] = "measured"
            receipt["stage"] = "complete"
    except KeyboardInterrupt:
        receipt["status"] = "interrupted"
    except (Exception, SystemExit):
        receipt["status"] = "failed"
    finally:
        if repo is not None:
            try:
                repo.close()
            except Exception:
                receipt["status"] = "failed"
        if created:
            receipt["pg_cleanup"] = owned_command("dropdb", username, database)
            if not receipt["pg_cleanup"]:
                receipt["status"] = "cleanup_failed"
        receipt["external_http_attempts"] = len(attempts)
        os.environ.clear()
        os.environ.update(previous_environment)
        logging.disable(previous_disable)
        signal.signal(signal.SIGTERM, previous_sigterm)


def main(argv=None):
    command = parser()
    options = command.parse_args(argv)
    if options.tabs < 1 or options.cycles < 2 or not math.isfinite(options.read_rate) or options.read_rate <= 0:
        command.error("tabs must be positive; cycles >= 2; read-rate must be finite and positive")
    receipt = {"status": "prepared", "measured": False, "protocol": "collab-pulse-t10-v1",
               "scenarios": ["group_visible_tabs", "public_base_and_everyone_readers"],
               "tabs": options.tabs, "interval_source": "Settings.collab_pulse_interval_seconds",
               "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    if options.execute:
        try:
            receipt["head"] = subprocess.check_output(["git", "-C", str(options.repo), "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True).strip()
        except (OSError, subprocess.CalledProcessError):
            receipt["status"] = "failed"
        else:
            execute(options, receipt)
        receipt["measured"] = receipt["status"] == "measured"
    encoded = json.dumps(receipt, ensure_ascii=False, indent=2) + "\n"
    if options.output:
        options.output.write_text(encoded)
    else:
        print(encoded, end="")
    return 0 if receipt["status"] in ("prepared", "measured") else 1


if __name__ == "__main__":
    raise SystemExit(main())
