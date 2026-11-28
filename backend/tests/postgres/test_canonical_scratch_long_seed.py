"""Canonical scratch preserves long seed identities across paged publication."""
import random
import string

import psycopg
import pytest

from app.repositories.postgres.migrator import PostgresMigrator
from app.repositories.postgres.schema_manifest import POSTGRES_SCHEMA_MANIFEST
from app.repositories.postgres.unified_kg_store import UnifiedKgStore

pytestmark = pytest.mark.postgres_integration
NOW = "2026-11-16T00:00:00+00:00"


def test_index_upgrade_preserves_existing_scratch_rows(postgres_database):
    with postgres_database.write() as db:
        # Reconstruct the v91 catalog/ledger and exercise the real upgrader.
        db.execute('DROP INDEX idx_kg_canonical_scratch_nb_run_seed')
        db.execute('CREATE INDEX idx_kg_canonical_scratch_nb_run_seed ON kg_canonical_scratch(notebook_id,run_id,seed)')
        db.execute('DROP INDEX idx_clusters_nb_canonical_name_lower')
        db.execute('CREATE INDEX idx_clusters_nb_canonical_name_lower ON concept_clusters(notebook_id,lower(canonical_name))')
        UnifiedKgStore.insert_canonical_scratch_rows(db, [('upgrade', 'run', 'seed', 'canonical', 'name', 'description', 'signature')])
        before = [dict(row) for row in db.execute("SELECT * FROM kg_canonical_scratch WHERE notebook_id='upgrade'")]
        db.execute('ALTER TABLE groups DROP COLUMN IF EXISTS is_public')
        db.execute('DELETE FROM silicon_schema_migrations WHERE version >= 92')
    assert PostgresMigrator(postgres_database).migrate() == POSTGRES_SCHEMA_MANIFEST.postgres_version
    with postgres_database.connect() as db:
        assert [dict(row) for row in db.execute("SELECT * FROM kg_canonical_scratch WHERE notebook_id='upgrade'")] == before
        index = db.execute("SELECT indexdef FROM pg_indexes WHERE schemaname=current_schema() "
                           "AND indexname='idx_kg_canonical_scratch_nb_run_seed'").fetchone()['indexdef']
        assert 'md5(seed)' in index


def test_duplicate_exact_seeds_still_fail_instead_of_selecting_one(postgres_database):
    with pytest.raises(psycopg.errors.UniqueViolation):
        with postgres_database.write() as db:
            db.execute("INSERT INTO notebooks(id,name,created_at,updated_at) VALUES ('duplicate','test',%s,%s)", (NOW, NOW))
            db.execute("INSERT INTO knowledge_objects(id,notebook_id,object_type,payload,evidence,created_at,updated_at) "
                       "VALUES ('member','duplicate','claim','{}','[]',%s,%s)", (NOW, NOW))
            db.execute("INSERT INTO kg_cluster_scratch(notebook_id,run_id,object_id,seed) VALUES ('duplicate','run','member','same')")
            UnifiedKgStore.insert_canonical_scratch_rows(db, [
                ('duplicate', 'run', 'same', cid, 'name', '', '') for cid in ('first', 'second')
            ])
            UnifiedKgStore.write_cluster_map_generation(db, 'duplicate', 'claim', 'run', NOW, 1, page_rows=1)


def test_long_incompressible_seeds_survive_scratch_and_paged_publication(postgres_database):
    rng = random.Random(1072)
    prefix = ''.join(rng.choices(string.ascii_letters + string.digits, k=12000))
    seeds = [prefix + '-first', prefix + '-second', '短种子']
    with postgres_database.write() as db:
        db.execute("INSERT INTO notebooks(id,name,created_at,updated_at) VALUES ('long-seed','test',%s,%s)", (NOW, NOW))
        for index, seed in enumerate(seeds):
            oid = f'long-object-{index}'
            db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,payload,evidence,created_at,updated_at) "
                       "VALUES (%s,'long-seed','claim','{}','[]',%s,%s)", (oid, NOW, NOW))
            db.execute("INSERT INTO kg_cluster_scratch(notebook_id,run_id,object_id,seed) VALUES ('long-seed','run',%s,%s)", (oid, seed))
        UnifiedKgStore.insert_canonical_scratch_rows(db, [
            ('long-seed', 'run', seed, f'canonical-{index}', seed, '', '')
            for index, seed in enumerate(seeds)
        ])
        cursor = None
        for _ in range(len(seeds) + 1):
            cursor = UnifiedKgStore.write_cluster_map_generation(
                db, 'long-seed', 'claim', 'run', NOW, 0, start=cursor, page_rows=1,
            )
            if cursor is None:
                break
        assert cursor is None
        rows = db.execute("SELECT member_object_id,canonical_id,canonical_name FROM concept_clusters "
                          "WHERE notebook_id='long-seed' ORDER BY member_object_id").fetchall()
        assert [(row['member_object_id'], row['canonical_id'], row['canonical_name']) for row in rows] == [
            (f'long-object-{index}', f'canonical-{index}', seed) for index, seed in enumerate(seeds)
        ]
        assert [row['seed'] for row in db.execute("SELECT seed FROM kg_canonical_scratch "
                                                "WHERE notebook_id='long-seed' ORDER BY canonical_id")] == seeds
    store = UnifiedKgStore(postgres_database)
    for index, seed in enumerate(seeds):
        assert store.resolve_focal('long-seed', seed.lower()) == f'canonical-{index}'
    assert store.resolve_focal('long-seed', prefix.lower()) is None
