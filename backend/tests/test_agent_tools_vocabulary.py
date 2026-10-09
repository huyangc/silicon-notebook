"""The user-facing copy in `app.domain.agent_tools` passes the UI vocabulary guard.

`OWNER_ONLY_TIERS_MESSAGE` reaches the Agent access page through DYNAMIC
`user_error(422, OWNER_ONLY_TIERS_MESSAGE)` sites (registered in
`test_user_error.py`), which the static scan of `scripts/check_ui_vocabulary.py`
cannot read; this runs the guard's own `terms_in` over it so a blacklisted
word still fails. It is also the single source of that sentence: the
exception and both token routes use the constant.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

from app.domain import agent_tools
from app.repositories.identity_errors import AgentOwnerOnlyTierError

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _ROOT / "scripts" / "check_ui_vocabulary.py"
_spec = importlib.util.spec_from_file_location("check_ui_vocabulary", _SCRIPT_PATH)
guard = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules.setdefault("check_ui_vocabulary", guard)
_spec.loader.exec_module(guard)


def test_the_owner_only_refusal_passes_the_vocabulary_guard():
    assert agent_tools.OWNER_ONLY_TIERS_MESSAGE.strip()
    assert guard.terms_in(agent_tools.OWNER_ONLY_TIERS_MESSAGE) == []


def test_the_exception_carries_the_one_constant():
    assert str(AgentOwnerOnlyTierError()) == agent_tools.OWNER_ONLY_TIERS_MESSAGE


def test_the_sentence_is_written_once_in_app_code():
    app_dir = _ROOT / "backend" / "app"
    holders = sorted(
        str(path.relative_to(app_dir))
        for path in app_dir.rglob("*.py")
        if agent_tools.OWNER_ONLY_TIERS_MESSAGE in path.read_text(encoding="utf-8")
    )
    assert holders == ["domain/agent_tools.py"]
