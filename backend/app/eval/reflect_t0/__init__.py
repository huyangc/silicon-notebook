"""Public-paper question catalog for the Legacy retrieval diagnostics rig.

Questions are data. Notebook IDs are supplied explicitly by the runner.
"""
from __future__ import annotations

import json
from pathlib import Path

QUESTIONS_PATH = Path(__file__).resolve().parent / "questions.json"


def load_questions() -> dict:
    """读题集。返回原始 dict,不做任何加工——加工是 rig 的事。"""
    return json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
