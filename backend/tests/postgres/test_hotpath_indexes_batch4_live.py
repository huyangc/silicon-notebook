"""Live-PostgreSQL half of hot-path fix batch 4's contract that a fake
connection cannot exercise (see ``backend/tests/test_hotpath_indexes_batch4.py``
for the fake-connection half: migration<->spec anti-drift and the
VISIBLE_SOURCE_TYPES_PREDICATE reconciliation pins).

Things only a real server can prove:

  1. All three indexes actually build via ``install_hotpath_indexes`` (real
     ``CREATE INDEX CONCURRENTLY``) and migration 48 is a true no-op ledger
     entry once they exist online -- mirrors the batch-1/2/3 equivalents.
     Migration 0048's pre-existing-index validation DO block gets its accept
     path exercised here and its reject paths (wrong shape, INVALID residue)
     in their own tests below.
  2. **The three-key composite really does BitmapOr.** This is the load-bearing
     verification for the shape decision this batch made:
     ``idx_sources_nb_title_file_trgm`` carries TWO trigram keys so that the
     query's ``LOWER(title) LIKE … OR LOWER(file_name) LIKE …`` can be answered
     by scanning ONE index twice and OR-ing the bitmaps. Had the planner
     refused, the fallback was two separate two-key indexes. Only a real server
     can settle that, so it is asserted here rather than reasoned about.
  3. Every one of the three UNION legs is served by its own new GIN index
     (Bitmap Index Scan), by default, with NO planner knobs anywhere in this
     module -- no ``enable_seqscan=off``, no dropping of competing indexes.
     That is deliberate: a plan assertion that needs a knob is usually pinning
     the cost model rather than the change. Getting there required one thing
     the fixture must do and an earlier draft of it did not -- VACUUM the three
     tables after the bulk seed, so the GIN fastupdate pending list is merged
     and the planner sees the steady state rather than a load artifact. See
     ``_seed_search_corpus``'s docstring for the measured before/after.
  4. **The SQL the adapter actually issues is the UNION shape.** A shape guard
     in the same spirit as ``test_core_store_conformance.py``'s
     ``test_kg_extracted_batch_query_is_driven_by_page_source_ids``: the
     semantic tests in that file only check ANSWERS, and the whole point of
     this rewrite is that the old and new shapes give the SAME answers, so
     nothing there can catch a regression to the cross-table OR-EXISTS form.
     This captures the real query text with a spy (so a hand-copied SQL string
     cannot drift out of sync with the real one) and its plan.
"""
from __future__ import annotations

import psycopg
import pytest

from app.repositories.postgres._store_utils import normalize_timestamp
from app.repositories.postgres.hotpath_indexes import (
    inspect_hotpath_indexes,
    install_hotpath_indexes,
)
from app.repositories.postgres.migrator import PostgresMigrator
from app.repositories.postgres.source_store import SourceStore


pytestmark = pytest.mark.postgres_integration

_BATCH4_NAMES = (
    "idx_sources_nb_title_file_trgm",
    "idx_source_authors_nb_name_trgm",
    "idx_source_paper_meta_nb_ptitle_trgm",
)

NOW = "2026-07-22T10:00:00+00:00"


def _schema_of(database) -> str:
    with database.connect() as connection:
        return connection.execute(
            "SELECT current_schema() AS name"
        ).fetchone()["name"]


def _seed_notebook(database, notebook_id: str) -> None:
    now = normalize_timestamp(NOW)
    with database.write() as db:
        db.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s)",
            (notebook_id, notebook_id, now, now),
        )


_TARGET_NOTEBOOK = "nb-batch4-target"
# 20k sources in the notebook under test plus two 5k neighbours. Both numbers
# are load-bearing; see _seed_search_corpus's docstring for why neither can be
# shrunk without the plan assertions below quietly stopping to mean anything.
_TARGET_SOURCES = 20000
_NOISE_NOTEBOOKS = 2
_NOISE_SOURCES = 5000


