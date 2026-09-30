"""One fixture and one contract for the E2-4 store reads, run on both backends.

``test_store_evidence_ceiling_plans.py`` (SQLite) and
``postgres/test_store_evidence_ceiling_explain_pins.py`` seed the same rows
through ``seed`` (placeholders rewritten per dialect) and assert the same
``check_*`` contract, so the two stores cannot drift apart:

* B-6  ``in_network_relation_rows(allowed_source_ids=)`` -- in-ceiling rows
  only, one row per edge with ``source_count`` = distinct in-ceiling sources;
* B-9  ``chunk_exact_search(allowed_source_ids=)`` -- the ceiling filters
  below the probe window;
* B-11 ``node_context`` -- elements are re-read only from the object's own
  library; a promoted object's foreign evidence keeps its stored span/title;
* N-4  ``follow_relation_evidence_rows`` -- titles only from the relation's
  own library, and ``notebook_id=`` restricts the rows.
"""
from __future__ import annotations

import json
from typing import Any, Callable

NB = "nb-pub"          # the library the objects live in
PRIV = "nb-priv"       # the promoter's private library
USER = "u-e24"
NOW = "2026-09-30T00:00:00+00:00"

OWN_TEXT = "own element text"
PRIVATE_TEXT = "PRIVATE CURRENT TEXT"
PRIVATE_TITLE = "Private Current Name"
STORED_SPAN = "stored snapshot"
STORED_TITLE = "Stored Title"
STORED_QUOTE = "stored quote"
NEEDLE = "zqxident77"
OUT_OF_CEILING_HITS = 50
IN_CEILING_EXACT_SOURCE = "s-01"
IN_CEILING_EXACT_CHUNK = "c-exact-in"

SOURCES = [f"s-{index:02d}" for index in range(60)]


def _own_evidence() -> str:
    return json.dumps([{
        "source_id": "s-01", "element_id": "el-own", "quoted_span": "qa",
        "source_title": "Doc s-01",
    }])


def _foreign_evidence() -> str:
    return json.dumps([{
        "source_id": "s-priv", "element_id": "el-priv",
        "quoted_span": STORED_SPAN, "source_title": STORED_TITLE,
    }])


# (id, notebook, source, src object, tgt object, edge, status)
RELATIONS = [
    ("kr-1", NB, "s-01", "ko-a", "ko-b", "supports", "pending"),
    ("kr-2", NB, "s-02", "ko-a", "ko-b", "supports", "verified"),
    ("kr-3", NB, "s-02", "ko-a", "ko-b", "supports", "pending"),   # same source twice
    ("kr-4", NB, "s-03", "ko-a", "ko-b", "supports", "pending"),
    ("kr-5", NB, "s-04", "ko-a", "ko-b", "supports", "rejected"),  # never counted
    ("kr-6", NB, "s-05", "ko-b", "ko-c", "related_to", "pending"),
    ("kr-7", NB, "s-priv", "ko-a", "ko-c", "related_to", "pending"),  # foreign pointer
    ("kr-8", PRIV, "s-priv", "ko-x", "ko-y", "related_to", "pending"),
]


