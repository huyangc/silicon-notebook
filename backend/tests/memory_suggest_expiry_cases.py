"""Shared SQLite/PostgreSQL expiry cases through the startup maintenance pass."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from app.core.config import Settings
from app.domain.memory_suggest_contracts import LIFECYCLE_SUGGESTION_EXPIRED
from app.services import startup_warmup
from tests.memory_suggest_adoption_cases import OLD, World, wiring
from tests.memory_suggest_write_cases import MEMBER, NB, NOW, suggestion


def expiry_repo(world: World, *, limit: int | None = None) -> SimpleNamespace:
    settings = Settings(
        _env_file=None,
        auto_memory_suggest_enabled=False,
        auto_memory_suggest_clarified_enabled=False,
        auto_memory_suggest_repeated_enabled=False,
        auto_memory_suggest_corrected_enabled=False,
        auto_memory_suggest_expiry_sweep_limit=500,
    )
    if limit is not None:
        settings = settings.model_copy(update={"auto_memory_suggest_expiry_sweep_limit": limit})
    return SimpleNamespace(
        settings=settings, _runtime=SimpleNamespace(memory_service=world.service)
    )


def assert_30_days_to_rejected_expired_revision_and_event(world: World) -> None:
    world.seed()
    world.suggest_rows(
        replace(suggestion("expired"), created_at=OLD, updated_at=OLD),
        replace(
            suggestion("at-boundary"), created_at="2026-09-29T08:00:00+00:00",
            updated_at="2026-09-29T08:00:00+00:00",
        ),
    )
    world.reset()
    assert startup_warmup._expire_memory_suggestions(expiry_repo(world)) == 1
    assert world.status("expired") == "rejected"
    assert world.status("at-boundary") == "candidate"
    revisions = world.env.rows(
        "SELECT revision, change_reason, status FROM memory_revisions "
        "WHERE memory_id = ? ORDER BY revision", ("expired",),
    )
    assert [(r["revision"], r["change_reason"], r["status"]) for r in revisions] == [
        (1, "suggested", "candidate"), (2, "expired", "rejected"),
    ]
    assert world.events.items == [{
        "kind": "memory_lifecycle", "memory_id": "expired", "notebook_id": NB,
        "owner_id": MEMBER, "origin": "ask_answer", "status": "rejected",
        "action": LIFECYCLE_SUGGESTION_EXPIRED, "suggested_signal": "clarified",
    }]
    assert world.embeds == world.kg == []


def assert_adopted_and_manual_and_agent_candidates_untouched(world: World) -> None:
    world.seed()
    states = {
        "adopted": "confirmed", "deprecated": "deprecated", "ignored": "rejected",
        "manual": "candidate", "agent": "candidate",
    }
    for memory_id, status in states.items():
        world.memory(
            memory_id, title=memory_id, status=status, created_at=OLD,
            signal="clarified" if memory_id in {"adopted", "deprecated", "ignored"} else None,
            origin="external_agent" if memory_id == "agent" else "ask_answer",
        )
    assert startup_warmup._expire_memory_suggestions(expiry_repo(world)) == 0
    assert {memory_id: world.status(memory_id) for memory_id in states} == states
    assert world.env.rows("SELECT id FROM memory_revisions") == []
    assert world.events.items == []


def assert_page_limit_500_idempotent(world: World) -> None:
    world.seed()
    for index in range(501):
        world.memory(
            f"old-{index:04d}", title="old", status="candidate",
            signal="clarified", created_at=OLD,
        )
    repo = expiry_repo(world)
    assert repo.settings.auto_memory_suggest_expiry_sweep_limit == 500
    assert startup_warmup._expire_memory_suggestions(repo) == 500
    assert world.status("old-0500") == "candidate"
    assert startup_warmup._expire_memory_suggestions(repo) == 1
    assert startup_warmup._expire_memory_suggestions(repo) == 0
    revisions = world.env.rows(
        "SELECT COUNT(*) AS n FROM memory_revisions WHERE change_reason = ?", ("expired",)
    )
    assert revisions[0]["n"] == 501
    assert len(world.events.items) == 501


def assert_expiry_runs_when_deployment_switch_off(world: World) -> None:
    world.seed()
    world.suggest_rows(replace(suggestion("off-old"), created_at=OLD, updated_at=OLD))
    world.switch(wiring(enabled=False, clarified=False, repeated=False)).set_mode(MEMBER, "manual")
    world.reset()
    repo = expiry_repo(world, limit=1)
    assert not repo.settings.auto_memory_suggest_enabled
    assert startup_warmup._expire_memory_suggestions(repo) == 1
    assert world.status("off-old") == "rejected"
    assert world.events.items[0]["action"] == LIFECYCLE_SUGGESTION_EXPIRED
    assert world.events.items[0]["owner_id"] == MEMBER
    assert world.service.now() == NOW
