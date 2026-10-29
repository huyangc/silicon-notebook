"""How many ``HOTPATH_INDEX_SPECS`` entries each hot-path batch currently
contributes -- the one table the per-batch tests check the registry total
against, so adding a batch edits this table instead of every batch test's
hand-written count and message.

The registry means "indexes that should exist now": a later batch that
supersedes an earlier batch's index removes that entry (batch 6 retired
batch 1's ``idx_clusters_nb_canonical`` and batch 3's only entry).
"""
from __future__ import annotations

# batch -> (live entries, note)
BATCH_ENTRY_COUNTS: dict[int, tuple[int, str]] = {
    1: (7, "eight built; idx_clusters_nb_canonical superseded by batch 6"),
    2: (2, "payload-search GIN + checkup-H5 partial index"),
    3: (0, "its keyset-covering index superseded by batch 6"),
    4: (3, "source-search GIN trigram indexes (0048)"),
    5: (3, "delete-jobization FK / keyset indexes (0049)"),
    6: (3, "generation-aware cluster replacements (0051)"),
    7: (1, "own-notebook paper-metadata covering index (0084)"),
    8: (1, "find_sources summary trigram GIN (operator-built, no migration)"),
    9: (2, "source-element reading-order replacements (0086)"),
}
EXPECTED_SPEC_TOTAL = sum(count for count, _ in BATCH_ENTRY_COUNTS.values())


def total_mismatch_message(names) -> str:
    """The assertion message for a registry whose size is not the table's."""
    rows = "; ".join(
        f"batch {batch}: {count} ({note})"
        for batch, (count, note) in sorted(BATCH_ENTRY_COUNTS.items())
    )
    return (
        f"expected {EXPECTED_SPEC_TOTAL} HOTPATH_INDEX_SPECS entries ({rows}), "
        f"found {len(names)}: {sorted(names)}"
    )