def _seed_one_notebook(
    database, notebook_id: str, prefix: str, count: int, *, needles: bool
) -> None:
    """One notebook's worth of sources plus four author rows and one paper-meta
    row each, seeded server-side (INSERT … generate_series) so the whole corpus
    still costs well under a second."""
    now = normalize_timestamp(NOW)
    with database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        db.execute(
            "INSERT INTO sources"
            "(id,notebook_id,title,source_type,status,parse_status,file_name,"
            "file_path,file_size,file_hash,summary,doc_type,created_at,updated_at) "
            "SELECT %s||'-'||lpad(g::text,6,'0'), %s, "
            "'Reference Design Note '||g::text, 'pdf', 'extracted','extracted', "
            "%s||'-doc-'||lpad(g::text,6,'0')||'.pdf', 'uploads/x.pdf', 1, "
            "%s||'-hash-'||g::text, '', '', %s, %s "
            "FROM generate_series(0, %s) g",
            (prefix, notebook_id, prefix, prefix, now, now, count - 1),
        )
        if needles:
            # Hidden synthetic rows whose title carries EVERY needle below: the
            # partial index must not contain them, and the query must not
            # return them.
            db.execute(
                "INSERT INTO sources"
                "(id,notebook_id,title,source_type,status,parse_status,file_name,"
                "file_path,file_size,file_hash,summary,doc_type,created_at,updated_at) "
                "VALUES ('src-hidden-mem',%s,'zqxjtitle zqxjauthor zqxjpaper','memory',"
                "'extracted','extracted','zqxjfile.md','uploads/m.md',1,'hm','','',%s,%s),"
                "('src-hidden-kh',%s,'zqxjtitle zqxjauthor zqxjpaper','knowhow',"
                "'extracted','extracted','zqxjfile.md','uploads/k.md',1,'hk','','',%s,%s)",
                (notebook_id, now, now, notebook_id, now, now),
            )
        # Four author rows per source — the production notebook's own ratio
        # (210k author rows over 49k sources).
        db.execute(
            "INSERT INTO source_authors(id,source_id,notebook_id,position,name,"
            "affiliation,created_at) "
            "SELECT s.id||':auth:'||lpad(p::text,3,'0'), s.id, s.notebook_id, p, "
            "'Author '||p::text||' '||s.id, '', %s "
            "FROM sources s, generate_series(0,3) p WHERE s.notebook_id=%s",
            (now, notebook_id),
        )
        db.execute(
            "INSERT INTO source_paper_meta(source_id,notebook_id,is_paper,"
            "paper_title,venue,pub_year,doi,keywords,raw_json,model,"
            "created_at,updated_at) "
            "SELECT s.id, s.notebook_id, 1, 'Paper about '||s.id, '', NULL, '', "
            "'[]'::jsonb, '{}'::jsonb, '', %s, %s "
            "FROM sources s WHERE s.notebook_id=%s",
            (now, now, notebook_id),
        )
        if needles:
            # One rare needle per leg, each on a DIFFERENT source, so a leg
            # that silently stopped working cannot be covered by another leg.
            db.execute(
                "UPDATE sources SET title='Zqxjtitle Special Study' "
                "WHERE id=%s", (f"{prefix}-000001",),
            )
            db.execute(
                "UPDATE sources SET file_name='zqxjfile-000002.pdf' "
                "WHERE id=%s", (f"{prefix}-000002",),
            )
            db.execute(
                "UPDATE source_authors SET name='Zqxjauthor Lastname' "
                "WHERE source_id=%s AND position=0", (f"{prefix}-000003",),
            )
            db.execute(
                "UPDATE source_paper_meta SET paper_title='On Zqxjpaper Structures' "
                "WHERE source_id=%s", (f"{prefix}-000004",),
            )


