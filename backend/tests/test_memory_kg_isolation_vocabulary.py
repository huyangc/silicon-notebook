"""Every user-facing constant of `app.domain.memory_kg_isolation` passes the UI
vocabulary guard.

Those constants reach the screen through DYNAMIC `user_error(409, exc.user_message)`
sites (registered in `test_user_error.py`), which the static scan of
`scripts/check_ui_vocabulary.py` cannot read. This test closes that gap: it runs
the guard's own `terms_in` over every string constant of the module, so a
blacklisted word (「晋升」, 「基准库」, ...) put into one of them fails here even
though `check_ui_vocabulary.py` itself stays green.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

from app.domain import memory_kg_isolation

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _ROOT / "scripts" / "check_ui_vocabulary.py"
_spec = importlib.util.spec_from_file_location("check_ui_vocabulary", _SCRIPT_PATH)
guard = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules.setdefault("check_ui_vocabulary", guard)
_spec.loader.exec_module(guard)

# Machine codes written to `promotion_candidates.reason`: never rendered.
_MACHINE_CODES = {"MEMORY_PROMOTION_REJECTED_REASON", "PROMOTION_OBJECT_MISSING_REASON"}


def _copy_constants() -> dict[str, str]:
    return {
        name: value
        for name, value in vars(memory_kg_isolation).items()
        if name.isupper() and isinstance(value, str) and name not in _MACHINE_CODES
    }


def test_the_module_still_exposes_its_user_copy():
    # Guards the guard: an empty scan would pass vacuously.
    assert set(_copy_constants()) >= {
        "CROSS_CLASS_MESSAGE",
        "SAME_OWNER_MESSAGE",
        "PROMOTION_PROPOSE_MESSAGE",
        "PROMOTION_APPROVE_MESSAGE",
        "PROMOTION_OBJECT_MISSING_MESSAGE",
        "PUBLISH_HOLDS_MEMORY_MESSAGE",
    }


def test_every_user_facing_constant_passes_the_vocabulary_guard():
    hits = {
        name: guard.terms_in(value)
        for name, value in _copy_constants().items()
        if guard.terms_in(value)
    }
    assert hits == {}


def test_machine_codes_are_ascii_and_never_copy():
    for name in _MACHINE_CODES:
        value = getattr(memory_kg_isolation, name)
        assert value.isascii() and value == value.strip() and " " not in value


def test_the_guard_would_catch_the_old_wording():
    # The copy this round replaced carried 「晋升」: the check is not vacuous.
    assert guard.terms_in("个人记忆派生的知识对象不能从这里提交晋升") == ["晋升"]