def seed(execute: Callable[[str, tuple], Any], mark: str) -> None:
    """Insert the fixture; ``execute(sql, params)`` with ``?`` rewritten to
    ``mark``."""

    def run(sql: str, params: tuple) -> None:
        execute(sql.replace("?", mark), params)

    run("INSERT INTO users(id,email,display_name,role,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?)", (USER, "e24@example.test", "E", "admin", NOW, NOW))
    for notebook in (NB, PRIV):
        run("INSERT INTO notebooks(id,name,created_by,created_at,updated_at) "
            "VALUES (?,?,?,?,?)", (notebook, notebook, USER, NOW, NOW))
    for source in SOURCES:
        run("INSERT INTO sources(id,notebook_id,title,source_type,status,parse_status,"
            "created_at,updated_at) VALUES (?,?,?,'file','ready','ready',?,?)",
            (source, NB, f"Doc {source}", NOW, NOW))
    run("INSERT INTO sources(id,notebook_id,title,source_type,status,parse_status,"
        "created_at,updated_at) VALUES (?,?,?,'file','ready','ready',?,?)",
        ("s-priv", PRIV, PRIVATE_TITLE, NOW, NOW))
    for element, source, text in (
        ("el-own", "s-01", OWN_TEXT), ("el-priv", "s-priv", PRIVATE_TEXT),
    ):
        run("INSERT INTO source_elements(id,source_id,element_type,location_label,"
            "text,created_at) VALUES (?,?,'paragraph','p1',?,?)",
            (element, source, text, NOW))
    objects = [
        ("ko-a", NB, "concept", {"name": "alpha"}, _own_evidence(), "s-01"),
        ("ko-b", NB, "concept", {"name": "beta"}, _own_evidence(), "s-01"),
        ("ko-c", NB, "concept", {"name": "gamma"}, _own_evidence(), "s-01"),
        ("ko-prom", NB, "concept", {"name": "promoted"}, _foreign_evidence(), ""),
        ("ko-proc", NB, "procedure", {
            "name": "promoted procedure",
            "steps": [{"name": "step one", "element_id": "el-priv",
                       "quote": STORED_QUOTE}],
        }, _foreign_evidence(), ""),
        ("ko-x", PRIV, "concept", {"name": "x"}, "[]", "s-priv"),
        ("ko-y", PRIV, "concept", {"name": "y"}, "[]", "s-priv"),
    ]
    for object_id, notebook, kind, payload, evidence, source in objects:
        run("INSERT INTO knowledge_objects(id,notebook_id,object_type,status,payload,"
            "evidence,source_id,created_at,updated_at) "
            "VALUES (?,?,?,'approved',?,?,?,?,?)",
            (object_id, notebook, kind, json.dumps(payload), evidence, source, NOW, NOW))
    for relation in RELATIONS:
        run("INSERT INTO knowledge_relations(id,notebook_id,source_id,source_object_id,"
            "target_object_id,edge_type,review_status,evidence,created_at) "
            "VALUES (?,?,?,?,?,?,?,'[]',?)", (*relation, NOW))


def exact_chunks() -> list[tuple[str, str, str]]:
    """``(chunk id, source, text)``: 50 short out-of-ceiling hits that outrank
    the one long in-ceiling hit on either backend's score."""
    rows = [
        (f"c-exact-{index:02d}", SOURCES[10 + index], f"{NEEDLE} short {index}")
        for index in range(OUT_OF_CEILING_HITS)
    ]
    rows.append((
        IN_CEILING_EXACT_CHUNK, IN_CEILING_EXACT_SOURCE,
        f"{NEEDLE} " + " ".join(f"padding{n}" for n in range(120)),
    ))
    return rows


def edges(rows) -> dict[tuple[str, str, str], Any]:
    """``{(src, edge, tgt): source_count or None}`` of relation rows."""
    out = {}
    for row in rows:
        row = dict(row)
        out[(row["source_object_id"], row["edge_type"], row["target_object_id"])] = (
            int(row["source_count"]) if "source_count" in row else None
        )
    return out