def _seed_search_corpus(database) -> str:
    """Seed the target notebook plus two smaller neighbours; return the target's
    id.

    THREE properties of this corpus are load-bearing, and each was established
    by watching the planner rather than guessed:

    * **The neighbours.** ``notebook_id`` LEADS all three indexes precisely
      because a real deployment shares one ``sources`` / ``source_authors`` /
      ``source_paper_meta`` table across many notebooks, so the equality can
      narrow INSIDE index access. Seed every row into a single notebook and
      that leading key becomes a constant of selectivity 1.0 whose posting list
      covers the whole table -- the shape the index was designed for is then
      simply absent from the fixture.

    * **The target notebook's size.** The alternative plan the GIN has to beat
      is "walk a plain ``notebook_id`` btree, fetch every row of this notebook,
      filter". That alternative's cost grows with the notebook, while a
      selective trigram lookup's does not. 20k sources (the production
      notebook's own order of magnitude: 49k) with four author rows each
      (matching production's 210k-over-49k ratio) puts every leg firmly on the
      GIN side of that crossover.

    * **The VACUUM below, which is not hygiene.** A GIN index with
      ``fastupdate`` on (the default) parks freshly inserted entries in an
      unindexed PENDING LIST, and ``gincostestimate`` charges every GIN plan
      for scanning it. Immediately after a bulk seed that surcharge inflates
      the estimate roughly TENFOLD and the planner rejects its own index --
      measured on the benchmark corpus (migration 0048's own disposable
      benchmark schema, same shape as this fixture but not this fixture
      itself, so these exact figures are not reproducible by rerunning
      EXPLAIN here): the title arm was costed 1379 (a sequential scan won)
      before VACUUM and 85.52 (Bitmap Index Scan on the composite) after; the
      ``LIKE … OR LIKE …`` form went from ``idx_sources_notebook_status`` at
      1501 to a BitmapOr over two scans of the composite at 170.80; the
      paper-title leg went from ``idx_source_paper_meta_nb`` at 740 to its own
      GIN at 85.38. A bulk-load fixture without this VACUUM measures a
      transient state no production database stays in, and every plan
      assertion below would be pinning that artifact instead of the change.
      See migration 0048's header for the operator-facing consequence and
      those exact figures' source.
    """
    _seed_notebook(database, _TARGET_NOTEBOOK)
    _seed_one_notebook(
        database, _TARGET_NOTEBOOK, "src", _TARGET_SOURCES, needles=True
    )
    for index in range(_NOISE_NOTEBOOKS):
        other = f"nb-batch4-noise-{index}"
        _seed_notebook(database, other)
        _seed_one_notebook(
            database, other, f"noise{index}", _NOISE_SOURCES, needles=False
        )
    # VACUUM cannot run inside a transaction, so this needs its own autocommit
    # connection rather than ``database.write()`` (same technique the batch-2/3
    # live tests use for CONCURRENTLY). The scoped URL carries the test
    # schema's search_path, so these hit the right tables.
    with psycopg.connect(
        database.settings.database_url, autocommit=True
    ) as connection:
        connection.execute("SET statement_timeout=0")
        for table in ("sources", "source_authors", "source_paper_meta"):
            connection.execute(f"VACUUM {table}")
            connection.execute(f"ANALYZE {table}")
    return _TARGET_NOTEBOOK


@pytest.mark.xdist_group(name="postgres_hotpath_indexes_batch4")
def test_install_builds_the_three_new_indexes_and_is_idempotent(postgres_database):
    # One hop before migration 48 introduces the indexes, so they are genuinely
    # absent below -- the same "prove it's for real" structure as batch 1/2/3.
    assert PostgresMigrator(postgres_database).migrate(target_version=47) == 47
    schema = _schema_of(postgres_database)
    database_url = postgres_database.settings.database_url

    before = inspect_hotpath_indexes(database_url, schema=schema)
    for name in _BATCH4_NAMES:
        row = next(r for r in before["indexes"] if r["name"] == name)
        assert row["state"] == "缺失", name

    state = install_hotpath_indexes(database_url, schema=schema)
    assert all(row["state"] == "存在" for row in state["indexes"]), state

    # Idempotent rerun.
    repeated = install_hotpath_indexes(database_url, schema=schema)
    assert repeated == state

    # Migration 48's own plain (in-transaction) CREATE INDEX IF NOT EXISTS is a
    # true no-op ledger entry once the offline CONCURRENTLY builder already
    # built the indexes online -- and its validation DO block accepts them
    # (that is this test's accept-path coverage).
    assert PostgresMigrator(postgres_database).migrate() == 48
    after_migration = inspect_hotpath_indexes(database_url, schema=schema)
    assert after_migration == state


