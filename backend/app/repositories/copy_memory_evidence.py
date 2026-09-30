"""Deep copy (M2, E5-1): drop the evidence a Memory lent to a row the copy keeps.

A notebook copy never carries a Memory or anything derived from one. The
snapshot statements leave out every row whose OWN source is a Memory source,
but a Memory's evidence can also travel inside a row that is not Memory-derived:
a manual merge (``merge_objects_in_transaction``, both backends) appends the
merged object's evidence entries -- ``quoted_span`` (the Memory's text),
``source_title``, ``location_label``, ``element_id``, ``source_id`` -- to the
surviving object whole. Remapping cannot help (an unmapped id passes through,
the text was never an id), so the entries are removed at snapshot time, before
the service sees the rows, and ``copy_notebook`` stays unchanged.

Which entries: every entry whose ``source_id`` is a Memory source of the
notebook (``memory_sql``'s definition: ``source_type = 'memory'``, any member's,
orphans included). ``Evidence.source_id`` is a required field, so an entry
without one is not an evidence entry and is kept. What is kept is otherwise
byte-identical: a row none of whose entries came from a Memory keeps its
original JSON text.

Which columns: the snapshot's evidence arrays -- ``knowledge_objects``,
``knowledge_relations`` and ``knowledge_source_facts``. Relations and facts
cannot hold another source's evidence by construction (single-source relation
completion; the fact backfill refuses ``mixed_source_evidence``); they are
stripped anyway, because the rule is "no Memory text in the copy", not "no
Memory text where we expect it". No other snapshot column holds evidence
entries (object/fact ``payload`` holds the object's fields; ``chunks.element_ids``
names the chunk's own source's elements).

An object whose evidence becomes empty is kept with an empty array: the
product already lists evidence-less knowledge objects (manually created ones,
and every enumeration that renders "no evidence") and dropping it would also
drop the relations, vectors and cluster rows that point at it.

Mirror of the sibling ``strip_source_evidence_on`` (governance stores, E5-2),
which detaches one source's entries in place before that source is deleted;
this one works on the in-memory snapshot and never writes.
"""
from __future__ import annotations

import json
from typing import Iterable, MutableMapping

#: Snapshot tables whose ``evidence`` column is an array of Evidence entries.
EVIDENCE_TABLES = ("knowledge_objects", "knowledge_relations", "knowledge_source_facts")


def _entry_source(item: object) -> "str | None":
    if isinstance(item, dict):
        value = item.get("source_id")
        return value if isinstance(value, str) else None
    return None


def strip_memory_evidence(
    snapshot: MutableMapping[str, list],
    memory_source_ids: Iterable[str],
) -> int:
    """Remove, in place, every evidence entry citing one of ``memory_source_ids``
    from the snapshot's evidence arrays. Returns how many rows changed. Evidence
    is JSON text on both backends' snapshot rows (PostgreSQL rows pass through
    ``sqlite_compatible_row``); a value that does not parse as a JSON array is
    left as it is."""
    memory = frozenset(memory_source_ids)
    if not memory:
        return 0
    changed = 0
    for table in EVIDENCE_TABLES:
        for row in snapshot.get(table, ()):
            raw = row.get("evidence")
            if not raw:
                continue
            try:
                entries = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            except ValueError:
                continue
            if not isinstance(entries, list):
                continue
            kept = [item for item in entries if _entry_source(item) not in memory]
            if len(kept) != len(entries):
                row["evidence"] = json.dumps(kept, ensure_ascii=False)
                changed += 1
    return changed
