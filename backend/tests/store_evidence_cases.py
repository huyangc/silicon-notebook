"""One fixture and one contract for the E2-4 store reads, run on both backends.

``test_store_evidence_ceiling_plans.py`` (SQLite) and
``postgres/test_store_evidence_ceiling_explain_pins.py`` seed the same rows
through ``seed`` (placeholders rewritten per dialect) and assert the same
``check_*`` contract, so the two stores cannot drift apart:

* B-6  ``in_network_relation_rows(with_source_ids=)`` -- one row per edge and
  source (no id list bound), rejected rows never, today's rows without it;
* B-9  ``chunk_exact_search(allowed_source_ids=)`` -- the ceiling filters
  below the probe window;
* B-11 ``node_context`` -- elements are re-read only from the object's own
  library (occurrences, a definer's evidence, payload steps, legacy sibling
  steps, ``_element_texts`` ordinals); a promoted object's foreign evidence
  keeps its stored span/title;
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
    ("kr-def", NB, "s-01", "ko-definer", "ko-deft", "defines", "pending"),
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
        # ko-deft is defined by ko-definer, a promoted object (foreign evidence).
        ("ko-deft", NB, "concept", {"name": "defined"}, _own_evidence(), "s-01"),
        ("ko-definer", NB, "concept", {"name": "definer"}, _foreign_evidence(), ""),
        # A legacy promoted procedure: no payload steps, a section of its own.
        ("ko-leg", NB, "procedure", {"name": "legacy step", "section_path": "LS"},
         _foreign_evidence(), ""),
        # A MIXED pointer: names this library's source but another library's
        # element.
        ("ko-mixed", NB, "concept", {"name": "mixed"}, json.dumps([{
            "source_id": "s-01", "element_id": "el-priv",
            "quoted_span": "mixed stored", "source_title": "Doc s-01",
        }]), "s-01"),
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
    assert edges(read(ids)) == {
        ("ko-a", "supports", "ko-b"): None,
        ("ko-a", "related_to", "ko-c"): None,
        ("ko-b", "related_to", "ko-c"): None,
    }
    assert all("source_id" not in dict(row) for row in read(ids))
    sourced = [
        (dict(row)["source_object_id"], dict(row)["edge_type"],
         dict(row)["target_object_id"], dict(row)["source_id"])
        for row in read(ids, with_source_ids=True)
    ]
    # One row per edge and source: s-02's two rows collapse, the rejected s-04
    # row never appears; ordered by the edge, then the source.
    assert sourced == [
        ("ko-a", "related_to", "ko-c", "s-priv"),
        ("ko-a", "supports", "ko-b", "s-01"),
        ("ko-a", "supports", "ko-b", "s-02"),
        ("ko-a", "supports", "ko-b", "s-03"),
        ("ko-b", "related_to", "ko-c", "s-05"),
    ]
    assert read(["ko-a"], with_source_ids=True) == []


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
    # The other library's element is not even named.
    assert occurrence["element_id"] == ""
    # A mixed pointer (own source, another library's element): the stored
    # quote, the own source, and no element locator.
    (mixed,) = node_context("ko-mixed")["occurrences"]
    assert (mixed["source_id"], mixed["element_id"]) == ("s-01", "")
    assert mixed["element_text"] == "mixed stored"
    procedure = node_context("ko-proc")
    assert PRIVATE_TEXT not in json.dumps(procedure, ensure_ascii=False, default=str)
    assert [step["element_text"] for step in procedure["steps"]] == [STORED_QUOTE]
    # The object's own library is still enriched from the live element.
    own = node_context("ko-a")
    assert own["occurrences"][0]["element_text"] == OWN_TEXT
    # A definer promoted from the private library: its evidence is enriched
    # only from ko-deft's library, so the definition is the stored span.
    defined = node_context("ko-deft")
    assert PRIVATE_TEXT not in json.dumps(defined, ensure_ascii=False, default=str)
    assert defined["definition"] == STORED_SPAN
    assert defined["definition_basis"] == "defines_evidence"
    # A legacy promoted procedure (no payload steps): the sibling step's text
    # is read only from the object's own library.
    legacy = node_context("ko-leg")
    assert PRIVATE_TEXT not in json.dumps(legacy, ensure_ascii=False, default=str)
    assert [(step["name"], step["element_text"]) for step in legacy["steps"]] == [
        ("legacy step", ""),
    ]


def check_element_texts_owner(element_texts: Callable[..., tuple]) -> None:
    """``element_texts(ids, **kwargs)`` -> ``_element_texts``: with an owner
    library, texts AND document ordinals come from that library only (the
    first id naming a foreign element must not move the ordinal read there)."""
    texts, ordinal = element_texts(
        ["el-priv", "el-own"], with_ordinal=True, owner_notebook_id=NB,
    )
    assert texts == {"el-own": OWN_TEXT}
    assert "el-own" in ordinal and "el-priv" not in ordinal
    unowned, _ = element_texts(["el-priv", "el-own"])
    assert unowned == {"el-priv": PRIVATE_TEXT, "el-own": OWN_TEXT}


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


def seed_relation_vectors(execute: Callable[[str, tuple], Any], mark: str, vector: Any) -> None:
    """One relation vector per relation of the fixture (both libraries)."""
    for relation_id, notebook, *_rest in RELATIONS:
        execute(
            "INSERT INTO relation_embeddings(relation_id,notebook_id,vector,created_at) "
            "VALUES (?,?,?,?)".replace("?", mark),
            (relation_id, notebook, vector, NOW),
        )


def check_relation_delta_rows(read: Callable[..., list]) -> None:
    """``read(source_ids, **kwargs)`` -> ``relation_delta_rows`` rows (E2-2's
    Memory relation cache maps relations to sources in one batched read)."""
    sources = ["s-01", "s-02", "s-05", "s-99"]
    plain = [dict(row) for row in read(sources)]
    assert {row["vid"] for row in plain} == {"kr-1", "kr-2", "kr-3", "kr-6", "kr-def"}
    assert all("source_id" not in row for row in plain)
    sourced = [dict(row) for row in read(sources, with_source_id=True)]
    assert {(row["vid"], row["source_id"]) for row in sourced} == {
        ("kr-1", "s-01"), ("kr-2", "s-02"), ("kr-3", "s-02"), ("kr-6", "s-05"),
        ("kr-def", "s-01"),
    }
    assert all(row["vector"] for row in sourced)
    assert read([], with_source_id=True) == []
