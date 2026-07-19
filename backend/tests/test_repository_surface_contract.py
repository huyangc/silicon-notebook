from __future__ import annotations

import json
from pathlib import Path
import typing

from app.repositories.ownership_manifest import ConsumerSite, SURFACE_MEMBERS
from app.services.sqlite_repository import SQLiteRepository
from tests.architecture.repository_contract import fixture_generator, live_surface


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = (
    ROOT
    / "backend"
    / "tests"
    / "fixtures"
    / "repository_contract"
    / "facade_surface.json"
)

SITE_FIELDS = {"path", "scope", "kind", "target"}
PATCH_FIELDS = SITE_FIELDS | {"base", "compatibility"}
REQUIRED_GENERATOR_CALLABLES = {
    "collect_facade_surface",
    "generate_v9_fixture",
    "normalized_repository_snapshot",
    "generate_ask_goldens",
    "generate_api_contract",
    "main",
}


def _surface() -> dict[str, dict[str, object]]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_surface_sites_use_semantic_identity_without_source_positions():
    surface = _surface()

    assert surface
    for name, record in surface.items():
        assert record["consumers"], name
        for consumer in record["consumers"]:
            assert set(consumer) == SITE_FIELDS, (name, consumer)
            assert consumer["scope"], (name, consumer)
            assert consumer["kind"] in {
                "attribute",
                "compatibility",
                "import",
                "patch",
                "registry",
            }
            assert consumer["target"], (name, consumer)
        for patch in record["patch_targets"]:
            assert set(patch) == PATCH_FIELDS, (name, patch)
            assert patch["kind"] == "patch"
            assert patch["scope"], (name, patch)
            assert patch["target"] == name
            assert patch["compatibility"] in {
                "production-compatible",
                "test-only",
            }


def test_surface_snapshot_does_not_track_ordinary_test_consumers():
    for name, record in _surface().items():
        ordinary_test_consumers = [
            consumer
            for consumer in record["consumers"]
            if consumer["path"].startswith("backend/tests/")
            and consumer["kind"] != "patch"
        ]
        assert ordinary_test_consumers == [], (name, ordinary_test_consumers)


def test_ownership_manifest_matches_the_semantic_surface_snapshot():
    surface = _surface()
    members = {member.name: member for member in SURFACE_MEMBERS}

    assert set(members) == set(surface)
    for name, record in surface.items():
        member = members[name]
        assert member.owner == record["owner"]
        assert member.kind == record["kind"]
        assert member.consumers == tuple(
            ConsumerSite(**site) for site in record["consumers"]
        )
        assert member.patches == tuple(
            ConsumerSite(
                path=site["path"],
                scope=site["scope"],
                kind=site["kind"],
                target=site["target"],
            )
            for site in record["patch_targets"]
        )


def test_surface_snapshot_matches_the_live_semantic_collector():
    assert _surface() == live_surface()


def test_fixture_generator_exposes_all_supported_contract_entry_points():
    generator = fixture_generator()

    assert REQUIRED_GENERATOR_CALLABLES <= {
        name
        for name, value in vars(generator).items()
        if callable(value)
    }


def test_snapshot_generator_annotation_resolves_to_the_facade_type():
    hints = typing.get_type_hints(
        fixture_generator().normalized_repository_snapshot
    )

    assert hints == {
        "repo": SQLiteRepository,
        "notebook_id": str,
        "return": dict[str, object],
    }
