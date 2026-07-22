from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

from tests.postgres.conftest import (
    _require_dedicated_test_database,
    _url_with_search_path,
)


REPO_ROOT = Path(__file__).resolve().parents[3]


def test_psycopg_binary_and_pool_have_explicit_compatible_major_ranges():
    requirements = {
        line.split("#", 1)[0].strip()
        for line in (REPO_ROOT / "backend" / "requirements.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.split("#", 1)[0].strip()
    }
    assert "psycopg[binary]>=3.2,<4" in requirements
    assert "psycopg-pool>=3.2,<4" in requirements
    assert not any(line.startswith("psycopg[binary,pool]") for line in requirements)


def test_scoped_url_adds_one_search_path_without_losing_other_options():
    schema = f"sn_t4_{'a' * 32}"
    scoped = _url_with_search_path(
        "postgresql://postgres@db.example/silicon_notebook_task4_test?sslmode=require",
        schema,
    )
    query = parse_qsl(urlsplit(scoped).query, keep_blank_values=True)
    assert query == [("sslmode", "require"), ("options", f"-csearch_path={schema}")]


@pytest.mark.parametrize("key", ["options", "OPTIONS", "Options"])
def test_scoped_url_rejects_existing_libpq_options(key):
    schema = f"sn_t4_{'b' * 32}"
    with pytest.raises(RuntimeError, match="options"):
        _url_with_search_path(
            "postgresql://postgres@db.example/silicon_notebook_task4_test"
            f"?{key}=-cstatement_timeout%3D0",
            schema,
        )


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://postgres@db.example/postgres",
        "postgresql://postgres@db.example/silicon_notebook_production",
        "postgresql://postgres@db.example/silicon_notebook_task4",
    ],
)
def test_fixture_refuses_non_dedicated_database_names(url):
    with pytest.raises(RuntimeError, match="dedicated"):
        _require_dedicated_test_database(url)


def test_fixture_refuses_server_database_identity_mismatch():
    url = "postgresql://postgres@db.example/silicon_notebook_task4_test"
    with pytest.raises(RuntimeError, match="does not match"):
        _require_dedicated_test_database(url, "silicon_notebook_other_test")


def test_sqlite_default_subprocess_does_not_import_psycopg(tmp_path):
    env = os.environ.copy()
    env.pop("TEST_POSTGRES_URL", None)
    env["PYTHONPATH"] = str(REPO_ROOT / "backend")
    sqlite_url = f"sqlite:///{tmp_path / 'offline-default.db'}"
    program = f"""
import sys
from app.core.config import Settings
from app.repositories.factory import create_repository
repo = create_repository(Settings(database_url={sqlite_url!r}))
assert not any(name == 'psycopg' or name.startswith('psycopg.') for name in sys.modules)
"""
    completed = subprocess.run(
        [sys.executable, "-c", program],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
