from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "backend" / "tests" / "fixtures"
GENERATOR = ROOT / "scripts" / "generate_repository_contract_fixtures.py"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_committed_v9_fixture_exists_and_is_self_contained():
    root = FIXTURES / "repository_v9"
    assert (root / "baseline.db").is_file()
    assert (root / "expected_snapshot.json").is_file()
    assert (root / "manifest.json").is_file()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["source_commit"] == "3334626"
    assert manifest["schema_version"] == 9
    assert manifest["storage_files"]


def test_v9_manifest_hashes_database_snapshot_and_every_storage_artifact():
    root = FIXTURES / "repository_v9"
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))

    for key in ("database", "expected_snapshot"):
        artifact = root / manifest[key]["path"]
        assert artifact.is_file()
        assert artifact.stat().st_size == manifest[key]["size"]
        assert _sha256(artifact) == manifest[key]["sha256"]

    storage = root / "storage"
    recorded = manifest["storage_files"]
    assert [entry["path"] for entry in recorded] == sorted(
        str(path.relative_to(storage))
        for path in storage.rglob("*")
        if path.is_file()
    )
    for entry in recorded:
        artifact = storage / entry["path"]
        assert artifact.stat().st_size == entry["size"]
        assert _sha256(artifact) == entry["sha256"]


def test_v9_database_contains_the_representative_mixed_format_state():
    database = FIXTURES / "repository_v9" / "baseline.db"
    sidecars = [Path(f"{database}-wal"), Path(f"{database}-shm")]
    assert not [path for path in sidecars if path.exists()]
    connection = sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
        assert connection.execute("SELECT COUNT(*) FROM notebooks").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM knowledge_objects").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM knowledge_relations").fetchone()[0] == 1
        vector_types = {
            row[0]
            for table in (
                "element_embeddings",
                "knowledge_embeddings",
                "relation_embeddings",
                "chunk_embeddings",
            )
            for row in connection.execute(f"SELECT typeof(vector) FROM {table}")
        }
        assert {"text", "blob"} <= vector_types
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()
    assert not [path for path in sidecars if path.exists()]


def test_expected_snapshot_has_rows_reads_context_and_ask_metadata():
    snapshot_path = FIXTURES / "repository_v9" / "expected_snapshot.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))

    assert set(snapshot) >= {
        "schema",
        "rows",
        "reads",
        "context",
        "ask_metadata",
    }
    # 快照 = 冻结的 v9 baseline.db 被【当前代码】打开后的状态：经 master
    # v10-v12、v13 Memory / Agent migration、v14 sources.memory_id migration、
    # v15 parse_status/source_type 覆盖索引 migration、v16 knowhow 表
    # migration、v17 source_paper_meta/source_authors 表 migration、v18
    # knowhow_cell_code 表 + role 词表重映射 migration、v19
    # notebook_assets.source_id 列 + 索引 migration、v20 notebook_bases 挂载
    # 表 + promotion_candidates.target_base_id 列，v21 normalized-anchor
    # expression index、v22 kg_build_jobs migration、v23 model_service_status
    # migration、v24 写锁瘦身改造点 2 的 kg_canonical_scratch 表 migration、
    # v25 credential scrub + system status table，以及 v26 knowhow_changes/
    # knowhow_milestones 表 migration 合法升级到当前版本。
    assert snapshot["schema"]["user_version"] == 26
    assert snapshot["rows"]["notebooks"]
    assert snapshot["reads"]["notebook"]
    assert snapshot["context"]["source_files"]
    assert snapshot["ask_metadata"]["jobs"]


def test_v9_fixture_replays_through_the_current_repository(tmp_path):
    spec = importlib.util.spec_from_file_location("repository_v9_fixture_generator", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    fixture_root = FIXTURES / "repository_v9"
    database = tmp_path / "baseline.db"
    storage = tmp_path / "storage"
    shutil.copyfile(fixture_root / "baseline.db", database)
    shutil.copytree(fixture_root / "storage", storage)
    with module._deterministic_runtime():
        repo = module._new_offline_repo(database, storage)
        actual = module.normalized_repository_snapshot(repo, "nb-fixture")
    expected = json.loads(
        (fixture_root / "expected_snapshot.json").read_text(encoding="utf-8")
    )
    assert actual == expected
