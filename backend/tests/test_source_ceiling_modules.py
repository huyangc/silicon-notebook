"""PR-B·B1: the per-backend ``source_ceiling`` modules — the one home for
honouring a source ceiling in SQL (normalisation, bound form and its cache,
the support predicate).  Pure unit tests; the SQL is exercised against real
databases in test_kg_enumeration_source_ceiling_store.py and the PostgreSQL
twin / EXPLAIN pins."""
from __future__ import annotations

import json
import sqlite3

import pytest

from app.repositories.postgres import id_binding as pg_binding
from app.repositories.postgres import source_ceiling as pg
from app.repositories.sqlite import id_binding as lite_binding
from app.repositories.sqlite import source_ceiling as lite
from app.services.source_scope import (
    CeilingSet,
    current_source_scope,
    source_scope_context,
)

BACKENDS = pytest.mark.parametrize("module", [pg, lite], ids=["postgres", "sqlite"])


@BACKENDS
def test_normalise_ceiling(module):
    assert module.normalise_ceiling(None) is None
    assert module.normalise_ceiling(()) == frozenset()
    assert module.normalise_ceiling(("",)) == frozenset()
    assert module.normalise_ceiling(["b", "", "a", "b"]) == frozenset({"a", "b"})
    assert module.normalise_ceiling(x for x in ("a", None)) == frozenset({"a"})
    clean = frozenset({"a", "b"})
    assert module.normalise_ceiling(clean) is clean
    dirty = frozenset({"a", ""})
    assert module.normalise_ceiling(dirty) == frozenset({"a"})


@BACKENDS
@pytest.mark.parametrize("value", ["src-ab12", b"src-ab12", ""])
def test_normalise_ceiling_rejects_one_string(module, value):
    # A str is an iterable of characters: "src-ab12" would silently become
    # the ceiling {"s", "r", "c", ...}.
    with pytest.raises(TypeError):
        module.normalise_ceiling(value)


def _ids(module, bound):
    if module is lite:
        return set(json.loads(bound.param))
    if bound.sql == "%s::text[]":
        return set(bound.param)
    return set(bound.param.split(pg_binding.ID_SEPARATOR))


@BACKENDS
def test_ceiling_param_is_memoised_on_the_ceiling_object_only(module):
    """The bound form lives ON the run's ``CeilingSet`` (one entry per backend)
    and nowhere else: the same object is served its own form, an equal but
    distinct object builds an equal form of its own, and a plain frozenset
    binds without any memo.  No process-level cache survives the run."""
    first = CeilingSet({"a", "b"})
    bound = module.ceiling_param(first)
    assert module.ceiling_param(first) is bound
    assert _ids(module, bound) == {"a", "b"}
    assert list(first.bound_forms.values()) == [bound]
    other = CeilingSet({"a", "b"})
    assert module.ceiling_param(other) is not bound
    assert module.ceiling_param(other) == bound
    plain = frozenset({"a", "b"})
    assert module.ceiling_param(plain) == bound
    assert module.ceiling_param(plain) is not module.ceiling_param(plain)
    assert not hasattr(module, "_cache")


def test_both_backends_memoise_side_by_side():
    ceiling = CeilingSet({"x", "y"})
    pg_bound = pg.ceiling_param(ceiling)
    lite_bound = lite.ceiling_param(ceiling)
    assert set(ceiling.bound_forms) == {"postgres", "sqlite"}
    assert pg.ceiling_param(ceiling) is pg_bound
    assert lite.ceiling_param(ceiling) is lite_bound


def test_the_scope_hands_out_ceiling_sets():
    """The run's frozen sets are ``CeilingSet``s from the start, so the
    catalog's ``SourceCeiling.members`` (the scope's own set, never copied)
    carries the memo to the store."""
    with source_scope_context("nb", {
        "mode": "include", "source_ids": ["a"], "hidden_source_ids": ["h"],
        "narrowed": True,
    }, None, {"peer": ["p"]}):
        scope = current_source_scope()
        assert isinstance(scope.source_ids, CeilingSet)
        assert isinstance(scope.hidden_source_ids, CeilingSet)
        assert isinstance(scope.source_ceiling_for("peer"), CeilingSet)


