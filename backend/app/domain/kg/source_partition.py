"""Source-partition companion format version, sunk from
app.services.kg.source_partition_index in B3.

Only ``SOURCE_PARTITION_FORMAT_VERSION`` moves — the one name
app.repositories (maintenance, both backends) imports directly to compare
against the persisted manifest's ``format_version`` before trusting a
companion CSR partition. The actual partition build/save/load I/O stays in
app.services.kg.source_partition_index (disk-heavy, explicitly out of scope
for this move — see filesystem/scale_artifact_store.py, which keeps
importing the I/O functions from the services module unchanged).
``app.services.kg.source_partition_index`` re-exports this name unchanged
for existing importers.
"""
from __future__ import annotations

SOURCE_PARTITION_FORMAT_VERSION = 2
