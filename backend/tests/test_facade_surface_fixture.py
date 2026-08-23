"""`facade_surface.json` fixture parity — is anyone actually watching it?

`backend/tests/architecture/facade_contract.py::FIXTURE` points at this
frozen fixture but, as of this writing, has zero readers anywhere in the
repo (grep-verified: nothing else imports ``FIXTURE``). Nothing compares
the JSON snapshot on disk against what ``collect_facade_surface()`` would
produce today, so retiring a facade method (or adding/removing a
compatibility export) can silently drift the frozen snapshot out of sync
with the live surface without any test ever noticing.

This test does not replace ``--rebaseline-surface`` as the source of truth
for per-name detail (``owner``/``consumers``/``patch_targets``, which are
deliberately pinned to ``SURFACE_SOURCE_COMMIT`` rather than the live tree —
see ``scripts/generate_repository_contract_fixtures.py``'s
``collect_facade_surface()`` and its ``--rebaseline-surface`` branch). It
only asserts the *name set* stays in sync. That is safe to check against the
live default (``OWNER_BY_MEMBER``) call: ``collect_facade_surface()`` adds a
name to the returned surface based purely on class/module/instance-attribute
membership — the ``owner_by_member`` mapping only fills in the per-name
``owner`` field, it never gates whether a name is present at all. So the
owner column can legitimately drift as ``OWNER_BY_MEMBER`` evolves between
here and ``SURFACE_SOURCE_COMMIT`` without this test caring; the *set* of
names cannot drift silently.
"""
from __future__ import annotations

import json

import pytest

from tests.architecture.facade_contract import FIXTURE
from tests.architecture.repository_contract import live_surface


@pytest.mark.architecture_contract
def test_facade_surface_fixture_name_set_matches_the_live_surface():
    frozen_names = set(json.loads(FIXTURE.read_text(encoding="utf-8")))
    live_names = set(live_surface())
    assert live_names == frozen_names, (
        "facade_surface.json has drifted from the live facade/compatibility "
        f"surface (added: {sorted(live_names - frozen_names)}, removed: "
        f"{sorted(frozen_names - live_names)}). Run "
        "`PYTHONPATH=backend python3 "
        "scripts/generate_repository_contract_fixtures.py "
        "--rebaseline-surface` to refresh it."
    )
