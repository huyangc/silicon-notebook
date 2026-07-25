from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.migration.shadow.cli import _emit, _safe_error, build_parser


def test_cli_has_only_explicit_run_scoped_commands():
    parser = build_parser()
    for command in ("status", "preflight", "start-forward", "verify", "worker"):
        namespace = parser.parse_args([command, "--run-id", "release-2026"] + (
            ["--confirmation-token", "x"] if command == "start-forward" else
            ["--level", "full", "--confirmation-token", "x"] if command == "verify" else
            ["--direction", "forward", "--confirmation-token", "x"] if command == "worker" else []
        ))
        assert namespace.run_id == "release-2026"
    with pytest.raises(SystemExit):
        parser.parse_args(["worker", "--run-id", "release-2026", "--direction", "forward"])


def test_cli_exposes_no_force_checkpoint_or_reverse_direction():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["worker", "--run-id", "r1", "--direction", "reverse", "--confirmation-token", "x"]
        )
    with pytest.raises(SystemExit):
        parser.parse_args(["status", "--run-id", "r1", "--force-checkpoint", "99"])


def test_human_output_redacts_confirmation_token(capsys):
    _emit(
        {
            "command": "preflight",
            "status": "confirmed",
            "confirmation_token": "private-token-value",
        },
        as_json=False,
    )
    output = capsys.readouterr().out
    assert "private-token-value" not in output
    assert "<redacted" in output


def test_json_output_is_machine_readable(capsys):
    _emit({"command": "status", "status": "ok", "checkpoint": 17}, as_json=True)
    assert json.loads(capsys.readouterr().out)["checkpoint"] == 17


def test_errors_redact_urls_and_secret_assignments():
    error = ValueError(
        "postgresql://operator:password@db.example/private password=hunter2 token=abc"
    )
    rendered = _safe_error(error)
    assert "hunter2" not in rendered
    assert "token=abc" not in rendered
    assert "db.example/private" not in rendered


def test_local_supervisor_validates_pid_identity_and_never_kills_by_pattern():
    script = (Path(__file__).parents[3] / "scripts" / "shadow.sh").read_text()
    assert 'ps -p "$pid" -o command=' in script
    assert 'ps -p "$pid" -o lstart=' in script
    assert 'kill -TERM "$pid"' in script
    assert "pkill" not in script
    assert "killall" not in script
