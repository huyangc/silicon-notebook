from __future__ import annotations

import importlib.util
from functools import lru_cache
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[3]
GENERATOR = ROOT / "scripts" / "generate_repository_contract_fixtures.py"


@lru_cache(maxsize=1)
def fixture_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "repository_contract_fixture_generator",
        GENERATOR,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load repository fixture generator: {GENERATOR}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@lru_cache(maxsize=1)
def live_surface() -> dict[str, dict[str, object]]:
    surface = fixture_generator().collect_facade_surface()
    assert isinstance(surface, dict)
    return surface


@lru_cache(maxsize=1)
def live_surface_names() -> set[str]:
    return fixture_generator().collect_facade_surface_names()
