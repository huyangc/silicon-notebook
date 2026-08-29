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
    # v25 凭据清除 + 系统模型服务状态表(#328)、v26 knowhow_changes/
    # knowhow_milestones 表 migration(#327)、v27 P1.5 的 sources.chunked_at
    # 完成标记列 migration，以及 v28 app_settings 表 + user_profiles.
    # upload_document_limit 列 migration，v29 cluster membership 唯一
    # 索引与确定性存量去重 migration，以及 v30 sources(notebook_id, file_hash)
    # 内容哈希去重索引 migration，以及 v31 两张 inert shadow capture 内部表，
    # v32 深度报告问题理解契约，以及 v33 relationship endpoint/id keyset
    # 覆盖索引、v34 关系补全水位、v35 生成中 Ask 的浏览器提交时间、v36
    # KG 质量分析的三张预计算产物表、v37 按 (source_id, element_type,
    # created_at, id) 的集合枚举索引、v38 用户可见来源身份索引，以及 v39
    # 命令目录抽取的 catalog_jobs / catalog_candidates 两张表，以及 v40
    # source-local fact / evidence-element binding 两张表，以及 v41 可恢复的
    # source-fact backfill 状态表，v42 notebook 级来源反查索引回填
    # 游标/计数账本，v43 报告公开分享的 share_token/shared_at 列，v44 生成问题
    # 影子索引，v45 user_profiles.ui_mode 界面模式偏好列，v46 element→chunk
    # 反查索引 chunk_elements / 其离线回填账本 chunk_element_backfills /
    # unified_kg_state.chunk_elements_indexed 标记列，v47 notebook schema
    # 覆盖表，v48 sources.agent_profile_id 出处列，v49 群组知识共享 P1
    # 的 groups / group_members / notebook_grants 三张表，v50 群组知识
    # 共享 P2 的 notebook_share_requests 表，v51 Agentic Memory P1 的
    # agent_notebook_profile / agent_profile_jobs 两张表，v52 问答会话
    # 公开分享 T1 的 conversations.share_token/shared_through_at/
    # shared_through_id 三列，v53 Agentic Memory P2 的
    # agent_profile_jobs.claim_token 巡固认领代际列，v54 Agentic Memory P2
    # 部署级全局的 retrieval_experiences 检索策略经验库，以及 v55 Agentic Memory
    # P3 的 agent_observations 观察日志表 / user_profiles.search_profile_json
    # 检索偏好列、v56 群组唯一 owner 指针、v57 群组邀请能力、v58 索引管线
    # desired/published identity、v59 未发布整本重建暂存表，v60 把
    # agent_observations 分成「Agent 自己写下的短句」与「调用记账」两种的
    # kind 列，以及 v61 热路径修复批 1 的五组索引（六组 PostgreSQL 侧对应
    # migrations/0039_hotpath_batch1_indexes.sql，第 6 组
    # chunks(source_id, ordinal) 在 SQLite 侧不适用，理由见
    # _migration_61 的 docstring），以及 v62 用户提问总览的创建者/时间排序
    # 索引，v63 部署插件运行时开关 + 审计的 extension_runtime_toggles 表，
    # 合法升级到当前版本。
    assert snapshot["schema"]["user_version"] == 63
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
