"""Batch 8 of ``HOTPATH_INDEX_SPECS``: ``idx_sources_nb_summary_trgm``, the
summary arm of ``find_sources``' metadata leg (fake-free, no live PG; the live
twin is ``tests/postgres/test_hotpath_indexes_batch8_live.py``).

Batch 8 has no migration (operator-built with ``scripts/build_hotpath_indexes.py
--apply``), so the anti-drift pair here is the spec against the QUERY: the leg
must spell the index's key expression and its partial predicate verbatim, or
the planner can never use it.
"""
from __future__ import annotations

from pathlib import Path

from app.repositories.postgres import source_store
from app.repositories.postgres.hotpath_indexes import HOTPATH_INDEX_SPECS
from app.repositories.postgres.source_store import (
    VISIBLE_SOURCE_TYPES_PREDICATE,
    _find_sources_term_legs,
)

_NAME = "idx_sources_nb_summary_trgm"
_MIGRATIONS = (
    Path(source_store.__file__).resolve().parent / "migrations"
)


def _spec():
    return next(spec for spec in HOTPATH_INDEX_SPECS if spec.name == _NAME)


def test_the_spec_is_a_partial_notebook_rooted_trigram_gin():
    spec = _spec()
    assert spec.table == "sources" and spec.using == "gin"
    assert spec.columns == ("notebook_id", "lower(summary)")
    assert spec.opclasses == ("public:text_ops", "public:gin_trgm_ops")
    assert spec.collations == ("pg_catalog:C", "pg_catalog:C")
    assert spec.predicate == VISIBLE_SOURCE_TYPES_PREDICATE
    rendered = spec.ddl("public", concurrently=True).as_string(None)
    assert rendered == (
        'CREATE INDEX CONCURRENTLY IF NOT EXISTS "idx_sources_nb_summary_trgm" '
        'ON "public"."sources" USING gin (notebook_id public.text_ops, '
        "lower(summary) public.gin_trgm_ops) "
        "WHERE source_type NOT IN ('memory','knowhow')"
    )


def test_the_summary_leg_spells_the_key_and_the_predicate_verbatim():
    statement, params = _find_sources_term_legs("nb", ["diffusion", "gan"])
    legs = statement.split(" UNION ALL ")
    summary = next(leg for leg in legs if "'summary'" in leg)
    assert f"notebook_id=%s AND {VISIBLE_SOURCE_TYPES_PREDICATE} " in summary
    assert summary.count("lower(summary) LIKE %s") == 2
    assert statement.count("%s") == len(params)


def test_batch8_has_no_migration_of_its_own():
    """Operator-built only: no migration may create it behind the registry's
    back (a later migration would need the 0048-style shape validation)."""
    for path in sorted(_MIGRATIONS.glob("*.sql")):
        assert _NAME not in path.read_text(encoding="utf-8"), path.name
