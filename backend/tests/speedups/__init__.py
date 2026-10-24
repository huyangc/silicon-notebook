"""Test-process-only speedups, installed from ``conftest.pytest_configure``.

A speedup here may change how much work a test process repeats, never what a
test observes: each one keeps a result-equivalence pin test of its own and an
off switch for side-by-side comparison. Nothing in this package is imported by
production code, and production code is never edited to make one work.

``register`` runs once per pytest process (controller and every xdist worker)
before collection, which is before any test module imports ``app.main``.
"""
from __future__ import annotations

from importlib import import_module

# Dotted module names under ``tests.speedups``; each exposes ``install()``.
_SPEEDUPS: tuple[str, ...] = ("app_routes", "settings_sources")


def register(config) -> None:  # noqa: ARG001 - pytest hook signature
    for name in _SPEEDUPS:
        import_module(f"{__name__}.{name}").install()
