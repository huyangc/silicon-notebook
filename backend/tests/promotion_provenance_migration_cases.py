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
  - ``ko-mem``: names a member's Memory source -> the entry is DROPPED (fail
    closed, M1), the object keeps ``[]``;
  - ``ko-memprom``: created by an approved Memory promotion (``pc-3``) ->
    rewritten as the approval does now: one "晋升自个人记忆：<title>" source per
    Memory, the card keeps only its stored quote (never the live text);
  - ``ko-nosrc``: an entry with NO source id in an attested library (the
    reverse index has no row for it) -> found by the extra scan, rewritten;
  - ``ko-own``: only its own source and a legacy string item -> untouched.
* ``nb-pp-base2``: a public library whose reverse index is NOT attested and
  whose object has no reverse-index row at all -> found by the evidence scan.
* ``nb-pp-private`` (personal): its objects are never touched.
"""
from __future__ import annotations

import json

from app.domain.promotion_provenance import (
    OriginElement,
    memory_origin_key,
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


NOSRC_ENTRY = {"source_title": "无来源", "element_id": "el-z",
               "element_type": "paragraph", "location_label": "p1",
               "quoted_span": "orphan quote", "confidence": 1.0}

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
    (BASE, "ko-memprom", "pc-3", [_entry("s-p", "el-p1", "card quote", "原件甲")],
     ("s-p",)),
    (BASE, "ko-nosrc", "", [NOSRC_ENTRY], ()),
    (BASE, "ko-own", "", ["legacy string", _entry("s-b", "el-b", "own", "公共原件")],
     ("s-b",)),
    (BASE2, "ko-b2", "", [_entry("s-p", "el-p1", "q", "原件甲")], ()),
    (PRIVATE, "ko-p", "", [_entry("s-p", "el-p1", "private", "原件甲")], ("s-p",)),
)
CANDIDATES = (
    # id, notebook, object, object_type
    ("pc-1", PRIVATE, "ko-p", "claim"),
    ("pc-2", PRIVATE, "ko-p", "claim"),
    ("pc-3", PRIVATE, "mem-pp2", "memory"),
)
MEMORIES = (("mem-pp", "记忆"), ("mem-pp2", "增益记忆"))


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
    for memory_id, title in MEMORIES:
        ins("memory_items", "id,notebook_id,created_by,origin,status,title,content_md,"
            "created_at,updated_at",
            (memory_id, PRIVATE, USER, "ask_answer", "confirmed", title, "x", NOW, NOW))
    for notebook, source_id, title, source_type, memory_id in SOURCES:
        ins("sources", "id,notebook_id,title,source_type,memory_id,created_at,updated_at",
            (source_id, notebook, title, source_type, memory_id, NOW, NOW))
    for element_id, source_id, text in ELEMENTS:
        ins("source_elements", "id,source_id,element_type,location_label,text,"
            "metadata,created_at",
            (element_id, source_id, "paragraph", "p1", text, "{}", NOW),
            json_columns=("metadata",))
    for candidate_id, notebook, object_id, object_type in CANDIDATES:
        ins("promotion_candidates", "id,notebook_id,object_id,object_type,status,"
            "created_at,updated_at,target_base_id",
            (candidate_id, notebook, object_id, object_type, "approved", NOW, NOW, BASE))
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
    memory_sources = {source_id for _nb, source_id, _t, kind, _m in SOURCES
                      if kind == "memory"}
    candidates = {row[0]: row for row in CANDIDATES}
    titles = dict(MEMORIES)
    out = {}
    for notebook, object_id, candidate, evidence, _rows in OBJECTS:
        if notebook == PRIVATE:
            out[object_id] = evidence
            continue
        own = {source_id for source_id, nb in notebooks.items() if nb == notebook}
        row = candidates.get(candidate)
        memory = (row[2], titles[row[2]]) if row and row[3] == "memory" else None
        out[object_id] = plan_promotion_evidence(
            notebook, evidence, own_source_ids=own,
            source_notebooks=notebooks, origin_elements=live,
            fallback_origin_notebook_id=row[1] if row else "",
            memory=memory, memory_source_ids=memory_sources,
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


def _memory_rewritten(base, memory_id, origin, element, quote, title, origin_notebook):
    source = _promo(base, memory_origin_key(memory_id))
    return {"source_id": source, "source_title": title,
            "element_id": promotion_element_id(source, element, quote),
            "element_type": "paragraph", "location_label": "p1",
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
    "ko-mem": [],
    "ko-memprom": [_memory_rewritten(BASE, "mem-pp2", "s-p", "el-p1", "card quote",
                                     "原件甲", PRIVATE)],
    "ko-nosrc": [{**NOSRC_ENTRY, "source_id": _promo(BASE, ""),
                  "element_id": promotion_element_id(_promo(BASE, ""), "el-z",
                                                     "orphan quote"),
                  "origin_source_id": "", "origin_notebook_id": ""}],
    "ko-own": ["legacy string", _entry("s-b", "el-b", "own", "公共原件")],
    "ko-b2": [_rewritten(BASE2, "s-p", "el-p1", "LIVE ONE", "q", "原件甲", PRIVATE)],
    "ko-p": [_entry("s-p", "el-p1", "private", "原件甲")],
}

EXPECTED_SOURCES = {
    # id: (notebook, title, type, status, parse_status)
    _promo(BASE, "s-p"): (BASE, "晋升自：原件甲", "promotion", "active", "extracted"),
    _promo(BASE, "s-x"): (BASE, "晋升自：已删原件", "promotion", "active", "extracted"),
    _promo(BASE, memory_origin_key("mem-pp2")): (
        BASE, "晋升自个人记忆：增益记忆", "promotion", "active", "extracted"),
    _promo(BASE, ""): (BASE, "晋升自：无来源", "promotion", "active", "extracted"),
    _promo(BASE2, "s-p"): (BASE2, "晋升自：原件甲", "promotion", "active", "extracted"),
}


def _element(base, origin, element, text, origin_notebook, element_type="paragraph"):
    source = _promo(base, origin)
    return (promotion_element_id(source, element, text), (
        source, element_type, "p1", text,
        {"promotion": {"origin_source_id": origin, "origin_element_id": element,
                       "origin_notebook_id": origin_notebook}},
    ))


def _memory_element(base, memory_id, origin, element, text, origin_notebook):
    source = _promo(base, memory_origin_key(memory_id))
    return (promotion_element_id(source, element, text), (
        source, "paragraph", "p1", text,
        {"promotion": {"origin_source_id": origin, "origin_element_id": element,
                       "origin_notebook_id": origin_notebook}},
    ))


EXPECTED_ELEMENTS = dict((
    # the Memory promotion's card keeps its quote, never the live text
    _memory_element(BASE, "mem-pp2", "s-p", "el-p1", "card quote", PRIVATE),
    _element(BASE, "s-p", "el-p1", "LIVE ONE", PRIVATE),
    _element(BASE, "s-p", "el-p2", "LIVE TWO", PRIVATE),
    # an entry's own element_type is kept on the entry; the element row needs
    # one, so an empty type is written as a paragraph
    _element(BASE, "s-x", "el-x", "snap", PRIVATE),
    _element(BASE, "", "el-z", "orphan quote", ""),
    _element(BASE2, "s-p", "el-p1", "LIVE ONE", PRIVATE),
))

EXPECTED_STATE = {BASE: (1, 1), BASE2: (1, 1)}

EXPECTED_REVERSE_INDEX = {
    ("ko-new", _promo(BASE, "s-p"), BASE),
    ("ko-merged", "s-b", BASE),
    ("ko-merged", _promo(BASE, "s-p"), BASE),
    ("ko-gone", _promo(BASE, "s-x"), BASE),
    ("ko-memprom", _promo(BASE, memory_origin_key("mem-pp2")), BASE),
    ("ko-nosrc", _promo(BASE, ""), BASE),
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
    state = {
        row["notebook_id"]: (int(row["dirty"]), int(row["kg_mutation_seq"]))
        for row in db.execute(
            "SELECT notebook_id,dirty,kg_mutation_seq FROM unified_kg_state").fetchall()
        if row["notebook_id"] in (BASE, BASE2, PRIVATE)
    }
    return {"sources": sources, "elements": elements, "evidence": evidence,
            "reverse": reverse, "state": state}


def assert_migrated(after: dict) -> None:
    assert after["evidence"] == EXPECTED_EVIDENCE
    assert after["sources"] == EXPECTED_SOURCES
    assert after["elements"] == EXPECTED_ELEMENTS
    assert after["reverse"] == EXPECTED_REVERSE_INDEX
    # every touched library is marked dirty with its mutation seq bumped once
    # (the attested library had a state row at seq 0; the other gets one)
    assert after["state"] == EXPECTED_STATE
    # nothing points into another notebook any more, except the personal one
    for object_id, items in after["evidence"].items():
        if object_id == "ko-p":
            continue
        for item in items:
            if isinstance(item, dict):
                assert not item["source_id"].startswith("s-p"), (object_id, item)
    # nothing of a member's Memory was copied: neither its live element text
    # nor the excerpt a pre-guard entry stored
    texts = {row[3] for row in after["elements"].values()}
    assert "MEMORY FULL TEXT" not in texts and "mem quote" not in texts
    # the Memory promotion's card was not widened to the live element text
    assert after["evidence"]["ko-memprom"][0]["quoted_span"] == "card quote"
