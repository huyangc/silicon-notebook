"""The KG artifact file-format codec that ``ScaleArtifactStore`` persists through.

``app.repositories.filesystem.scale_artifact_store`` owns the on-disk layout,
the manifest probes and the staging/swap sequence, but the array encodings
themselves (scale index, viz index, source-partition companions) are written
in the same ``app.services.kg`` modules as the KG algorithms that use them.
The store must not import services, so its composer injects this codec instead
(``ScaleArtifactStore(settings, codec=KG_SCALE_ARTIFACT_CODEC)``); the store
only knows the structural ``ScaleArtifactCodec`` protocol it declares.

Every method resolves its target through the owning module's attribute AT CALL
TIME, exactly as the store did before the injection: a test that monkeypatches
``scale_index.load_scale_index`` (the disk-cache suites' spies),
``viz_index.save_viz_index`` or ``source_partition_index.load_source_partition``
still intercepts the store's calls, and the hop costs one bound-method call
next to a disk read. Stateless; one shared instance is enough.
"""

from __future__ import annotations

from app.services.kg import scale_index, source_partition_index, viz_index


class KgScaleArtifactCodec:
    """Delegates the store's encode/decode calls to the ``services.kg`` modules."""

    __slots__ = ()

    def load_scale_index(self, directory):
        return scale_index.load_scale_index(directory)

    def save_scale_index(self, directory, **artifacts):
        return scale_index.save_scale_index(directory, **artifacts)

    def load_viz_index(self, directory):
        return viz_index.load_viz_index(directory)

    def save_viz_index(self, directory, **artifacts):
        return viz_index.save_viz_index(directory, **artifacts)

    def build_source_partition(self, raw, **identity):
        return source_partition_index.build_source_partition(raw, **identity)

    def save_source_partition(self, directory, partition):
        return source_partition_index.save_source_partition(directory, partition)

    def inspect_source_partition_manifest(self, root, **expected):
        return source_partition_index.inspect_source_partition_manifest(
            root, **expected
        )

    def load_source_partition(self, root, **expected):
        return source_partition_index.load_source_partition(root, **expected)

    def validate_partition_root(
        self, root, expected_parent_version, expected_parent_build_id=None
    ):
        return source_partition_index.validate_partition_root(
            root, expected_parent_version, expected_parent_build_id
        )


KG_SCALE_ARTIFACT_CODEC = KgScaleArtifactCodec()
