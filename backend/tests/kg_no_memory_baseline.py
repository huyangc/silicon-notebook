"""The E4-7 reads of a notebook WITHOUT Memory, as a comparable record.

The permission remediation's red line (plan §5 preamble): a notebook with no
mounted library and no Memory answers every read byte for byte as before.
``tests/fixtures/kg_no_memory_reads_baseline.json`` holds what these reads
answered at ``fac0298ec`` -- the commit E4-7 starts from, i.e. before the
viewer scope reached any of them.
``test_kg_service_readers.py::test_a_notebook_without_memory_answers_the_pre_e4_7_bytes``
compares the current code against that record, so a change that alters a
no-Memory answer goes red even when it alters the scoped and the unscoped
path alike (comparing the two paths of the current code with each other
could not see it).

Recapture only for a change that is MEANT to alter these answers: run
``capture(path)`` from a test on that code (the fixture is deterministic --
ids and timestamps are replaced by stable names below).  Never recapture to
make an E4-7-style change pass: that defeats the guard.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List

from app.models.schemas import NotebookCreate

FIXTURE = "kg_no_memory_reads_baseline.json"
# Reads that are ANSWERED (compared value for value).  ``summary`` keeps only
# the two fields E4-7 touches (``counts`` and ``kg_ready``): the rest of the
# card belongs to other surfaces and changes for their own reasons.
READS = ("list", "types", "graph", "search", "unified_full", "unified_bounded",
         "neighbours", "summary")
_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z)?")


def build_plain_world(repo, *, as_user: Callable[..., Any]) -> Dict[str, Any]:
    """One notebook, one plain source, a concept and a claim about it, a
    rebuilt unified KG and a published viz artifact -- no Memory at all."""
    user = repo.create_user("a00000021", "pw123456")
    nb = as_user(user, repo.create_notebook, NotebookCreate(name="plain")).id
    now = "2026-09-01T00:00:00"
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
            "file_name,file_path,file_size,file_hash,summary,doc_type,memory_id,"
            "created_at,updated_at) VALUES ('src-p',?,'src-p','markdown','extracted',"
            "'parsed','d.md','',0,'','','',NULL,?,?)", (nb, now, now))
        db.execute(
            "INSERT INTO source_elements (id,source_id,element_type,location_label,"
            "text,metadata,created_at) VALUES ('el-p','src-p','paragraph','p0',"
            "'PLAIN text','{}',?)", (now,))
    evidence = [{"source_id": "src-p", "source_title": "src-p", "element_id": "el-p",
                 "element_type": "paragraph", "location_label": "p",
                 "quoted_span": "quote el-p", "confidence": 1.0}]
    repo.store_kg(nb, "src-p", [
        {"local_id": "c", "object_type": "concept",
         "payload": {"name": "Plainword", "section_path": "1"}, "evidence": evidence},
        {"local_id": "d", "object_type": "claim",
         "payload": {"name": "plain claim", "section_path": "1"}, "evidence": evidence}],
        [{"source_local_id": "d", "target_local_id": "c", "edge_type": "about",
          "evidence": []}])
    repo.rebuild_unified_kg(nb)
    assert repo._runtime.scale_artifacts.build_viz(nb) is not None
    with repo._write() as db:
        concept = db.execute(
            "SELECT id FROM knowledge_objects WHERE notebook_id=? "
            "AND json_extract(payload,'$.name')='Plainword'", (nb,)).fetchone()["id"]
    return {"repo": repo, "user": user, "nb": nb, "concept": concept}


def reads(world) -> Dict[str, Callable[[], Any]]:
    repo, nb = world["repo"], world["nb"]

    def summary():
        card = repo.get_notebook(nb).model_dump()
        return {"counts": card["counts"], "kg_ready": card["kg_ready"]}

    return {
        "list": lambda: repo.list_knowledge(nb, "concept").model_dump(),
        "types": lambda: [t.model_dump() for t in repo.knowledge_types(nb)],
        "graph": lambda: repo.knowledge_graph(nb).model_dump(),
        "search": lambda: repo.kg_search(nb, "Plainword"),
        "unified_full": lambda: repo.unified_graph(nb, level="concept"),
        "unified_bounded": lambda: repo.unified_graph(nb, level="object", limit=80),
        "neighbours": lambda: repo.kg_neighbors(nb, world["concept"]),
        "summary": summary,
    }


def _names(world) -> List[tuple]:
    """``(random id, stable name)``, longest id first."""
    repo, nb = world["repo"], world["nb"]
    pairs = [(nb, "<notebook>"), (str(world["user"].id), "<user>")]
    with repo._runtime.database.connect() as db:
        objects = {}
        for row in db.execute(
            "SELECT id, object_type, json_extract(payload,'$.name') AS name "
            "FROM knowledge_objects WHERE notebook_id=?", (nb,)
        ).fetchall():
            objects[row["id"]] = f"<object {row['object_type']}:{row['name']}>"
        pairs += objects.items()
        for row in db.execute(
            "SELECT id, source_object_id, target_object_id, edge_type "
            "FROM knowledge_relations WHERE notebook_id=?", (nb,)
        ).fetchall():
            pairs.append((row["id"], "<relation {}>{}:{}>".format(
                objects.get(row["source_object_id"], "?"),
                objects.get(row["target_object_id"], "?"), row["edge_type"])))
    return sorted(pairs, key=lambda pair: -len(pair[0]))


def normalise(world, value: Any) -> Any:
    """``value`` as JSON with every random id and timestamp replaced by a
    stable name (the comparison stays byte for byte on everything else)."""
    text = json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
    for raw, name in _names(world):
        text = text.replace(raw, name)
    return json.loads(_TIMESTAMP.sub("<timestamp>", text))


def comparable(name: str, answer: Any) -> Any:
    """``answer`` in the form compared with the record: the neighbour view's
    viz path builds its node list from a set (its order follows the
    process's string hashing, so it was never part of the answer); every
    other read is compared as it stands, order included."""
    if name == "neighbours" and isinstance(answer, dict):
        return {**answer, "nodes": sorted(
            answer.get("nodes", []), key=lambda node: json.dumps(node, sort_keys=True))}
    return answer


def record(world, *, as_user: Callable[..., Any]) -> dict:
    """``{read: normalised answer}`` for every read, each run once to warm
    the per-process memos and once recorded.  Answers only: the statements
    a read issues change legitimately with the store work beside E4-7 (the
    E4-4 count memo, for one); E4-7's own statement delta is pinned apart,
    against the same code with the viewer scope switched off."""
    out = {}
    for name, read in reads(world).items():
        as_user(world["user"], read)
        out[name] = normalise(world, as_user(world["user"], read))
    return out


def capture(path, world, *, as_user) -> None:
    """Write the record (only for a change meant to alter these answers)."""
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(record(world, as_user=as_user), handle,
                  ensure_ascii=False, indent=1, sort_keys=True)
        handle.write("\n")