def test_postgres_bound_form_joins_text_and_falls_back_on_separator():
    plain = pg.ceiling_param(frozenset({"b", "a"}))
    assert plain.sql == "string_to_array(%s,E'\\x1f')"
    assert plain.param == "a\x1fb"
    odd = pg.ceiling_param(frozenset({"c", "a\x1fb"}))
    assert odd.sql == "%s::text[]" and odd.param == ["a\x1fb", "c"]


@pytest.mark.parametrize(
    "ceiling",
    [frozenset({"b", "a", "c"}), frozenset({"a\x1fb", "c"}), frozenset({"甲", 'q"t', "z,y"})],
)
def test_the_bound_form_is_the_id_binding_module_s(ceiling):
    """The binding half is ``id_binding``'s, not a second copy: a ceiling
    binds exactly as ``bind_ids`` over its sorted ids (PostgreSQL) or with
    ``sort=True`` (SQLite), and the predicate is the NON-driving
    ``member_of`` on both backends and both branches."""
    assert pg.ceiling_param(ceiling) == pg_binding.bind_ids(sorted(ceiling))
    assert lite.ceiling_param(ceiling) == lite_binding.bind_ids(ceiling, sort=True)
    for module, binding in ((pg, pg_binding), (lite, lite_binding)):
        bound = module.ceiling_param(ceiling)
        certified = module.evidence_support_sql("ko", bound, authoritative=False)
        authoritative = module.evidence_support_sql("ko", bound, authoritative=True)
        assert binding.member_of("kos.source_id", bound) in certified
        evidence = (module.EVIDENCE_ITEM_SOURCE if module is pg
                    else f"CAST({module.EVIDENCE_ITEM_SOURCE} AS TEXT)")
        assert binding.member_of(evidence, bound) in authoritative
    # SQLite's driving form (``drive_by``: no unary plus) would seek once per
    # ceiling id per candidate row; every ``IN`` list here is ``+``-guarded.
    lite_bound = lite.ceiling_param(ceiling)
    certified = lite.evidence_support_sql("ko", lite_bound, authoritative=False)
    authoritative = lite.evidence_support_sql("ko", lite_bound, authoritative=True)
    assert certified.count(" IN ") == 1 and "AND +kos.source_id IN " in certified
    assert authoritative.count(" IN ") == 1
    assert "+CAST(" + lite.EVIDENCE_ITEM_SOURCE + " AS TEXT) IN " in authoritative


@BACKENDS
def test_support_predicate_text(module):
    bound = module.ceiling_param(frozenset({"a"}))
    certified = module.evidence_support_sql("ko", bound, authoritative=False)
    authoritative = module.evidence_support_sql("ko", bound, authoritative=True)
    # One placeholder each; support is evidence (kos / evidence JSON), never
    # the owner column.
    for text in (certified, authoritative):
        assert text.count(bound.sql) == 1
        assert "ko.source_id" not in text
    assert "knowledge_object_sources kos" in certified and "kos.object_id=ko.id" in certified
    assert "ko.evidence" in authoritative and "knowledge_object_sources" not in authoritative
    assert authoritative.startswith("EXISTS (SELECT 1 FROM ")


def test_sqlite_reverse_index_probe_keeps_the_unary_plus():
    bound = lite.ceiling_param(frozenset({"a"}))
    assert f"+kos.source_id IN {bound.sql}" in lite.evidence_support_sql(
        "ko", bound, authoritative=False)



def test_sqlite_numeric_evidence_source_matches_its_text_id():
    """``{"source_id": 123}`` in the evidence JSON is an INTEGER to
    ``json_extract``; the bound list holds TEXT.  The evidence side is cast,
    so the object is supported by ceiling ``{"123"}`` -- as PostgreSQL's
    ``->>`` and the executor's Python re-check (``str()``) already read it."""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE knowledge_objects (id TEXT, notebook_id TEXT, evidence TEXT)")
    db.executemany("INSERT INTO knowledge_objects VALUES (?,?,?)", [
        ("numeric", "nb", json.dumps([{"source_id": 123}])),
        ("text", "nb", json.dumps([{"source_id": "123"}])),
        ("other", "nb", json.dumps([{"source_id": 124}])),
    ])
    bound = lite.ceiling_param(CeilingSet({"123"}))
    rows = db.execute(
        "SELECT id FROM knowledge_objects WHERE "
        + lite.evidence_support_sql("knowledge_objects", bound, authoritative=True)
        + " ORDER BY id", (bound.param,)).fetchall()
    assert [row[0] for row in rows] == ["numeric", "text"]
