"""Seed reuse keeps every KG viewer read and mutable world independent."""
from __future__ import annotations

import sqlite3
from collections import Counter
from pathlib import Path

import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from tests import test_kg_viewer_scope as helpers
from tests.world_templates import (
    OPEN_TIME_REWRITES,
    WORLD_DB_NAME,
    WORLD_STORAGE_NAME,
    world_env,
    world_template,
)

# Expose the same explicit fixtures without introducing another repository
# construction or changing their dependency order.
repo = helpers.repo
scenario = helpers.scenario
scenario_seed = helpers.scenario_seed


def _database_state(path):
    """Every real table, including FTS shadow rows; only known reopen stamps
    of the seeded admin are masked, as in the shared template contract."""
    connection = sqlite3.connect(path)
    try:
        schema = connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name",
        ).fetchall()
        rows = {}
        for kind, name, _table, sql in schema:
            if kind != "table" or str(sql or "").lstrip().upper().startswith("CREATE VIRTUAL"):
                continue
            cursor = connection.execute('SELECT * FROM "' + name.replace('"', '""') + '"')
            columns = [column[0] for column in cursor.description]
            masked = OPEN_TIME_REWRITES.get(name)
            values = []
            for row in cursor:
                if masked and row[columns.index(masked[0])] == masked[1]:
                    row = tuple(
                        "<reopen stamp>" if column in masked[2] else value
                        for column, value in zip(columns, row)
                    )
                values.append(row)
            rows[name] = Counter(values)
        header = tuple(
            connection.execute(f"PRAGMA {name}").fetchone()[0]
            for name in ("user_version", "application_id", "journal_mode")
        )
        return schema, header, rows
    finally:
        connection.close()


def _storage_state(root):
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*") if path.is_file()
    }


def _projections(repo, value):
    answers = {}
    for name in ("a", "b"):
        user = getattr(value, name)
        for object_name in ("engram_s", "engram_ma", "flow", "secret"):
            object_id = getattr(value.ids, object_name)
            try:
                answers[name, "context", object_name] = helpers.as_user(
                    user, repo.node_context, value.nb, object_id,
                )
            except KeyError:
                answers[name, "context", object_name] = "hidden"
        for canonical_name in ("engram_canonical", "secret_canonical"):
            canonical = getattr(value.ids, canonical_name)
            try:
                answers[name, "detail", canonical_name] = helpers.as_user(
                    user, repo.concept_detail, value.nb, canonical,
                )
            except KeyError:
                answers[name, "detail", canonical_name] = "hidden"
        answers[name, "neighbors"] = helpers.as_user(
            user, repo.kg_neighbors, value.nb, value.ids.engram_s,
        )
        answers[name, "types"] = helpers.as_user(
            user, repo.knowledge_types, value.nb,
        )
    return answers


@pytest.mark.parametrize("b_memory", [False, True])
def test_template_copies_preserve_all_seeded_state_and_reader_results(
    b_memory, tmp_path, tmp_path_factory, monkeypatch,
):
    baseline = {}

    def build(root):
        payload = helpers._seed_scenario_at(root, b_memory=b_memory)
        baseline["payload"] = payload
        baseline["database"] = _database_state(root / WORLD_DB_NAME)
        baseline["storage"] = _storage_state(root / WORLD_STORAGE_NAME)
        return payload

    template = world_template(
        tmp_path_factory, f"kg-viewer-differential-{b_memory}", build,
        helpers._reopen_scenario_at, env=helpers._SCENARIO_ENV,
    )
    first_root, first_payload = template.materialize(tmp_path, "first")
    for name, value in {**world_env(first_root), **helpers._SCENARIO_ENV}.items():
        monkeypatch.setenv(name, value)
    first = SQLiteRepository(Settings())
    try:
        first_value = helpers._scenario_from_ids(first, first_payload)
        assert first_payload == baseline["payload"]
        assert _database_state(first.db_path) == baseline["database"]
        assert _storage_state(Path(first.settings.storage_dir)) == baseline["storage"]
        expected = _projections(first, first_value)
        with first._write() as db:
            db.execute("UPDATE notebooks SET name='only in first copy' WHERE id=?",
                       (first_value.nb,))
            db.execute("UPDATE source_elements SET text='copy contamination'")
        # Warm a memo after the mutation, and mutate the returned data as well.
        helpers.as_user(first_value.a, first.knowledge_types, first_value.nb)
        first_value.ids.secret = "changed locally"
        first_payload["nb"] = "changed payload"
        (Path(first.settings.storage_dir) / "only-first-copy").write_text("private")
    finally:
        first.close()

    second_root, second_payload = template.materialize(tmp_path, "second")
    for name, value in world_env(second_root).items():
        monkeypatch.setenv(name, value)
    second = SQLiteRepository(Settings())
    try:
        second_value = helpers._scenario_from_ids(second, second_payload)
        assert second_payload == baseline["payload"]
        assert _database_state(second.db_path) == baseline["database"]
        assert _storage_state(Path(second.settings.storage_dir)) == baseline["storage"]
        assert second is not first and second._runtime is not first._runtime
        assert second_value.a is not first_value.a
        assert second_value.ids is not first_value.ids
        assert _projections(second, second_value) == expected
        assert str(second_root) in str(second.db_path)
        assert str(first_root) not in repr(second_payload)
    finally:
        second.close()
    template.verify_pristine()


@pytest.fixture
def scenario_reader_spy(repo, monkeypatch):
    seen = []
    original = repo._runtime.knowledge.node_context

    def spy(*args, **kwargs):
        seen.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(repo._runtime.knowledge, "node_context", spy)
    return seen


def test_runtime_spies_remain_on_the_repository_the_scenario_reads(
    repo, scenario_reader_spy, scenario,
):
    assert scenario_reader_spy == []
    result = helpers.as_user(scenario.b, repo.node_context, scenario.nb,
                             scenario.ids.engram_s)
    assert result["definition"] == "VISIBLE definition of Engram"
    assert len(scenario_reader_spy) == 1


def test_consumers_without_a_scenario_still_receive_an_empty_repository(repo, tmp_path):
    assert repo.db_path == tmp_path / "t.db"
    with repo._connect() as db:
        assert db.execute("SELECT count(*) FROM notebooks").fetchone()[0] == 0
        assert [row[0] for row in db.execute("SELECT id FROM users")] == ["user-local"]


def test_changed_settings_keep_the_original_real_builder(monkeypatch, request):
    dimension = 16 if helpers._SCENARIO_DEFAULTS["embed_dim"] != 16 else 8
    monkeypatch.setenv("EMBED_DIM", str(dimension))
    calls = []
    original = helpers.build_scenario

    def build(repo, *, b_memory):
        calls.append((repo, b_memory))
        return original(repo, b_memory=b_memory)

    monkeypatch.setattr(helpers, "build_scenario", build)
    value = request.getfixturevalue("scenario")
    repository = request.getfixturevalue("repo")
    assert calls == [(repository, False)]
    assert repository.settings.embed_dim == dimension
    assert helpers.as_user(value.b, repository.node_context, value.nb,
                           value.ids.engram_s)["definition"] == "VISIBLE definition of Engram"
