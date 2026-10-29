"""A deep copy and a ``.snbk`` import keep every source's element order, on
SQLite (the PostgreSQL twin is ``tests/postgres/test_copy_element_order_pg.py``;
the world and the checks live in ``tests/copy_element_order_cases.py``).

Also pins the id a copy gives an element (``copied_element_id``): the shapes it
carries, and that it keeps the reading-order key of every pair of elements of
one source."""
from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from app.core.config import Settings
from app.repositories import element_reading_order as order
from app.services.sqlite_repository import SQLiteRepository
from tests import copy_element_order_cases as cases
from tests import notebook_import_cases as import_cases
from tests.test_notebook_archive_export import BACKEND


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("SILICON_NOTEBOOK_ENV_FILE", "")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'order.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    for key, value in import_cases.ENV.items():
        monkeypatch.setenv(key, value)
    repository = SQLiteRepository(Settings())
    try:
        cases.seed(repository, BACKEND)
        yield repository
    finally:
        repository.close()


def test_deep_copy_keeps_every_readers_element_order(repo):
    cases.check_order_kept(repo, cases.deep_copy(repo))


def test_copy_of_the_copy_keeps_it_too(repo):
    """Copying the copy carries every id over again with the same suffix: the
    order holds and no id grows with each generation of copies."""
    first = cases.deep_copy(repo)
    second = repo._runtime.sharing.copy_notebook(first, new_owner_id=cases.OWNER).id
    assert _suffixes(repo, first) == _suffixes(repo, second)
    origin = cases._origins(repo, second)
    for source_id in (cases.BIG, cases.MIXED, cases.LEGACY):
        twin = cases._sources(repo, second)[f"title {source_id}"]
        assert [origin[i][2:] for i in cases.order_cases.read_whole(repo, twin)] == \
            cases.order_cases.read_whole(repo, source_id)


def _suffixes(repo, notebook_id: str) -> dict[str, str]:
    """original id -> what follows ``el-<own source id>-`` in its copy's id."""
    rows = cases._rows(
        repo, "SELECT e.id, e.source_id, e.text FROM source_elements e JOIN sources s "
              "ON s.id = e.source_id WHERE s.notebook_id = ? AND s.source_type <> 'knowhow'",
        (notebook_id,))
    out = {}
    for row in rows:
        prefix = f"el-{row['source_id']}-"
        assert row["id"].startswith(prefix), row["id"]
        out[row["text"]] = row["id"][len(prefix):]
    return out


def test_import_keeps_every_readers_element_order(repo, tmp_path, monkeypatch):
    cases.check_order_kept(repo, cases.export_and_import(repo, tmp_path, monkeypatch))


def test_runbook_quotes_the_scrambled_sources_query():
    """The query the operations runbook gives for copies made before the fix is
    the one ``check_order_kept`` runs on both backends."""
    docs = Path(__file__).resolve().parents[2] / "docs"
    for name in ("operations.md", "operations_zh.md"):
        assert cases.SCRAMBLED_SOURCES_SQL in (docs / name).read_text(encoding="utf-8"), name


@pytest.mark.parametrize(
    "element_id,expected",
    [
        ("el-src-a-0001", "el-src-new-0001"),
        ("el-src-a-10000", "el-src-new-10000"),
        ("el-src-a-0012-g001", "el-src-new-0012-g001"),
        ("el-src-a-late", "el-src-new-late"),
        ("el-promo-0123abcd", "el-src-new-~el-promo-0123abcd"),
        ("el-0123456789abcdef", "el-src-new-~el-0123456789abcdef"),
        ("el-0123456789abcdef-g001", "el-src-new-~el-0123456789abcdef-g001"),
        ("el-src-ab-0001", "el-src-new-~el-src-ab-0001"),
        ("0042", "el-src-new-~0042"),
        # A copy of a copy: the carried mark stays one mark.
        ("el-src-a-~el-promo-0123abcd", "el-src-new-~el-promo-0123abcd"),
        # Nothing to carry: the caller mints a fresh id.
        ("el-src-a-", None),
        ("el-src-a-00 12", None),
        ("el-src-a-0012/../x", None),
        ("", None),
        ("el-src-a-" + "1" * 200, None),
    ],
)
def test_copied_element_id_shapes(element_id, expected):
    assert order.copied_element_id(element_id, "src-a", "src-new") == expected


def test_copied_ids_keep_the_reading_order_of_every_pair():
    """For every pair of one source's elements that carry over, the copy's
    ``(digits, id)`` compares as the original's did -- so with ``created_at``
    kept, every reader orders the copy as it ordered the original."""
    ids = [
        "el-s-0001", "el-s-0002", "el-s-0012", "el-s-0012-g001", "el-s-0012-g002",
        "el-s-0013", "el-s-9999", "el-s-9999-g001", "el-s-10000", "el-s-10001-g001",
        "el-s-123456", "el-promo-00ff", "el-promo-ff00", "el-0abc", "el-zzz",
        "el-0abc-g001", "el-kh-77",
    ]

    def key(source_id, element_id):
        return (order.index_digits(source_id, element_id), element_id.encode())

    for a, b in itertools.combinations(ids, 2):
        new_a = order.copied_element_id(a, "s", "src-new")
        new_b = order.copied_element_id(b, "s", "src-new")
        assert new_a and new_b and new_a != new_b
        before = key("s", a) < key("s", b)
        assert (key("src-new", new_a) < key("src-new", new_b)) == before, (a, b)
        assert order.index_digits("src-new", new_a) == order.index_digits("s", a)
