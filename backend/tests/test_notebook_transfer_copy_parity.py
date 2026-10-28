"""The web import's follow-up failure sentences are written twice: on the job
(``app.services.notebook_transfer.jobs._MESSAGES``, what the API and the CLI-facing
record say) and in the frontend's code table (``frontend/app/notebook-transfer-api.ts``
``FAILURE_TEXT``, what the panel shows -- it maps the stable code, it never shows the
backend's message). They must say the same thing, word for word."""
from __future__ import annotations

import re
from pathlib import Path

from app.services.notebook_transfer import jobs

FRONTEND_TABLE = Path(__file__).resolve().parents[2] / "frontend" / "app" / "notebook-transfer-api.ts"


def _frontend_text(code: str) -> str:
    source = FRONTEND_TABLE.read_text("utf-8")
    table = source.split("const FAILURE_TEXT: Record<string, string> = {", 1)[1].split("\n};", 1)[0]
    (text,) = re.findall(rf'^\s*{code}: "([^"]+)",$', table, re.MULTILINE)
    return text


def test_follow_up_failures_read_the_same_on_both_sides():
    for code in ("import_followup_failed", "import_followup_interrupted"):
        assert _frontend_text(code) == jobs._MESSAGES[code], code
