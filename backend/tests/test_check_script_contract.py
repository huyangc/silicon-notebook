"""The local aggregate gate must not silently drop a committed test root."""
from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_check_script_runs_every_committed_test_root():
    check = (ROOT / "scripts" / "check.sh").read_text(encoding="utf-8")
    assert '"$ROOT_DIR/backend/tests"' in check
    assert '"$ROOT_DIR/fangan/testcases/harness/tests"' in check
    assert "npm run test" in check

    package = json.loads(
        (ROOT / "frontend" / "package.json").read_text(encoding="utf-8")
    )
    assert package["scripts"]["test"] == (
        "node --test $(find app -name '*.test.mjs' -type f -print)"
    )
