"""Task 26 — facade monkeypatch targets have explicit owners.

Every Task-1 monkeypatch consumer now patches its canonical component seam.
The only facade/module members the CURRENT suite may still patch are the
manifest-marked, intentionally late-bound production compatibility seams:
the ``RepositoryCompatibilitySeams`` module seams (``_now`` / ``_new_id`` /
``_COPY_CHUNK`` / ``_remap_json_ids``) and the facade seats the runtime
wiring resolves per call (``_write`` / ``_connect`` / ...).  Test-only
facade-private patch targets are forbidden — those probes belong on the
canonical store/service/runtime component.
"""
from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FACADE_SOURCE = ROOT / "backend" / "app" / "services" / "sqlite_repository.py"


def test_every_private_facade_patch_targets_a_manifest_marked_seam():
    from app.repositories.ownership_manifest import LATE_BOUND_COMPATIBILITY_SEAMS
    from tests.test_repository_surface_manifest import _static_repository_patches

    offenders = sorted(
        f"{file}:{line} -> {target} (base={base})"
        for file, line, target, base in _static_repository_patches()
        if target.startswith("_") and target not in LATE_BOUND_COMPATIBILITY_SEAMS
    )
    assert offenders == [], (
        "test-only facade-private patch targets are forbidden — patch the "
        "canonical component seam (or register a genuine late-bound "
        f"production seam in the ownership manifest): {offenders}"
    )


def _facade_init() -> ast.FunctionDef:
    tree = ast.parse(FACADE_SOURCE.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "SQLiteRepository":
            for member in node.body:
                if isinstance(member, ast.FunctionDef) and member.name == "__init__":
                    return member
    raise AssertionError("SQLiteRepository.__init__ not found")


def test_manifest_seams_are_late_bound_in_the_facade_wiring():
    """Each manifest seam must be provably late-bound: the facade constructor
    passes it to the runtime inside a lambda (resolved per call), never as an
    eagerly evaluated value — so post-construction monkeypatches stay
    authoritative for production flows."""
    from app.repositories.ownership_manifest import LATE_BOUND_COMPATIBILITY_SEAMS

    init = _facade_init()
    lambda_attribute_names: set[str] = set()
    lambda_names: set[str] = set()
    for node in ast.walk(init):
        if not isinstance(node, ast.Lambda):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Attribute):
                lambda_attribute_names.add(inner.attr)
            elif isinstance(inner, ast.Name):
                lambda_names.add(inner.id)

    for name, seam in LATE_BOUND_COMPATIBILITY_SEAMS.items():
        assert seam.name == name
        assert seam.scope in {"module", "facade"}, name
        if seam.scope == "module":
            assert seam.resolved_through in lambda_names, (
                f"module seam {name} is not resolved late by the runtime seams"
            )
        else:
            assert seam.resolved_through in lambda_attribute_names, (
                f"facade seam {name} is not resolved late by the runtime wiring"
            )


def test_module_seams_mirror_the_runtime_compatibility_dataclass():
    from app.repositories.ownership_manifest import LATE_BOUND_COMPATIBILITY_SEAMS
    from app.services.repository_runtime import RepositoryCompatibilitySeams

    module_seams = {
        name
        for name, seam in LATE_BOUND_COMPATIBILITY_SEAMS.items()
        if seam.scope == "module"
    }
    assert module_seams == {"_now", "_new_id", "_COPY_CHUNK", "_remap_json_ids"}
    assert {field.name for field in dataclasses.fields(RepositoryCompatibilitySeams)} == {
        "new_id",
        "now",
        "copy_chunk_size",
        "remap_json_ids",
    }
