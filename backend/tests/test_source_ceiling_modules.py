"""PR-B·B1: the per-backend ``source_ceiling`` modules — the one home for
honouring a source ceiling in SQL (normalisation, bound form and its cache,
the support predicate).  Pure unit tests; the SQL is exercised against real
databases in test_kg_enumeration_source_ceiling_store.py and the PostgreSQL
twin / EXPLAIN pins."""
from __future__ import annotations

import json

import pytest

from app.repositories.postgres import source_ceiling as pg
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
        return set(json.loads(bound.value))
    if bound.sql == "%b":
        return set(bound.value)
    return set(bound.value.split(pg.SEPARATOR))


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
    impostor = pg.BoundCeiling("string_to_array(%s, E'\\x1f')", "other")
    with pg._cache_lock:
        pg._cache[id(ceiling)] = (frozenset({"other"}), impostor)
    assert pg.ceiling_param(ceiling).value != "other"
    assert _ids(pg, pg.ceiling_param(ceiling)) == {"x", "y"}


def test_postgres_bound_form_joins_text_and_falls_back_on_separator():
    plain = pg.ceiling_param(frozenset({"a", "b"}))
    assert plain.sql == "string_to_array(%s, E'\\x1f')"
    assert isinstance(plain.value, str) and set(plain.value.split("\x1f")) == {"a", "b"}
    odd = pg.ceiling_param(frozenset({"a\x1fb", "c"}))
    assert odd.sql == "%b" and sorted(odd.value) == ["a\x1fb", "c"]


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
