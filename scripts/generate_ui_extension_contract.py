#!/usr/bin/env python3
"""Regenerate (or, with --check, verify) the cross-stack UI extension contract
fixture `backend/tests/fixtures/ui_extension_contract.json` from the live
backend registry.

That fixture is the parity anchor between two independent registries:
`backend/tests/test_extension_ui_projection.py::
test_default_backend_ui_topology_matches_cross_stack_contract` compares it
against the backend's `default_extension_runtime()` registry, and
`frontend/tests/guards/extension-ui-parity.test.mjs` compares it against the
frontend's build-time `WORKSPACE_UI_CONTRIBUTIONS`. Both consumers must see
the identical set of `{plugin_id, version, contribution_id, slot,
capability}` rows a shipped contribution declares — this script derives that
set from the single shared projection `app.extensions.ui_projection.
ui_contribution_contract` (the same function the backend test imports), so
there is exactly one place that knows the wire shape.

Usage:
    python3 scripts/generate_ui_extension_contract.py            # write the fixture
    python3 scripts/generate_ui_extension_contract.py --check    # verify only, no write

--check exits non-zero and prints a diff when the committed fixture is stale
(e.g. a new `ui_contributions` entry was added to a manifest but the fixture
was not refreshed). It is wired into scripts/check_contracts.sh so a stale
fixture fails the contracts lane instead of only being caught by the slower
pytest run.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.extensions import default_extension_runtime  # noqa: E402
from app.extensions.ui_projection import ui_contribution_contract  # noqa: E402


FIXTURE_PATH = ROOT / "backend" / "tests" / "fixtures" / "ui_extension_contract.json"

# Mirrors the wire-schema literal `SystemExtensionsResponse.api_version`
# (backend/app/models/system.py) and the value the frontend parity guard
# asserts (frontend/tests/guards/extension-ui-parity.test.mjs).
API_VERSION = "1"


def build_fixture() -> dict[str, object]:
    registry = default_extension_runtime().registry
    return {
        "api_version": API_VERSION,
        "contributions": ui_contribution_contract(registry),
    }


def render(fixture: dict[str, object]) -> str:
    return json.dumps(fixture, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the committed fixture matches the live registry; never write",
    )
    args = parser.parse_args(argv)

    fixture = build_fixture()
    rendered = render(fixture)

    if not args.check:
        FIXTURE_PATH.write_text(rendered, encoding="utf-8")
        print(f"wrote {FIXTURE_PATH.relative_to(ROOT)}")
        return 0

    if not FIXTURE_PATH.exists():
        print(
            f"UI extension contract fixture missing: {FIXTURE_PATH.relative_to(ROOT)}\n"
            "  run `python3 scripts/generate_ui_extension_contract.py` to create it",
            file=sys.stderr,
        )
        return 1

    on_disk = FIXTURE_PATH.read_text(encoding="utf-8")
    if on_disk == rendered:
        print(f"UI extension contract fixture OK ({len(fixture['contributions'])} contributions)")
        return 0

    print(
        "UI extension contract fixture is STALE — the live registry no longer "
        f"matches {FIXTURE_PATH.relative_to(ROOT)}.\n"
        "  run `python3 scripts/generate_ui_extension_contract.py` to refresh it, "
        "then re-run the frontend parity guard.",
        file=sys.stderr,
    )
    try:
        on_disk_json = json.loads(on_disk)
    except json.JSONDecodeError:
        on_disk_json = None
    print(f"  committed: {on_disk_json}", file=sys.stderr)
    print(f"  live     : {fixture}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
