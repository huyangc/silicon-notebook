#!/usr/bin/env python3
"""Cross-stack contract: the frontend's user-facing ask-mode ids
(frontend/app/ask-modes.ts) must exactly equal the backend registry's
user_facing ids (backend/app/services/ask_modes.py), and each mode's
frontend ``streamsTrace`` must equal the backend's ``streaming``. Adding or
renaming a mode on one side without the other — or claiming a live trace for an
engine that never streams one — fails here. Run by scripts/check.sh."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
from app.services.ask_modes import ASK_MODES as BACKEND_ASK_MODES  # noqa: E402
from app.services.ask_modes import user_facing_mode_ids  # noqa: E402


def _mode_entries() -> list[str]:
    text = (ROOT / "frontend/app/ask-modes.ts").read_text(encoding="utf-8")
    m = re.search(r"export const ASK_MODES[^\[]*\[(.*?)\];", text, re.S)
    if not m:
        raise SystemExit("ask-modes.ts: ASK_MODES array not found")
    # One entry per `{ ... }` object literal; the array holds no nested braces.
    return re.findall(r"\{([^{}]*)\}", m.group(1))


def frontend_ids() -> list[str]:
    ids: list[str] = []
    for entry in _mode_entries():
        found = re.search(r'id:\s*"([A-Za-z0-9_]+)"', entry)
        if found:
            ids.append(found.group(1))
    return ids


def frontend_streams_trace() -> dict[str, bool]:
    """`{id: streamsTrace}` — a mode missing the flag is a contract error, not
    a silent default: the UI would then guess whether to show a live trace."""
    flags: dict[str, bool] = {}
    for entry in _mode_entries():
        found = re.search(r'id:\s*"([A-Za-z0-9_]+)"', entry)
        if not found:
            continue
        streams = re.search(r"streamsTrace:\s*(true|false)", entry)
        if not streams:
            raise SystemExit(
                f"ask-modes.ts: mode {found.group(1)!r} has no streamsTrace flag")
        flags[found.group(1)] = streams.group(1) == "true"
    return flags


_PLUGIN_MODE_LITERAL = re.compile(
    r"[\"']([a-z][a-z0-9_-]*\.[a-z0-9._-]+)[\"']"
)
_MODE_LITERAL_CONTEXTS = (
    re.compile(
        r"\b(?:mode|pendingMode|selectedMode)\b\s*(?::|=)\s*"
        r"[\"']([a-z][a-z0-9_-]*\.[a-z0-9._-]+)[\"']"
    ),
    re.compile(
        r"\b(?:setMode|selectMode|executeAsk|modeLabel|groupOf|requiresKg|"
        r"streamsTrace|canUseMode)\s*\(\s*"
        r"[\"']([a-z][a-z0-9_-]*\.[a-z0-9._-]+)[\"']"
    ),
)


def hard_coded_plugin_modes() -> list[str]:
    """Return production call sites that compile a deployment mode id.

    Dotted mode ids are exclusively runtime data.  The context-aware scan
    avoids treating unrelated dotted window/storage keys as Ask modes while
    still covering every owner/selector call that can publish one.
    """

    offenders: list[str] = []
    app_root = ROOT / "frontend/app"
    for path in sorted((*app_root.rglob("*.ts"), *app_root.rglob("*.tsx"))):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            for pattern in _MODE_LITERAL_CONTEXTS:
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
                    break
        if path.name == "ask-modes.ts":
            for match in _PLUGIN_MODE_LITERAL.finditer(text):
                lineno = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
    return sorted(set(offenders))


def main() -> int:
    backend = set(user_facing_mode_ids())
    frontend = set(frontend_ids())
    if backend != frontend:
        print("ask-mode contract MISMATCH", file=sys.stderr)
        print(f"  backend user_facing : {sorted(backend)}", file=sys.stderr)
        print(f"  frontend ASK_MODES  : {sorted(frontend)}", file=sys.stderr)
        print(f"  only backend: {sorted(backend - frontend)} | "
              f"only frontend: {sorted(frontend - backend)}", file=sys.stderr)
        return 1
    frontend_streams = frontend_streams_trace()
    drift = {
        mode_id: (BACKEND_ASK_MODES[mode_id].streaming, frontend_streams[mode_id])
        for mode_id in sorted(backend)
        if BACKEND_ASK_MODES[mode_id].streaming != frontend_streams[mode_id]
    }
    if drift:
        print("ask-mode streaming contract MISMATCH", file=sys.stderr)
        for mode_id, (backend_flag, frontend_flag) in drift.items():
            print(f"  {mode_id}: backend streaming={backend_flag} | "
                  f"frontend streamsTrace={frontend_flag}", file=sys.stderr)
        return 1
    hard_coded = hard_coded_plugin_modes()
    if hard_coded:
        print("ask-mode plugin literal contract MISMATCH", file=sys.stderr)
        for location in hard_coded:
            print(
                f"  {location}: deployment mode ids must come from /ask-modes",
                file=sys.stderr,
            )
        return 1
    print(f"ask-mode contract OK: {sorted(backend)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
