"""PR-B·B1: the per-backend ``source_ceiling`` modules — the one home for
honouring a source ceiling in SQL (normalisation, bound form and its cache,
the support predicate).  Pure unit tests; the SQL is exercised against real
databases in test_kg_enumeration_source_ceiling_store.py and the PostgreSQL
twin / EXPLAIN pins."""
from __future__ import annotations

import json

import pytest

from app.repositories.postgres import id_binding as pg_binding
from app.repositories.postgres import source_ceiling as pg
from app.repositories.sqlite import id_binding as lite_binding
from app.repositories.sqlite import source_ceiling as lite

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
def test_ceiling_param_is_cached_per_object_and_never_crossed(module):
    first = frozenset({"a", "b"})
    bound = module.ceiling_param(first)
    assert module.ceiling_param(first) is bound
    assert _ids(module, bound) == {"a", "b"}
    # An equal but distinct object gets an equal form, never another
    # ceiling's; many ceilings through the small LRU each keep their own.
    assert _ids(module, module.ceiling_param(frozenset({"a", "b"}))) == {"a", "b"}
    for index in range(40):
        ceiling = frozenset({f"s-{index}", f"t-{index}"})
        assert _ids(module, module.ceiling_param(ceiling)) == set(ceiling)
    assert len(module._cache) <= module._CACHE_LIMIT
    # Evicted and recomputed: still its own form.
    assert _ids(module, module.ceiling_param(first)) == {"a", "b"}


def test_ceiling_param_identity_is_checked_not_trusted():
    """A cache entry whose key object is not THIS ceiling (an id reused
    after the original died) must not be served."""
    ceiling = frozenset({"x", "y"})
    impostor = pg_binding.BoundIds("string_to_array(%s,E'\\x1f')", "other")
    with pg._cache_lock:
        pg._cache[id(ceiling)] = (frozenset({"other"}), impostor)
    assert pg.ceiling_param(ceiling).param != "other"
    assert _ids(pg, pg.ceiling_param(ceiling)) == {"x", "y"}


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
        assert binding.member_of(module.EVIDENCE_ITEM_SOURCE, bound) in authoritative
    # SQLite's driving form (``drive_by``: no unary plus) would seek once per
    # ceiling id per candidate row; every ``IN`` list here is ``+``-guarded.
    lite_bound = lite.ceiling_param(ceiling)
    certified = lite.evidence_support_sql("ko", lite_bound, authoritative=False)
    authoritative = lite.evidence_support_sql("ko", lite_bound, authoritative=True)
    assert certified.count(" IN ") == 1 and "AND +kos.source_id IN " in certified
    assert authoritative.count(" IN ") == 1
    assert "+" + lite.EVIDENCE_ITEM_SOURCE + " IN " in authoritative


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
