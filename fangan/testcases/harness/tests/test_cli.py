import json
from pathlib import Path
import subprocess
import sys


def test_cli_gold_vs_itself(tmp_path: Path, testcases_root: Path) -> None:
    gold_dir = testcases_root / "engram" / "ch00_abstract"
    out_json = tmp_path / "r.json"
    out_md = tmp_path / "r.md"
    cmd = [
        sys.executable,
        "-m",
        "harness.score",
        "--gold",
        str(gold_dir),
        "--pred",
        str(gold_dir / "gold.yaml"),
        "--out",
        str(out_json),
        "--md",
        str(out_md),
    ]
    proc = subprocess.run(
        cmd,
        cwd=testcases_root,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert data["weighted_score"] == 100.0
    assert out_md.read_text(encoding="utf-8").strip() != ""
    assert "100.0" in proc.stdout
