"""Provision disposable, least-privilege PostgreSQL CI targets."""
import json
import os
import time
from urllib.parse import quote

import psycopg
from psycopg import sql


def configure_ci_logging(connection, connect):
    # RAISE LOG includes its entire migration DO statement by default.
    # Keep messages and their diagnostics, but omit repeated SQL bodies
    # on this disposable CI server (including SQL from real errors).
    connection.execute("ALTER SYSTEM SET log_min_error_statement = 'panic'")
    if not connection.execute("SELECT pg_reload_conf()").fetchone()[0]:
        raise RuntimeError("could not reload PostgreSQL CI logging policy")
    for _ in range(50):
        # Reload is asynchronous. A new session must observe the policy
        # before any test role, database or migration is provisioned.
        with connect() as probe:
            if probe.execute("SHOW log_min_error_statement").fetchone()[0] == "panic":
                return
        time.sleep(0.1)
    raise RuntimeError("PostgreSQL CI logging policy did not become effective")

def verify_ci_resources(connection):
    for setting, expected_bytes in (
        ("shared_buffers", 512 * 1024**2),
        ("max_wal_size", 2 * 1024**3),
    ):
        actual_bytes = connection.execute(
            "SELECT pg_size_bytes(current_setting(%s))", (setting,)
        ).fetchone()[0]
        if actual_bytes != expected_bytes:
            raise RuntimeError(f"unexpected PostgreSQL CI {setting} budget")
    # tmpfs is bounded at 6 GiB; max_wal_size is a soft checkpoint
    # target, not a WAL or total-memory cap. Keep durability intact.
    for setting in ("fsync", "synchronous_commit", "full_page_writes"):
        if connection.execute(
            "SELECT current_setting(%s)", (setting,)
        ).fetchone()[0] != "on":
            raise RuntimeError(f"unexpected {setting} policy")

role = "silicon_notebook_app"
decoy_owner_role = "silicon_notebook_ci_owner_decoy"
password = os.environ["POSTGRES_APP_PASSWORD"]
targets = []
admin_connection = dict(
    host=os.environ["PGHOST"],
    port=os.environ["PGPORT"],
    user=os.environ["PGUSER"],
    password=os.environ["PGPASSWORD"],
    dbname="postgres",
    autocommit=True,
)
with psycopg.connect(**admin_connection) as connection:
    configure_ci_logging(connection, lambda: psycopg.connect(**admin_connection))
    verify_ci_resources(connection)
    connection.execute(
        sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION").format(
            sql.Identifier(role), sql.Literal(password)
        )
    )
    connection.execute(
        sql.SQL(
            "CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOREPLICATION"
        ).format(sql.Identifier(decoy_owner_role))
    )
    connection.execute(
        sql.SQL("GRANT {} TO {}").format(
            sql.Identifier(decoy_owner_role), sql.Identifier(role)
        )
    )
    # Migrations use a database-wide advisory lock. Each worker
    # needs its own database, not merely its own schema.
    for worker in range(4):
        group = {}
        for key, kind, options in (
            ("primary", "ci", "ENCODING 'UTF8'"),
            ("non_c", "non_c", "ENCODING 'UTF8' LOCALE_PROVIDER icu ICU_LOCALE 'en-US'"),
            ("non_utf", "non_utf", "ENCODING 'SQL_ASCII' LC_COLLATE 'C' LC_CTYPE 'C'"),
        ):
            database = f"silicon_notebook_{kind}_w{worker}_test"
            connection.execute(
                sql.SQL("CREATE DATABASE {} WITH OWNER {} {} TEMPLATE template0").format(
                    sql.Identifier(database), sql.Identifier(role), sql.SQL(options)
                )
            )
            group[key] = (
                f"postgresql://{quote(role, safe='')}:{quote(password, safe='')}"
                f"@{os.environ['PGHOST']}:{os.environ['PGPORT']}/{database}"
            )
        targets.append(group)
with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
    output.write(f"targets={json.dumps(targets)}\n")
