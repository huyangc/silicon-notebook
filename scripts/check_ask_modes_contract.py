#!/usr/bin/env python3
"""Cross-stack contract: the frontend's user-facing ask-mode ids
(frontend/app/ask-modes.ts) must exactly equal the backend registry's
user_facing ids (backend/app/services/ask_modes.py), each mode's frontend
``streamsTrace`` must equal the backend's ``streaming``, and its frontend
``requiresKg`` must equal the backend's ``requires_kg``. Adding or renaming a
mode on one side without the other — claiming a live trace for an engine that
never streams one, or gating submission on a graph the backend no longer
requires — fails here. Run by scripts/check.sh."""
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
    # 锚定在赋值号上,而不是第一个 `[`:声明是
    # `export const ASK_MODES: readonly AskModeDef[] = Object.freeze([...]);`,
    # 类型注解里的 `[` 会让旧锚点把捕获区错位到 ASK_MODE_GROUPS。
    m = re.search(
        r"export const ASK_MODES[^=]*=\s*(?:Object\.freeze\()?\s*\[(.*?)\]\s*\)?;",
        text,
        re.S,
    )
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


def _frontend_flags(field: str) -> dict[str, bool]:
    """`{id: <field>}` for a boolean field of the built-in table — a mode
    missing the flag is a contract error, not a silent default: the UI would
    then guess whether to show a live trace / whether to gate submission."""
    flags: dict[str, bool] = {}
    pattern = re.compile(rf"\b{re.escape(field)}:\s*(true|false)")
    for entry in _mode_entries():
        found = re.search(r'id:\s*"([A-Za-z0-9_]+)"', entry)
        if not found:
            continue
        flag = pattern.search(entry)
        if not flag:
            raise SystemExit(
                f"ask-modes.ts: mode {found.group(1)!r} has no {field} flag")
        flags[found.group(1)] = flag.group(1) == "true"
    return flags


def frontend_streams_trace() -> dict[str, bool]:
    return _frontend_flags("streamsTrace")


def frontend_requires_kg() -> dict[str, bool]:
    """`{id: requiresKg}`. The frontend submit gate blocks a mode outright when
    this is true and the notebook has no graph, so it must mirror the backend's
    ``requires_kg`` exactly — a hard precondition, never a quality hint."""
    return _frontend_flags("requiresKg")


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
    # 扫描面覆盖 app 与 features 两个生产目录(features 里住着 extension SDK 与
    # 同步进来的 ext-* 包,同样不许写死部署 mode id)。已知边界(如实登记,不是
    # 承诺):判据是上下文正则,`const M = "x.y"` 先存变量再用的形态覆盖不到——
    # 那类间接写死靠评审,不靠本闸。
    scan_roots = (ROOT / "frontend/app", ROOT / "frontend/features")
    for path in sorted(
        p
        for root in scan_roots
        for p in (*root.rglob("*.ts"), *root.rglob("*.tsx"))
    ):
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
    # (backend attribute, frontend field, frontend reader). Every boolean the
    # two tables both carry is reconciled here; a flag reconciled on one side
    # only is exactly how reasoning's requires_kg drifted apart before.
    for backend_attr, frontend_field, reader in (
        ("streaming", "streamsTrace", frontend_streams_trace),
        ("requires_kg", "requiresKg", frontend_requires_kg),
    ):
        frontend_flags = reader()
        drift = {
            mode_id: (
                getattr(BACKEND_ASK_MODES[mode_id], backend_attr),
                frontend_flags[mode_id],
            )
            for mode_id in sorted(backend)
            if getattr(BACKEND_ASK_MODES[mode_id], backend_attr)
            != frontend_flags[mode_id]
        }
        if drift:
            print(f"ask-mode {backend_attr} contract MISMATCH", file=sys.stderr)
            for mode_id, (backend_flag, frontend_flag) in drift.items():
                print(f"  {mode_id}: backend {backend_attr}={backend_flag} | "
                      f"frontend {frontend_field}={frontend_flag}", file=sys.stderr)
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
