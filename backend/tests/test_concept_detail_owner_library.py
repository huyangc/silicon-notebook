"""PR-E2·E2-4 (ledger B-11) on the KG concept detail panel, viewer-scoped path.

A cluster of a shared library whose member evidence also carries an item
pointing at an element of ANOTHER library (the shape a promotion leaves).
The unscoped path re-reads elements only from the cluster's own library and
shows the stored quote (``test_node_context.py``).  This file pins the branch
that runs when the library holds another member's Memory
(``KnowledgeQueryService._viewer_resolved_evidence``): there the viewer scope
only admits sources of THIS library, so the other library's item is dropped
for every viewer -- even for A, who owns that library -- and no current text
of it is ever shown.

That branch also passes ``owner_notebook_id``.  For an item that names the
other library's source (the shape here) removing it changes nothing -- the
scope drops the item either way -- so this test pins the outcome, not the
keyword.  The two differ only for a MIXED pointer (an item naming a source of
this library but an element of another): with the owner the element is not
read, the item keeps its stored span and is shown; without it the element's
real source is read back, judged unreadable and the item is dropped.  Neither
shows the other library's text.
"""
from __future__ import annotations

import json

import pytest

from app.models.schemas import NotebookCreate
from tests import test_kg_viewer_scope as kg_viewer_scope
from tests.test_kg_viewer_scope import (  # noqa: F401  (``repo`` is a fixture)
    _ev,
    _source,
    as_user,
    build_scenario,
    reader_of,
    repo,
)


def test_viewer_scoped_concept_detail_never_reads_another_librarys_element(repo):
    s = build_scenario(repo, b_memory=True)
    private = as_user(s.a, repo.create_notebook, NotebookCreate(name="private")).id
    with repo._write() as db:
        _source(db, private, "src-priv", elements=[("el-priv", "PRIVATE CURRENT TEXT")])
        row = db.execute(
            "SELECT evidence FROM knowledge_objects WHERE id=?", (s.ids.engram_s,),
        ).fetchone()
        evidence = json.loads(row["evidence"]) + [{
            **_ev("src-priv", "el-priv"), "quoted_span": "stored snapshot",
        }]
        db.execute("UPDATE knowledge_objects SET evidence=? WHERE id=?",
                   (json.dumps(evidence), s.ids.engram_s))
    assert as_user(s.a, reader_of(repo).for_notebook, s.nb) is not None
    canonical = repo.cluster_map(s.nb)[s.ids.engram_s]
    detail = as_user(s.a, repo.concept_detail, s.nb, canonical)
    assert "PRIVATE CURRENT TEXT" not in json.dumps(detail, ensure_ascii=False, default=str)
    texts = [item.get("element_text") for item in detail["evidence"]]
    assert "VISIBLE occurrence of Engram" in texts
    assert "stored snapshot" not in texts
    other = as_user(s.b, repo.concept_detail, s.nb, canonical)
    assert "PRIVATE CURRENT TEXT" not in json.dumps(other, ensure_ascii=False, default=str)
    assert "stored snapshot" not in [item.get("element_text") for item in other["evidence"]]


def check_mixed_pointer_never_reaches_the_raw_lists(
    repo, helpers, b_memory, *, update_evidence_sql,
):
    """A MIXED pointer -- an item naming this library's source but an element
    of ANOTHER library (``el-priv``, in A's private notebook) -- on a cluster
    member AND on an attached neighbour.  Enrichment clears its locator in the
    top-level ``evidence``; ``members[].evidence`` and ``attached[].evidence``
    are the raw stored items and must carry that same cleared locator, never
    the other library's element id (codex #823 r2 P2).  ``b_memory`` selects
    the branch: with B's Memory in the library A's view runs under the viewer
    rule, without it the detail is read unfiltered."""
    s = helpers.build_scenario(repo, b_memory=b_memory)
    private = helpers.as_user(s.a, repo.create_notebook, NotebookCreate(name="private")).id
    mixed = {**helpers._ev("src-s", "el-priv"), "quoted_span": "mixed stored"}
    with repo._write() as db:
        helpers._source(db, private, "src-priv",
                        elements=[("el-priv", "PRIVATE CURRENT TEXT")])
    for object_id in (s.ids.engram_s, s.ids.definer_s):
        with repo._write() as db:
            stored = db.execute(
                update_evidence_sql[0], (object_id,)).fetchone()["evidence"]
            stored = json.loads(stored) if isinstance(stored, str) else stored
            db.execute(update_evidence_sql[1],
                       (json.dumps([*stored, mixed]), object_id))

    detail = helpers.as_user(s.a, repo.concept_detail, s.nb, s.ids.engram_canonical)

    dumped = json.dumps(detail, ensure_ascii=False, default=str)
    assert "el-priv" not in dumped and "PRIVATE CURRENT TEXT" not in dumped
    raw = [
        item for entry in [*detail["members"], *detail["attached"]]
        for item in entry["evidence"] if item.get("quoted_span") == "mixed stored"
    ]
    # The item stays (it names a readable source of this library), on the
    # member and on the attached ``defines`` neighbour, without its locator.
    assert len(raw) == 2, raw
    assert all(item["element_id"] == "" and item["source_id"] == "src-s" for item in raw)
    top = [item for item in detail["evidence"] if item.get("quoted_span") == "mixed stored"]
    assert [item["element_id"] for item in top] == [""]


_SQLITE_EVIDENCE_SQL = (
    "SELECT evidence FROM knowledge_objects WHERE id=?",
    "UPDATE knowledge_objects SET evidence=? WHERE id=?",
)


@pytest.mark.parametrize("b_memory", [True, False])
def test_mixed_pointer_never_reaches_the_raw_lists(repo, b_memory):
    check_mixed_pointer_never_reaches_the_raw_lists(
        repo, kg_viewer_scope, b_memory, update_evidence_sql=_SQLITE_EVIDENCE_SQL,
    )
