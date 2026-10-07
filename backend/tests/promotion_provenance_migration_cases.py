"""The pre-upgrade world of the PR-E8 provenance migration, shared by SQLite
``_migration_88`` (``test_promotion_provenance_migration.py``) and PostgreSQL
``0068_promotion_provenance.sql`` (``postgres/test_promotion_provenance_migration_pg.py``).

Both backends seed this world, run their migration, and must arrive at the
SAME ``EXPECTED`` rows -- that is the cross-backend equality -- and
``expected_from_planner`` derives the evidence half of ``EXPECTED`` from the
runtime planner itself, so the migration and the approval paths cannot drift.

* ``nb-pp-base``: a public library whose reverse index is attested.
  - ``ko-new``: a fresh promotion (``source_candidate_id`` = ``pc-1``) whose
    entry names the promoter's live element -> the element's CURRENT text;
  - ``ko-merged``: a native object with its own entry plus a merged foreign
    one (no candidate id) -> only the foreign entry moves;
  - ``ko-gone``: the promoter's source is gone -> the stored quote, the
    candidate's notebook as the origin notebook;
  - ``ko-drop``: gone and no quote -> dropped, the object keeps ``[]``;
  - ``ko-mem``: names a member's Memory element -> its text is never read,
    the stored quote is used;
  - ``ko-own``: only its own source and a legacy string item -> untouched.
* ``nb-pp-base2``: a public library whose reverse index is NOT attested and
  whose object has no reverse-index row at all -> found by the evidence scan.
* ``nb-pp-private`` (personal): its objects are never touched.
"""
from __future__ import annotations

import json

from app.domain.promotion_provenance import (
    OriginElement,
    plan_promotion_evidence,
    promotion_element_id,
    promotion_source_id,
)

NOW = "2026-10-01T00:00:00+00:00"
USER = "u-pp"
BASE = "nb-pp-base"
BASE2 = "nb-pp-base2"
PRIVATE = "nb-pp-private"

SOURCES = (
    # notebook, id, title, type, memory_id
    (PRIVATE, "s-p", "原件甲", "markdown", None),
    (PRIVATE, "s-mem", "记忆来源", "memory", "mem-pp"),
    (BASE, "s-b", "公共原件", "markdown", None),
)
ELEMENTS = (
    ("el-p1", "s-p", "LIVE ONE"),
    ("el-p2", "s-p", "LIVE TWO"),
    ("el-mem", "s-mem", "MEMORY FULL TEXT"),
    ("el-b", "s-b", "PUBLIC TEXT"),
)


def _entry(source_id, element_id, quote, title, *, element_type="paragraph"):
    item = {"source_id": source_id, "source_title": title, "element_id": element_id,
            "element_type": element_type, "location_label": "p1",
            "confidence": 1.0}
    if quote is not None:
        item["quoted_span"] = quote
    return item


OBJECTS = (
    # notebook, id, candidate, evidence, reverse-index rows
    (BASE, "ko-new", "pc-1", [_entry("s-p", "el-p1", "stored one", "原件甲")], ("s-p",)),
    (BASE, "ko-merged", "", [
        _entry("s-b", "el-b", "public quote", "公共原件"),
        _entry("s-p", "el-p2", "stored two", "原件甲"),
    ], ("s-b", "s-p")),
    (BASE, "ko-gone", "pc-2", [_entry("s-x", "el-x", "snap", "已删原件",
                                      element_type="")], ("s-x",)),
    (BASE, "ko-drop", "", [_entry("s-y", "el-y", None, "无摘录")], ("s-y",)),
    (BASE, "ko-mem", "", [_entry("s-mem", "el-mem", "mem quote", "记忆来源")], ("s-mem",)),
    (BASE, "ko-own", "", ["legacy string", _entry("s-b", "el-b", "own", "公共原件")],
     ("s-b",)),
    (BASE2, "ko-b2", "", [_entry("s-p", "el-p1", "q", "原件甲")], ()),
    (PRIVATE, "ko-p", "", [_entry("s-p", "el-p1", "private", "原件甲")], ("s-p",)),
)
CANDIDATES = (("pc-1", PRIVATE, "ko-p"), ("pc-2", PRIVATE, "ko-p"))