@pytest.mark.xdist_group(name="postgres_hotpath_indexes_batch4")
def test_both_trigram_keys_of_the_composite_are_live_and_chosen(postgres_database):
    """Each of the composite's TWO trigram keys must be independently usable.

    A multi-column GIN lets a scan constrain any subset of its keys, so the
    title arm uses ``(notebook_id, lower(title))`` and the file_name arm uses
    ``(notebook_id, lower(file_name))``, each leaving the other key free.
    Asserted with NO planner knobs.
    """
    assert PostgresMigrator(postgres_database).migrate() == 48
    notebook_id = _seed_search_corpus(postgres_database)

    arms = (
        ("title", "LOWER(title) LIKE %s", "%zqxjtitle%"),
        ("file_name", "LOWER(file_name) LIKE %s", "%zqxjfile%"),
    )
    # Measured in steady state: 85.52 for the composite against ~1379 for a
    # sequential scan, so a red here means the index stopped being usable, not
    # that a cost estimate drifted a few percent.
    with postgres_database.connect() as connection:
        for label, predicate, needle in arms:
            plan_text = "\n".join(
                str(row["QUERY PLAN"]) for row in connection.execute(
                    "EXPLAIN (COSTS OFF) SELECT id FROM sources WHERE notebook_id=%s "
                    "AND source_type NOT IN ('memory','knowhow') "
                    f"AND {predicate}",
                    (notebook_id, needle),
                ).fetchall()
            )
            assert (
                "Bitmap Index Scan on idx_sources_nb_title_file_trgm" in plan_text
            ), f"{label} arm must enter through the composite GIN:\n{plan_text}"
            # notebook_id must be INSIDE index access, not a post-scan filter --
            # the cross-notebook global-bitmap failure docs/operations.md
            # records for the legacy single-expression trigram indexes.
            assert "notebook_id = " in plan_text, f"{label}:\n{plan_text}"
            assert "Seq Scan" not in plan_text, f"{label}:\n{plan_text}"


@pytest.mark.xdist_group(name="postgres_hotpath_indexes_batch4")
def test_the_or_of_both_arms_bitmap_ors_one_composite_index(postgres_database):
    """THE shape decision of this batch, settled against a real planner: does one
    three-key index serve ``LIKE … OR LIKE …`` by being scanned twice?

    It does, by default, with no planner knobs -- that is what this test pins,
    and it is why the batch ships one three-key index instead of the documented
    fallback of two two-key indexes (one on lower(title), one on
    lower(file_name)). The fallback would not have helped anyway: two two-key
    indexes would each still carry the same ``notebook_id`` key at the same
    per-scan cost.

    It also settles a follow-up this batch explicitly considered and rejected:
    splitting the first UNION leg's ``OR`` into two separate single-arm legs.
    Measured on the benchmark corpus, that variant is a wash on a selective
    needle (0.13ms vs 0.16ms COUNT, inside noise) and consistently WORSE on a
    short one (33.94 vs 30.71 COUNT, 37.16 vs 33.13 page for a two-character
    needle) because it adds a fourth Append branch and a second full pass over
    ``sources`` when the pattern is too short for trigram extraction. BitmapOr
    inside one scan node is strictly the better shape.
    """
    assert PostgresMigrator(postgres_database).migrate() == 48
    notebook_id = _seed_search_corpus(postgres_database)

    with postgres_database.connect() as connection:
        plan_text = "\n".join(
            str(row["QUERY PLAN"]) for row in connection.execute(
                "EXPLAIN (COSTS OFF) SELECT id FROM sources "
                "WHERE notebook_id=%s AND source_type NOT IN ('memory','knowhow') "
                "AND (LOWER(title) LIKE %s OR LOWER(file_name) LIKE %s)",
                (notebook_id, "%zqxjtitle%", "%zqxjtitle%"),
            ).fetchall()
        )

    assert "BitmapOr" in plan_text, (
        "the two OR'd LIKE arms must be answerable by a BitmapOr over one "
        f"index:\n{plan_text}"
    )
    assert plan_text.count("Bitmap Index Scan on idx_sources_nb_title_file_trgm") == 2, (
        "expected BOTH arms to scan the SAME composite index -- one scan per "
        f"arm, each constraining its own trigram key:\n{plan_text}"
    )
    assert "Seq Scan" not in plan_text, plan_text


@pytest.mark.xdist_group(name="postgres_hotpath_indexes_batch4")
def test_author_leg_is_served_by_its_own_new_gin_index(postgres_database):
    """The author leg is the one the production diag caught scanning 210k rows
    whole (a parallel sequential scan inside a hashed subplan) -- the dominant
    term in the 363ms COUNT. It must now enter through its own index, and this
    is asserted with NO planner knobs at all."""
    assert PostgresMigrator(postgres_database).migrate() == 48
    notebook_id = _seed_search_corpus(postgres_database)

    with postgres_database.connect() as connection:
        plan_text = "\n".join(
            str(row["QUERY PLAN"]) for row in connection.execute(
                "EXPLAIN (COSTS OFF) SELECT a.source_id FROM source_authors a "
                "WHERE a.notebook_id=%s AND LOWER(a.name) LIKE %s",
                (notebook_id, "%zqxjauthor%"),
            ).fetchall()
        )

    assert "Bitmap Index Scan on idx_source_authors_nb_name_trgm" in plan_text, (
        f"the author leg must enter through its own GIN:\n{plan_text}"
    )
    assert "notebook_id = " in plan_text, plan_text
    assert "Seq Scan" not in plan_text, plan_text


