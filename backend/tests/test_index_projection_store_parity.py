"""The two ``IndexProjectionStore`` backends agree where no Protocol checks them.

``IndexProjectionStorePort`` does not declare every method the scale builder and
the artifact runtime call on the store: the M1 isolation readers (E4-6) and a few
older row readers are reached by duck typing. Nothing else would notice if one
backend changed such a method's parameters, or the isolation version constant,
and the other did not -- the first sign would be a production error on the
backend the tests did not happen to run.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from app.repositories.postgres import index_projection_store as postgres_store
from app.repositories.sqlite import index_projection_store as sqlite_store

_SERVICES = Path(__file__).resolve().parents[1] / "app" / "services"
_CALLERS = ("scale_index_builder.py", "scale_artifact_runtime.py")


def _called_store_methods() -> tuple:
    """Every ``self.projections.<name>`` the builder and the runtime use that is
    a method of either store class -- derived from the callers, so a new
    duck-typed call is covered the moment it is written. Instance callbacks
    (``connect``, ``in_batches``...) are not class members and are skipped."""
    names = set()
    for caller in _CALLERS:
        names.update(re.findall(
            r"self\.projections\.([A-Za-z_]\w*)", (_SERVICES / caller).read_text()
        ))
    return tuple(sorted(
        name for name in names
        if inspect.getattr_static(postgres_store.IndexProjectionStore, name, None)
        is not None
        or inspect.getattr_static(sqlite_store.IndexProjectionStore, name, None)
        is not None
    ))


UNDECLARED_METHODS = _called_store_methods()


def test_the_derived_method_list_covers_the_isolation_readers():
    for name in ("memory_source_ids", "memory_derived_ids",
                 "memory_cluster_canonicals", "predates_memory_isolation",
                 "built_before_memory_isolation", "memory_isolation_stamp",
                 "memory_sources_changed", "is_mounted_by_anyone", "notebook_name",
                 "notebook_owner", "notebook_tier", "active_object_graph_rows"):
        assert name in UNDECLARED_METHODS, name


def test_both_stores_stamp_the_same_memory_isolation_marker():
    """An artifact built on one backend and read on the other (the offline CLI
    exports and imports packages across hosts) must be judged the same way."""
    assert (
        postgres_store.MEMORY_ISOLATION_ARTIFACT_MARKER
        == sqlite_store.MEMORY_ISOLATION_ARTIFACT_MARKER
    )
    assert (
        postgres_store.MEMORY_ISOLATION_ARTIFACT_VERSION
        == sqlite_store.MEMORY_ISOLATION_ARTIFACT_VERSION
    )


def _parameters(signature: inspect.Signature) -> list[tuple]:
    # The return annotation is left out on purpose: the PostgreSQL row readers
    # are keyset generators where SQLite returns a list, and callers iterate both.
    return [
        (p.name, p.kind, p.default, p.annotation)
        for p in signature.parameters.values()
    ]


@pytest.mark.parametrize("name", UNDECLARED_METHODS)
def test_an_undeclared_store_method_has_one_signature_on_both_backends(name):
    postgres = inspect.getattr_static(postgres_store.IndexProjectionStore, name)
    sqlite = inspect.getattr_static(sqlite_store.IndexProjectionStore, name)
    # static on one and bound on the other would shift every argument by one
    assert type(postgres) is type(sqlite), (name, type(postgres), type(sqlite))
    assert _parameters(
        inspect.signature(getattr(postgres_store.IndexProjectionStore, name))
    ) == _parameters(
        inspect.signature(getattr(sqlite_store.IndexProjectionStore, name))
    ), name


def test_both_stores_stamp_and_judge_the_isolation_the_same_way():
    """The manifest fields one backend writes are read the same way by the
    other: the stamp, the "Memory set changed" test the fold uses, and the
    "built before the isolation" test every reader uses."""
    stores = (postgres_store.IndexProjectionStore, sqlite_store.IndexProjectionStore)
    stamps = [store.memory_isolation_stamp(["src-b", "src-a"]) for store in stores]
    assert stamps[0] == stamps[1]
    assert stamps[0]["memory_isolation"] == 1 and len(stamps[0]["memory_sources_digest"]) == 32
    assert stores[0].memory_isolation_stamp([])["memory_sources_digest"] == ""
    with_memory = ["nb", 3, "memory_isolation", 1]
    for store in stores:
        assert store.memory_sources_changed(stamps[0], ["src-a", "src-b"]) is False
        assert store.memory_sources_changed(stamps[0], ["src-a"]) is True
        # a manifest from before the isolation counts as stamped over no Memory
        assert store.memory_sources_changed({}, []) is False
        assert store.memory_sources_changed({}, ["src-a"]) is True
        assert store.predates_memory_isolation(stamps[0], with_memory) is False
        assert store.predates_memory_isolation({"version": ["nb", 3]}, with_memory) is True
        # whatever the notebook holds now: a Memory deleted before the upgrade
        # leaves no trace, so the artifact's own field alone decides
        assert store.predates_memory_isolation({"version": ["nb", 3]}, ["nb", 3]) is True
        assert store.predates_memory_isolation({"memory_isolation": 0}, ["nb", 3]) is True