def seed(db, *, postgres: bool) -> None:
    p = "%s" if postgres else "?"
    js = "%s::jsonb" if postgres else "?"

    def ins(table, columns, values, json_columns=()):
        names = [c.strip() for c in columns.split(",")]
        marks = ",".join(js if n in json_columns else p for n in names)
        db.execute(f"INSERT INTO {table}({columns}) VALUES ({marks})", values)

    if postgres:
        ins("users", "id,email,display_name,role,status,created_at,updated_at,"
            "username,password_hash,password_salt,password_iterations",
            (USER, "pp@example.test", USER, "user", "active", NOW, NOW,
             "p00000001", "", "", 0))
    else:
        ins("users", "id,email,display_name,role,status,created_at,updated_at",
            (USER, "pp@example.test", USER, "user", "active", NOW, NOW))
    for notebook, tier in ((BASE, "base"), (BASE2, "base"), (PRIVATE, "personal")):
        ins("notebooks", "id,name,purpose,primary_domain,status,created_by,"
            "created_at,updated_at,tier",
            (notebook, notebook, "", "", "ready", USER, NOW, NOW, tier))
    ins("unified_kg_state", "notebook_id,source_index_backfilled,updated_at",
        (BASE, 1, NOW))
    ins("memory_items", "id,notebook_id,created_by,origin,status,title,content_md,"
        "created_at,updated_at",
        ("mem-pp", PRIVATE, USER, "ask_answer", "confirmed", "记忆", "x", NOW, NOW))
    for notebook, source_id, title, source_type, memory_id in SOURCES:
        ins("sources", "id,notebook_id,title,source_type,memory_id,created_at,updated_at",
            (source_id, notebook, title, source_type, memory_id, NOW, NOW))
    for element_id, source_id, text in ELEMENTS:
        ins("source_elements", "id,source_id,element_type,location_label,text,"
            "metadata,created_at",
            (element_id, source_id, "paragraph", "p1", text, "{}", NOW),
            json_columns=("metadata",))
    for candidate_id, notebook, object_id in CANDIDATES:
        ins("promotion_candidates", "id,notebook_id,object_id,object_type,status,"
            "created_at,updated_at,target_base_id",
            (candidate_id, notebook, object_id, "claim", "approved", NOW, NOW, BASE))
    for notebook, object_id, candidate, evidence, index_rows in OBJECTS:
        ins("knowledge_objects", "id,notebook_id,object_type,status,source_id,payload,"
            "evidence,source_candidate_id,created_at,updated_at",
            (object_id, notebook, "claim", "approved", "",
             json.dumps({"name": object_id}), json.dumps(evidence, ensure_ascii=False),
             candidate, NOW, NOW),
            json_columns=("payload", "evidence"))
        for source_id in index_rows:
            ins("knowledge_object_sources", "object_id,source_id,notebook_id",
                (object_id, source_id, notebook))


def _promo(base, origin):
    return promotion_source_id(base, origin)


def expected_from_planner() -> dict:
    """Every object's evidence after the migration, computed by the RUNTIME
    planner with what the seeded world lets it read."""
    live = {element_id: OriginElement(source_id, text)
            for element_id, source_id, text in ELEMENTS if source_id != "s-mem"}
    notebooks = {source_id: notebook for notebook, source_id, *_ in SOURCES}
    candidates = {candidate_id: notebook for candidate_id, notebook, _ in CANDIDATES}
    out = {}
    for notebook, object_id, candidate, evidence, _rows in OBJECTS:
        if notebook == PRIVATE:
            out[object_id] = evidence
            continue
        own = {source_id for source_id, nb in notebooks.items() if nb == notebook}
        out[object_id] = plan_promotion_evidence(
            notebook, evidence, own_source_ids=own,
            source_notebooks=notebooks, origin_elements=live,
            fallback_origin_notebook_id=candidates.get(candidate, ""),
        ).evidence
    return out


def _rewritten(base, origin, element, text, quote, title, origin_notebook,
               element_type="paragraph"):
    source = _promo(base, origin)
    return {"source_id": source, "source_title": title,
            "element_id": promotion_element_id(source, element, text),
            "element_type": element_type, "location_label": "p1",
            "confidence": 1.0, "quoted_span": quote,
            "origin_source_id": origin, "origin_notebook_id": origin_notebook}


# The literal expectation (the planner must agree with it, and so must both
# migrations).
EXPECTED_EVIDENCE = {
    "ko-new": [_rewritten(BASE, "s-p", "el-p1", "LIVE ONE", "stored one", "原件甲", PRIVATE)],
    "ko-merged": [
        _entry("s-b", "el-b", "public quote", "公共原件"),
        _rewritten(BASE, "s-p", "el-p2", "LIVE TWO", "stored two", "原件甲", PRIVATE),
    ],
    "ko-gone": [_rewritten(BASE, "s-x", "el-x", "snap", "snap", "已删原件", PRIVATE,
                           element_type="")],
    "ko-drop": [],
    "ko-mem": [_rewritten(BASE, "s-mem", "el-mem", "mem quote", "mem quote", "记忆来源",
                          PRIVATE)],
    "ko-own": ["legacy string", _entry("s-b", "el-b", "own", "公共原件")],
    "ko-b2": [_rewritten(BASE2, "s-p", "el-p1", "LIVE ONE", "q", "原件甲", PRIVATE)],
    "ko-p": [_entry("s-p", "el-p1", "private", "原件甲")],
}