@pytest.mark.xdist_group(name="postgres_hotpath_indexes_batch4")
def test_paper_title_leg_is_served_by_its_own_new_gin_index(postgres_database):
    """The paper-title leg, asserted with no planner knobs.

    ``source_paper_meta`` is the smallest of the three tables (30,002 rows in
    this fixture -- every seeded source gets a paper-meta row here, unlike
    migration 0048's own disposable benchmark corpus, which only gives 80% of
    its sources one and lands on 24k; 39k in the production notebook's
    deployment) and it carries a pre-existing plain
    ``idx_source_paper_meta_nb`` btree, so this leg is the one whose index has
    the most to prove. In steady state it wins clearly: measured 85.38 for the
    new GIN against 740.55 for the btree-plus-filter alternative on the
    benchmark corpus. Since this fixture's table is smaller than production's,
    a green here is evidence for production too, not merely at production
    scale.
    """
    assert PostgresMigrator(postgres_database).migrate() == 48
    notebook_id = _seed_search_corpus(postgres_database)

    with postgres_database.connect() as connection:
        plan_text = "\n".join(
            str(row["QUERY PLAN"]) for row in connection.execute(
                "EXPLAIN (COSTS OFF) SELECT m.source_id FROM source_paper_meta m "
                "WHERE m.notebook_id=%s AND LOWER(m.paper_title) LIKE %s",
                (notebook_id, "%zqxjpaper%"),
            ).fetchall()
        )

    assert "Bitmap Index Scan on idx_source_paper_meta_nb_ptitle_trgm" in plan_text, (
        f"the paper-title leg must enter through its own GIN:\n{plan_text}"
    )
    assert "notebook_id = " in plan_text, plan_text
    assert "Seq Scan" not in plan_text, plan_text


