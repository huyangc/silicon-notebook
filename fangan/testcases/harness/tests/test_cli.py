import json
import os
import subprocess
import sys

REPO = "/Users/hzf/workspace/silicon_notebook"
TC = os.path.join(REPO, "fangan/testcases")
GOLD_DIR = os.path.join(TC, "engram/ch00_abstract")


def test_cli_gold_vs_itself(tmp_path):
    out_json = tmp_path / "r.json"
    out_md = tmp_path / "r.md"
    cmd = [sys.executable, "-m", "harness.score",
           "--gold", GOLD_DIR,
           "--pred", os.path.join(GOLD_DIR, "gold.yaml"),
           "--out", str(out_json), "--md", str(out_md)]
    proc = subprocess.run(cmd, cwd=TC, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(out_json.read_text())
    assert data["weighted_score"] == 100.0
    assert out_md.read_text().strip() != ""
    assert "100.0" in proc.stdout  # headline score echoed to stdout
