"""Transaction-scoped serialization for logical cluster artifacts."""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any


_CLUSTER_LOCK_NAMESPACE = "silicon-notebook:cluster-artifact"
_CONCEPT_CLUSTERS_ARTIFACT = "concept_clusters"


def lock_cluster_artifact_types(
    connection: Any,
    notebook_id: str,
    object_types: Iterable[str],
) -> None:
    """Lock cluster slices in one process-independent, deadlock-safe order.

    Every writer of ``concept_clusters`` uses the same namespaced transaction
    advisory key.  Sorting the complete key bytes before acquisition preserves
    a fixed order if a future writer replaces several object types in one
    transaction.  Hash collisions only serialize unrelated slices; the formal
    unique index remains the final membership-integrity guard.
    """
    keys = {
        f"{_CLUSTER_LOCK_NAMESPACE}:{notebook_id}:"
        f"{_CONCEPT_CLUSTERS_ARTIFACT}:{object_type}"
        for object_type in object_types
    }
    for key in sorted(keys, key=lambda value: value.encode("utf-8")):
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (key,),
        )


def lock_cluster_artifact_type(
    connection: Any,
    notebook_id: str,
    object_type: str,
) -> None:
    lock_cluster_artifact_types(connection, notebook_id, (object_type,))
