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

from app.models.schemas import NotebookCreate
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
