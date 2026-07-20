from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def testcases_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def gold_paths(testcases_root: Path) -> tuple[Path, ...]:
    return tuple(sorted(testcases_root.glob("*/ch*/gold.yaml")))