@pytest.mark.xdist_group(name="postgres_hotpath_indexes_batch4")
def test_list_sources_page_issues_the_union_shape_and_its_plan_uses_the_indexes(
    postgres_database,
):
    """Shape guard against a regression to the cross-table OR-EXISTS predicate.

    The semantic tests in ``test_core_store_conformance.py`` cannot catch that
    regression -- the whole point of the rewrite is that both shapes return the
    SAME rows. This checks the query text the adapter actually issues (captured
    with the same spy the conformance file's plan tests use, so a hand-copied
    SQL string cannot drift out of sync with the real one) and the plan
    PostgreSQL produces for it.
    """
    assert PostgresMigrator(postgres_database).migrate() == 48
    notebook_id = _seed_search_corpus(postgres_database)
    sources = SourceStore(postgres_database, now=lambda: normalize_timestamp(NOW))

    captured: list[tuple[str, object]] = []
    original_connect = postgres_database.connect

    class _SpyingConnect:
        def __init__(self, inner):
            self._inner = inner

        def __enter__(self):
            connection = self._inner.__enter__()
            original_execute = connection.execute

            def spying_execute(sql, params=None, **kwargs):
                text = str(sql)
                if "FROM sources " in text and "COUNT(*)" in text:
                    captured.append((text, params))
                return original_execute(sql, params, **kwargs)

            connection.execute = spying_execute
            return connection

        def __exit__(self, *args):
            return self._inner.__exit__(*args)

    postgres_database.connect = lambda: _SpyingConnect(original_connect())
    try:
        page = sources.list_sources_page(notebook_id, offset=0, limit=50, q="zqxjauthor")
    finally:
        postgres_database.connect = original_connect

    # The author leg alone must find src-000003, and the two hidden synthetic
    # rows whose titles also carry the needle must stay out of both the items
    # and the count.
    assert page.total_count == 1
    assert [item.id for item in page.items] == ["src-000003"]

    assert captured, "list_sources_page must issue a COUNT over sources"
    captured_sql, captured_params = captured[0]
    assert "sources.id IN (" in captured_sql, (
        f"the q filter must be an id semi-join, not a cross-table OR:\n{captured_sql}"
    )
    assert captured_sql.count("UNION SELECT") == 2, (
        f"expected a three-leg UNION (two UNION keywords):\n{captured_sql}"
    )
    assert "UNION ALL" not in captured_sql, (
        f"UNION ALL would let a multi-leg hit be counted more than once "
        f"if the semi-join were ever replaced by a join:\n{captured_sql}"
    )
    assert "EXISTS(SELECT 1 FROM source_authors" not in captured_sql, (
        f"the retired cross-table OR-EXISTS shape is back:\n{captured_sql}"
    )
    assert "EXISTS(SELECT 1 FROM source_paper_meta" not in captured_sql, (
        f"the retired cross-table OR-EXISTS shape is back:\n{captured_sql}"
    )

    with postgres_database.connect() as connection:
        plan_text = "\n".join(
            str(row["QUERY PLAN"]) for row in connection.execute(
                f"EXPLAIN (COSTS OFF) {captured_sql}", captured_params
            ).fetchall()
        )

    # The structural win, which holds at every scale: the three legs are now
    # independent, separately-plannable relations under an Append, so NOTHING
    # is a hashed subplan any more. That -- not any single index -- is what
    # removed the "materialize all of source_authors, then all of
    # source_paper_meta, once per execution" cost the production diag measured.
    assert "SubPlan" not in plan_text, (
        f"a hashed SubPlan is the old cross-table-OR shape's signature -- the "
        f"three legs must be independent, indexable relations:\n{plan_text}"
    )
    assert "Append" in plan_text, (
        f"expected the three UNION legs to appear as an Append:\n{plan_text}"
    )
    # End to end, on the query the adapter really issues: every one of the
    # three new indexes is in the plan, and the first leg's two OR'd arms
    # BitmapOr two scans of the composite -- no planner knobs anywhere.
    for index_name in _BATCH4_NAMES:
        assert f"Bitmap Index Scan on {index_name}" in plan_text, (
            f"expected {index_name} in the COUNT plan:\n{plan_text}"
        )
    assert "BitmapOr" in plan_text, plan_text
    assert "Seq Scan on source_authors" not in plan_text, plan_text
    assert "Seq Scan on source_paper_meta" not in plan_text, plan_text


# ---------------------------------------------------------------------------
# Migration 48's pre-existing-index validation DO block (same pattern as
# migrations 0042 / 0043) -- IF NOT EXISTS alone would silently skip creation
# over an INVALID residue row or an operator's wrong-shape index and still mark
# the migration applied.
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group(name="postgres_hotpath_indexes_batch4")
def test_migration_rejects_a_same_named_wrong_shape_index(postgres_database):
    migrator = PostgresMigrator(postgres_database)
    assert migrator.migrate(target_version=47) == 47
    with postgres_database.write() as db:
        # Same name, same table, but a plain btree instead of the composite
        # GIN -- keys and predicate could look plausible while the index is
        # useless for a LIKE '%…%'.
        db.execute(
            "CREATE INDEX idx_sources_nb_title_file_trgm "
            "ON sources(notebook_id, title, file_name)"
        )
    with pytest.raises(
        psycopg.errors.RaiseException, match="does not match the expected definition"
    ):
        migrator.migrate()
    # The ledger did not advance -- RAISE rolled back the whole migration
    # transaction (including the ledger INSERT).
    assert migrator.migrate(target_version=47) == 47
    with postgres_database.write() as db:
        db.execute("DROP INDEX idx_sources_nb_title_file_trgm")
    assert migrator.migrate() == 48


@pytest.mark.xdist_group(name="postgres_hotpath_indexes_batch4")
def test_migration_rejects_a_gin_missing_the_partial_predicate(postgres_database):
    """The subtler wrong shape: right name, right table, right access method,
    right keys and opclasses -- but built WITHOUT the partial predicate, so it
    also indexes the hidden Memory/knowhow projection rows. The DO block must
    reject it on the predicate dimension alone."""
    migrator = PostgresMigrator(postgres_database)
    assert migrator.migrate(target_version=47) == 47
    with postgres_database.write() as db:
        db.execute(
            "CREATE INDEX idx_sources_nb_title_file_trgm ON sources USING gin ("
            "notebook_id public.text_ops, "
            "lower(title) public.gin_trgm_ops, "
            "lower(file_name) public.gin_trgm_ops)"
        )
    with pytest.raises(
        psycopg.errors.RaiseException, match="does not match the expected definition"
    ):
        migrator.migrate()
    assert migrator.migrate(target_version=47) == 47
    with postgres_database.write() as db:
        db.execute("DROP INDEX idx_sources_nb_title_file_trgm")
    assert migrator.migrate() == 48


