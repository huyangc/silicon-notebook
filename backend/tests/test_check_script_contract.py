"""The local aggregate gate must not silently drop a committed test root."""
from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_check_script_runs_every_committed_test_root():
    check = (ROOT / "scripts" / "check.sh").read_text(encoding="utf-8")
    lanes = "\n".join(
        (ROOT / "scripts" / f"check_{name}.sh").read_text(encoding="utf-8")
        for name in ("backend", "contracts", "frontend")
    )
    assert 'check_${lane}.sh' in check
    assert '"$ROOT_DIR/backend/tests"' in lanes
    assert '"$ROOT_DIR/fangan/testcases/harness/tests"' in lanes
    assert "npm run test" in lanes

    package = json.loads(
        (ROOT / "frontend" / "package.json").read_text(encoding="utf-8")
    )
    frontend_test = package["scripts"]["test"]
    assert "test:node" in frontend_test
    assert "test:component" in frontend_test
