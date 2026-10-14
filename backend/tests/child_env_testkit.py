"""Bytecode policy for child processes a test starts with its own environment.

A test that hand-builds a child's environment (instead of extending
``os.environ``) drops the gate's ``PYTHONPYCACHEPREFIX``. Any Python the child
runs would then write ``.pyc`` files next to the repository sources. Spread
``BYTECODE_ENV`` into such an environment: it carries the prefix through when
the test process has one and turns bytecode writing off otherwise. Neither
variable changes what the child does or prints.
``test_test_architecture_policy.py`` requires it of every hand-built
environment under ``backend/tests``.
"""
from __future__ import annotations

import os

BYTECODE_ENV: dict[str, str] = (
    {"PYTHONPYCACHEPREFIX": os.environ["PYTHONPYCACHEPREFIX"]}
    if os.environ.get("PYTHONPYCACHEPREFIX")
    else {"PYTHONDONTWRITEBYTECODE": "1"}
)