@pytest.mark.xdist_group(name="postgres_hotpath_indexes_batch4")
def test_migration_rejects_an_invalid_same_named_index(postgres_database):
    """A real INVALID residue, no superuser catalog surgery (same rationale as
    batch 2/3's equivalents -- the CI PostgreSQL role is NOSUPERUSER):
    ``CREATE UNIQUE INDEX CONCURRENTLY`` over two source rows that share the
    same notebook_id fails at its second, catalog-visibility phase and leaves
    an ``indisvalid=false`` row behind -- the same shape an interrupted
    CONCURRENTLY build leaves."""
    migrator = PostgresMigrator(postgres_database)
    assert migrator.migrate(target_version=47) == 47
    now = normalize_timestamp(NOW)
    with postgres_database.write() as db:
        db.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) "
            "VALUES ('nb-inv-src','invalid-residue',%s,%s)",
            (now, now),
        )
        db.execute(
            "INSERT INTO sources"
            "(id,notebook_id,title,source_type,status,parse_status,file_name,"
            "file_path,file_size,file_hash,summary,doc_type,created_at,updated_at) "
            "VALUES ('src-inv-1','nb-inv-src','Dup','pdf','extracted','extracted',"
            "'a.pdf','uploads/a.pdf',1,'h1','','',%s,%s),"
            "('src-inv-2','nb-inv-src','Dup','pdf','extracted','extracted',"
            "'a.pdf','uploads/a.pdf',1,'h2','','',%s,%s)",
            (now, now, now, now),
        )
    with psycopg.connect(
        postgres_database.settings.database_url, autocommit=True
    ) as conn:
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "CREATE UNIQUE INDEX CONCURRENTLY idx_sources_nb_title_file_trgm "
                "ON sources(notebook_id, title, file_name)"
            )
        residue = conn.execute(
            "SELECT i.indisvalid FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = current_schema() "
            "AND c.relname = 'idx_sources_nb_title_file_trgm'"
        ).fetchone()
    assert residue is not None and residue[0] is False
    with pytest.raises(psycopg.errors.RaiseException, match="INVALID"):
        migrator.migrate()
    assert migrator.migrate(target_version=47) == 47
    with postgres_database.write() as db:
        db.execute("DROP INDEX idx_sources_nb_title_file_trgm")
    assert migrator.migrate() == 48


@pytest.mark.xdist_group(name="postgres_hotpath_indexes_batch4")
def test_migration_accepts_a_prebuilt_index_with_reloptions(postgres_database):
    """0048 header 与 docs/operations 承诺:预建的复合 GIN 可以带
    ``WITH (fastupdate = off)``(GIN 写放大的标准缓解手段),校验块只比对
    ``_matches_shape`` 覆盖的语义维度,不比对完整 ``pg_get_indexdef``,所以这个
    reloption 不会被判成形状不符。同款用例见批 2 的
    ``test_migration_accepts_a_prebuilt_index_with_reloptions``。"""
    migrator = PostgresMigrator(postgres_database)
    assert migrator.migrate(target_version=47) == 47
    with postgres_database.write() as db:
        db.execute(
            "CREATE INDEX idx_sources_nb_title_file_trgm "
            "ON sources USING gin ("
            "notebook_id public.text_ops, "
            "lower(title) public.gin_trgm_ops, "
            "lower(file_name) public.gin_trgm_ops) "
            "WHERE source_type NOT IN ('memory','knowhow')"
        )
        db.execute(
            "ALTER INDEX idx_sources_nb_title_file_trgm SET (fastupdate = off)"
        )
    assert migrator.migrate() == 48
    schema = _schema_of(postgres_database)
    state = inspect_hotpath_indexes(
        postgres_database.settings.database_url, schema=schema
    )
    by_name = {row["name"]: row["state"] for row in state["indexes"]}
    assert by_name["idx_sources_nb_title_file_trgm"] == "存在"