EXPECTED_SOURCES = {
    # id: (notebook, title, type, status, parse_status)
    _promo(BASE, "s-p"): (BASE, "晋升自：原件甲", "promotion", "active", "parsed"),
    _promo(BASE, "s-x"): (BASE, "晋升自：已删原件", "promotion", "active", "parsed"),
    _promo(BASE, "s-mem"): (BASE, "晋升自：记忆来源", "promotion", "active", "parsed"),
    _promo(BASE2, "s-p"): (BASE2, "晋升自：原件甲", "promotion", "active", "parsed"),
}


def _element(base, origin, element, text, origin_notebook, element_type="paragraph"):
    source = _promo(base, origin)
    return (promotion_element_id(source, element, text), (
        source, element_type, "p1", text,
        {"promotion": {"origin_source_id": origin, "origin_element_id": element,
                       "origin_notebook_id": origin_notebook}},
    ))


EXPECTED_ELEMENTS = dict((
    _element(BASE, "s-p", "el-p1", "LIVE ONE", PRIVATE),
    _element(BASE, "s-p", "el-p2", "LIVE TWO", PRIVATE),
    # an entry's own element_type is kept on the entry; the element row needs
    # one, so an empty type is written as a paragraph
    _element(BASE, "s-x", "el-x", "snap", PRIVATE),
    _element(BASE, "s-mem", "el-mem", "mem quote", PRIVATE),
    _element(BASE2, "s-p", "el-p1", "LIVE ONE", PRIVATE),
))

EXPECTED_REVERSE_INDEX = {
    ("ko-new", _promo(BASE, "s-p"), BASE),
    ("ko-merged", "s-b", BASE),
    ("ko-merged", _promo(BASE, "s-p"), BASE),
    ("ko-gone", _promo(BASE, "s-x"), BASE),
    ("ko-mem", _promo(BASE, "s-mem"), BASE),
    ("ko-own", "s-b", BASE),
    ("ko-b2", _promo(BASE2, "s-p"), BASE2),
    ("ko-p", "s-p", PRIVATE),
}


def snapshot(db) -> dict:
    def as_json(value):
        return json.loads(value) if isinstance(value, str) else value

    sources = {
        row["id"]: (row["notebook_id"], row["title"], row["source_type"],
                    row["status"], row["parse_status"])
        for row in db.execute(
            "SELECT id,notebook_id,title,source_type,status,parse_status FROM sources "
            "WHERE source_type='promotion'").fetchall()
    }
    elements = {
        row["id"]: (row["source_id"], row["element_type"], row["location_label"],
                    row["text"], as_json(row["metadata"]))
        for row in db.execute(
            "SELECT e.id,e.source_id,e.element_type,e.location_label,e.text,e.metadata "
            "FROM source_elements e JOIN sources s ON s.id=e.source_id "
            "WHERE s.source_type='promotion'").fetchall()
    }
    evidence = {
        row["id"]: as_json(row["evidence"])
        for row in db.execute("SELECT id,evidence FROM knowledge_objects").fetchall()
    }
    reverse = {
        (row["object_id"], row["source_id"], row["notebook_id"])
        for row in db.execute(
            "SELECT object_id,source_id,notebook_id FROM knowledge_object_sources"
        ).fetchall()
    }
    return {"sources": sources, "elements": elements, "evidence": evidence,
            "reverse": reverse}


def assert_migrated(after: dict) -> None:
    assert after["evidence"] == EXPECTED_EVIDENCE
    assert after["sources"] == EXPECTED_SOURCES
    assert after["elements"] == EXPECTED_ELEMENTS
    assert after["reverse"] == EXPECTED_REVERSE_INDEX
    # nothing points into another notebook any more, except the personal one
    for object_id, items in after["evidence"].items():
        if object_id == "ko-p":
            continue
        for item in items:
            if isinstance(item, dict):
                assert not item["source_id"].startswith("s-p"), (object_id, item)
    # what a member's Memory element holds was not copied
    assert all(row[3] != "MEMORY FULL TEXT" for row in after["elements"].values())
