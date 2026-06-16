#!/usr/bin/env python3
"""Cross-stack contract: the frontend's user-facing ask-mode ids
(frontend/app/ask-modes.ts) must exactly equal the backend registry's
user_facing ids (backend/app/services/ask_modes.py). Adding/renaming a mode on
one side without the other fails here. Run by scripts/check.sh."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
from app.services.ask_modes import user_facing_mode_ids  # noqa: E402


def frontend_ids() -> list[str]:
    text = (ROOT / "frontend/app/ask-modes.ts").read_text(encoding="utf-8")
    m = re.search(r"export const ASK_MODES[^\[]*\[(.*?)\];", text, re.S)
    if not m:
        raise SystemExit("ask-modes.ts: ASK_MODES array not found")
    return re.findall(r'id:\s*"([A-Za-z0-9_]+)"', m.group(1))


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
    print(f"ask-mode contract OK: {sorted(backend)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
