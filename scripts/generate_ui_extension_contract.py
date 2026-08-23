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

`contract_rows_match(committed, live) -> bool` and `contract_diff(committed,
live) -> list[str]` are the public comparator this script's own `--check`
uses — there is exactly one implementation of "do two fixture dicts describe
the same contribution set", and `scripts/check_deployment_extension_parity.py`
imports these two functions rather than re-spelling the comparison against a
deployment-plugin registry.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.extensions import default_extension_runtime  # noqa: E402
from app.extensions.ui_projection import (  # noqa: E402
    CONTRIBUTION_SORT_FIELDS,
    ui_contribution_contract,
)


FIXTURE_PATH = ROOT / "backend" / "tests" / "fixtures" / "ui_extension_contract.json"

# Mirrors the wire-schema literal `SystemExtensionsResponse.api_version`
# (backend/app/models/system.py) and the value the frontend parity guard
# asserts (frontend/tests/guards/extension-ui-parity.test.mjs).
API_VERSION = "1"


def build_fixture() -> dict[str, object]:
    registry = default_extension_runtime().registry
    # `ui_contribution_contract` already returns rows sorted by
    # `CONTRIBUTION_SORT_FIELDS` (see its docstring in ui_projection.py), so
    # the file on disk is deterministic and reproducible from an empty
    # topology onward without this script re-sorting. The `sorted(...)` call
    # here is kept anyway — it is a no-op on already-sorted input — purely
    # as defense in depth: both documented consumers already compare the row
    # *set*, not list order (see `contract_rows_match` below), so re-sorting
    # cannot itself turn a match into a mismatch or vice versa.
    contributions = sorted(
        ui_contribution_contract(registry), key=_contribution_sort_key
    )
    return {
        "api_version": API_VERSION,
        "contributions": contributions,
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

    try:
        on_disk_json = json.loads(on_disk)
    except json.JSONDecodeError as exc:
        print(
            "UI extension contract fixture is not valid JSON: "
            f"{FIXTURE_PATH.relative_to(ROOT)}\n"
            f"  parse error: {exc}\n"
            "  run `python3 scripts/generate_ui_extension_contract.py` to rewrite it.",
            file=sys.stderr,
        )
        return 1

    if contract_rows_match(on_disk_json, fixture):
        # Same contributions, same api_version — only whitespace/indentation/key
        # order/list order differ (both consumers compare the row *set*, per the
        # module docstring, so a reordering alone is not a contract break). Still
        # exit non-zero: the contracts lane must stay strict about byte-identical
        # fixtures so `git diff` on the fixture stays meaningful.
        print(
            "UI extension contract fixture is present but not byte-identical to "
            "the canonical rendering — content is identical, only formatting "
            "differs (仅格式差异，跑一次生成器即可):\n"
            "  python3 scripts/generate_ui_extension_contract.py",
            file=sys.stderr,
        )
        return 1

    print(
        "UI extension contract fixture is STALE — the live registry no longer "
        f"matches {FIXTURE_PATH.relative_to(ROOT)}.\n"
        "  run `python3 scripts/generate_ui_extension_contract.py` to refresh it, "
        "then re-run the frontend parity guard.",
        file=sys.stderr,
    )
    for line in contract_diff(on_disk_json, fixture):
        print(f"  {line}", file=sys.stderr)
    return 1


_CONTRIBUTION_KEY_FIELDS = ("plugin_id", "version", "contribution_id")
# Reuse the same field tuple `ui_contribution_contract` sorted by, so this
# script's own (now-redundant, see `build_fixture`) re-sort and its
# comparators in `contract_rows_match`/`contract_diff` can never silently
# drift from the one place that owns the sort order.
_CONTRIBUTION_SORT_FIELDS = CONTRIBUTION_SORT_FIELDS


def _contribution_sort_key(row: object) -> tuple[str, ...]:
    if not isinstance(row, dict):
        return (repr(row),)
    return tuple(str(row.get(field, "")) for field in _CONTRIBUTION_SORT_FIELDS)


def _normalized(fixture_obj: object) -> object:
    """Canonicalize a parsed fixture for content-equality comparison.

    Both documented consumers (the backend pytest parity test and the
    frontend `extension-ui-parity` guard) compare the *set* of contribution
    rows, not their list order — see the module docstring. So a fixture that
    differs from the live registry only in row order is not stale; sort rows
    into a stable order before comparing so that reordering does not get
    reported as drift.
    """
    if not isinstance(fixture_obj, dict):
        return fixture_obj
    contributions = fixture_obj.get("contributions")
    if not isinstance(contributions, list):
        return fixture_obj
    normalized = dict(fixture_obj)
    normalized["contributions"] = sorted(contributions, key=_contribution_sort_key)
    return normalized


def _contribution_key(row: object) -> tuple[str, ...] | None:
    if not isinstance(row, dict):
        return None
    return tuple(str(row.get(field, "")) for field in _CONTRIBUTION_KEY_FIELDS)


def contract_rows_match(committed: object, live: object) -> bool:
    """True when two parsed fixture dicts describe the identical contribution set.

    This is the one and only comparator both documented consumers rely on —
    `main()`'s own `--check` and `scripts/check_deployment_extension_parity.py`
    (which compares a `--frontend-contract` file against a *deployment*-plugin
    registry rather than this script's own `default_extension_runtime()`).
    Row order never matters (see `_normalized`'s docstring), so callers must
    not re-implement this as a bare `==` on unsorted `contributions` lists.
    """
    return _normalized(committed) == _normalized(live)


def contract_diff(committed: object, live: object) -> list[str]:
    """Render a per-key diff between the committed and live fixture dicts.

    Top-level scalar keys (e.g. `api_version`) are compared directly.
    `contributions` is compared as a keyed collection (by plugin_id/version/
    contribution_id) rather than as an ordered list, so an added, removed, or
    field-changed row is reported individually instead of dumping both full
    fixtures as opaque reprs. Paired with `contract_rows_match` above — call
    this only after that returns False, to explain *why*.
    """
    lines: list[str] = []
    if not isinstance(committed, dict) or not isinstance(live, dict):
        lines.append(f"committed={committed!r}")
        lines.append(f"live     ={live!r}")
        return lines

    keys = sorted(set(committed) | set(live), key=str)
    for key in keys:
        committed_value = committed.get(key, "<missing>")
        live_value = live.get(key, "<missing>")
        if committed_value == live_value:
            continue
        if key == "contributions" and isinstance(committed_value, list) and isinstance(live_value, list):
            lines.extend(_diff_contributions(committed_value, live_value))
        else:
            lines.append(f"{key}: committed={committed_value!r} live={live_value!r}")
    return lines


def _diff_contributions(committed_rows: list[object], live_rows: list[object]) -> list[str]:
    lines: list[str] = []
    committed_by_key = {_contribution_key(row): row for row in committed_rows}
    live_by_key = {_contribution_key(row): row for row in live_rows}

    def sort_keys(keys: set[object]) -> list[object]:
        return sorted(keys, key=lambda key: tuple(str(part) for part in key) if key else ("",))

    for key in sort_keys(set(committed_by_key) - set(live_by_key)):
        lines.append(f"contributions: removed committed row {committed_by_key[key]!r}")
    for key in sort_keys(set(live_by_key) - set(committed_by_key)):
        lines.append(f"contributions: added live row {live_by_key[key]!r}")
    for key in sort_keys(set(committed_by_key) & set(live_by_key)):
        committed_row = committed_by_key[key]
        live_row = live_by_key[key]
        if committed_row == live_row:
            continue
        field_keys = sorted(set(committed_row) | set(live_row), key=str)
        for field in field_keys:
            committed_field = committed_row.get(field, "<missing>")
            live_field = live_row.get(field, "<missing>")
            if committed_field != live_field:
                lines.append(
                    f"contributions[{key}].{field}: committed={committed_field!r} live={live_field!r}"
                )
    return lines


if __name__ == "__main__":
    raise SystemExit(main())