# ------------------------------------------------------------------ contract
def check_in_network_relations(read: Callable[..., list]) -> None:
    """``read(object_ids, **kwargs)`` -> ``in_network_relation_rows`` rows."""
    ids = ["ko-a", "ko-b", "ko-c"]
    unbound = read(ids)
    assert edges(unbound) == {
        ("ko-a", "supports", "ko-b"): None,
        ("ko-a", "related_to", "ko-c"): None,
        ("ko-b", "related_to", "ko-c"): None,
    }
    # Only in-ceiling rows, counted by DISTINCT source; an edge with no
    # in-ceiling row is gone; s-99 names no row.
    assert edges(read(ids, allowed_source_ids=frozenset({"s-01", "s-02", "s-99"}))) == {
        ("ko-a", "supports", "ko-b"): 2,
    }
    # The rejected s-04 row never counts, not even inside the ceiling.
    wide = frozenset({"s-01", "s-02", "s-03", "s-04", "s-05", "s-priv"})
    assert edges(read(ids, allowed_source_ids=wide)) == {
        ("ko-a", "supports", "ko-b"): 3,
        ("ko-a", "related_to", "ko-c"): 1,
        ("ko-b", "related_to", "ko-c"): 1,
    }
    # A list works as well as a set; blanks are dropped, not "unrestricted".
    assert edges(read(ids, allowed_source_ids=["s-05", "", "s-05"])) == {
        ("ko-b", "related_to", "ko-c"): 1,
    }
    assert read(ids, allowed_source_ids=frozenset()) == []
    assert read(ids, allowed_source_ids=[""]) == []
    # Row order is the documented ORDER BY on both paths.
    ordered = [
        (dict(row)["source_object_id"], dict(row)["edge_type"], dict(row)["target_object_id"])
        for row in read(ids, allowed_source_ids=wide)
    ]
    assert ordered == sorted(ordered, key=lambda edge: (edge[0], edge[1], edge[2]))


def check_chunk_exact(search: Callable[..., list]) -> None:
    """``search(needle, k, **kwargs)`` -> ``chunk_exact_search`` hits."""
    unbound = search(NEEDLE, 5)
    assert len(unbound) == 5
    assert IN_CEILING_EXACT_CHUNK not in {hit["chunk_id"] for hit in unbound}
    # 50 out-of-ceiling hits no longer fill the window: the one in-ceiling
    # hit comes back.
    bound = search(NEEDLE, 5, allowed_source_ids=[IN_CEILING_EXACT_SOURCE])
    assert [hit["chunk_id"] for hit in bound] == [IN_CEILING_EXACT_CHUNK]
    assert bound[0]["source_id"] == IN_CEILING_EXACT_SOURCE
    assert search(NEEDLE, 5, allowed_source_ids=[]) == []
    # Empty ids are dropped (an all-empty list denies everything, it never
    # means "unrestricted").
    assert search(NEEDLE, 5, allowed_source_ids=[""]) == []


def check_node_context_owner(node_context: Callable[[str], dict]) -> None:
    """``node_context(object_id)`` in ``NB`` without a ceiling."""
    promoted = node_context("ko-prom")
    rendered = json.dumps(promoted, ensure_ascii=False, default=str)
    assert PRIVATE_TEXT not in rendered
    assert PRIVATE_TITLE not in rendered
    (occurrence,) = promoted["occurrences"]
    assert occurrence["element_text"] == STORED_SPAN
    assert occurrence["quoted_span"] == STORED_SPAN
    assert occurrence["source_title"] == STORED_TITLE
    assert occurrence["source_id"] == "s-priv"
    procedure = node_context("ko-proc")
    assert PRIVATE_TEXT not in json.dumps(procedure, ensure_ascii=False, default=str)
    assert [step["element_text"] for step in procedure["steps"]] == [STORED_QUOTE]
    # The object's own library is still enriched from the live element.
    own = node_context("ko-a")
    assert own["occurrences"][0]["element_text"] == OWN_TEXT


def check_follow_relation_evidence(read: Callable[..., list]) -> None:
    """``read(relation_ids, **kwargs)`` -> ``follow_relation_evidence_rows``."""
    titles = {
        dict(row)["id"]: dict(row)["source_title"]
        for row in read(["kr-1", "kr-7", "kr-8"])
    }
    assert titles["kr-1"] == "Doc s-01"
    # kr-7 lives in NB but points at the private library's source: no title.
    assert not titles["kr-7"]
    # kr-8 belongs to the private library and names its own source.
    assert titles["kr-8"] == PRIVATE_TITLE
    scoped = {dict(row)["id"] for row in read(["kr-1", "kr-7", "kr-8"], notebook_id=NB)}
    assert scoped == {"kr-1", "kr-7"}
