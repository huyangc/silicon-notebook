"""M3 store layer (SQLite): the participant set follows the viewer.

The seven store call sites that resolve "which mounted libraries count" each take
a REQUIRED ``viewer_id`` and bind it to the viewer-scoped mount fragment.  The
world and the expectations are ``tests/mount_viewer_store_cases.py`` (shared with
the PostgreSQL run in ``tests/postgres/test_mount_viewer_store_pg.py``); one test
per call site, so reverting exactly one site to the viewer-independent fragment
turns exactly that site's test red.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.domain.knowledge_contracts import USABLE_STATUSES
from app.services.sqlite_repository import SQLiteRepository
from tests import mount_viewer_store_cases as cases


@pytest.fixture
def store_world(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repo = SQLiteRepository(Settings())
    now = datetime.now(timezone.utc).isoformat()
    with repo._write() as db:
        cases.seed_store_world(db.execute, "?", now)
    runtime = repo._runtime
    notebooks, queries = runtime.notebook_store, runtime.queries
    unified, knowledge = runtime.unified_kg, runtime.knowledge

    def on_db(call):
        def run(*args, **kwargs):
            with runtime.database.connect() as db:
                return call(db, *args, **kwargs)

        return run

    stores = SimpleNamespace(
        resolve_participants=on_db(
            lambda db, nb, v: notebooks.resolve_participants(db, nb, viewer_id=v)
        ),
        participant_ids=on_db(
            lambda db, nb, v: notebooks.participant_ids(db, nb, viewer_id=v)
        ),
        participant_tiers=on_db(
            lambda db, nb, v: notebooks.participant_tiers(db, nb, viewer_id=v)
        ),
        participant_notebook_ids=lambda nb, v: notebooks.participant_notebook_ids(
            nb, viewer_id=v
        ),
        participant_rows=on_db(
            lambda db, nb, v: notebooks.participant_rows(db, nb, viewer_id=v)
        ),
        usable_base_kg=on_db(
            lambda db, nb, v: queries.notebook_has_usable_base_kg(db, nb, viewer_id=v)
        ),
        mounted_bases_row=on_db(
            lambda db, nb, v: queries.mounted_bases_row(db, nb, viewer_id=v)
        ),
        mounted_base_ids=lambda nb, v: unified.mounted_base_ids(nb, viewer_id=v),
        any_mounted_has_kg_on=on_db(
            lambda db, nb, v: knowledge.any_mounted_has_kg_on(db, nb, viewer_id=v)
        ),
        any_mounted_has_kg=lambda nb, v: knowledge.any_mounted_has_kg(nb, viewer_id=v),
        any_mounted_has_kg_compat=lambda nb, v: knowledge.any_mounted_has_kg_compat(
            nb, viewer_id=v
        ),
        follow_start=on_db(
            lambda db, object_id, nb, v: knowledge.follow_start_row(
                db, object_id, nb, USABLE_STATUSES, viewer_id=v
            )
        ),
    )
    raw_calls = SimpleNamespace(
        resolve_participants=on_db(lambda db, nb: notebooks.resolve_participants(db, nb)),
        participant_ids=on_db(lambda db, nb: notebooks.participant_ids(db, nb)),
        participant_tiers=on_db(lambda db, nb: notebooks.participant_tiers(db, nb)),
        participant_notebook_ids=lambda nb: notebooks.participant_notebook_ids(nb),
        participant_rows=on_db(lambda db, nb: notebooks.participant_rows(db, nb)),
        notebook_has_usable_base_kg=on_db(
            lambda db, nb: queries.notebook_has_usable_base_kg(db, nb)
        ),
        mounted_bases_row=on_db(lambda db, nb: queries.mounted_bases_row(db, nb)),
        mounted_base_ids=lambda nb: unified.mounted_base_ids(nb),
        any_mounted_has_kg_on=on_db(lambda db, nb: knowledge.any_mounted_has_kg_on(db, nb)),
        any_mounted_has_kg=lambda nb: knowledge.any_mounted_has_kg(nb),
        any_mounted_has_kg_compat=lambda nb: knowledge.any_mounted_has_kg_compat(nb),
        follow_start_row=on_db(
            lambda db, nb: knowledge.follow_start_row(db, "ko-b", nb, USABLE_STATUSES)
        ),
    )
    return SimpleNamespace(stores=stores, raw=raw_calls)


@pytest.mark.parametrize("site", sorted(cases.SITE_CHECKS))
def test_each_participant_call_site_follows_the_viewer(store_world, site):
    """主人有效、成员无效、成员另有读权则有效;公共库与 everyone 对所有人(含空查看者)
    有效;挂载人未知只剩公共库;空查看者只剩公共库与 everyone。"""
    assert cases.SITE_CHECKS[site](store_world.stores) == []


def test_every_call_site_requires_the_viewer_keyword(store_world):
    """漏传 `viewer_id` 当场 TypeError,不会静默回到与查看者无关的旧集合。"""
    assert cases.required_keyword_failures(store_world.raw) == []
