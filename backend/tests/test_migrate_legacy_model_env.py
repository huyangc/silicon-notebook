from __future__ import annotations

from pathlib import Path
import stat

from dotenv import dotenv_values
import pytest

from app.core.config import Settings
from app.services.model_registry import WORKLOADS, SystemModelServiceRegistry
from scripts import migrate_legacy_model_env as migration


def _legacy_env() -> str:
    return """# keep this deployment setting
DATABASE_URL=sqlite:///custom.db
OPENAI_COMPAT_BASE_URL=https://general.example/v1
OPENAI_COMPAT_API_KEY='general secret#1'
OPENAI_COMPAT_MODEL=general-model
REASONING_LLM_BASE_URL=https://reasoning.example/v1
REASONING_LLM_API_KEY=reasoning-secret
REASONING_LLM_MODEL=reasoning-model
REWRITE_LLM_BASE_URL=
REWRITE_LLM_API_KEY=
REWRITE_LLM_MODEL=rewrite-model
KG_LLM_BASE_URL=https://kg.example/v1
KG_LLM_API_KEY=kg-secret
KG_LLM_MODEL=kg-model
EMBED_PROVIDER=DashScope
EMBED_BASE_URL=https://embedding.example
EMBED_API_KEY=embedding-secret
EMBED_MODEL=embedding-model
RERANK_BASE_URL=https://dashscope.aliyuncs.com/api/v1
RERANK_API_KEY=rerank-secret
RERANK_MODEL=qwen3-rerank
RERANK_API_STYLE=dashscope
KG_EXTRACT_WORKERS=5
KG_ASK_RESERVE=7
EMBED_CONCURRENCY=3
KG_JOB_CONCURRENCY=9
"""


def test_build_plan_preserves_roles_bindings_protocols_and_inferred_capacity(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(_legacy_env(), encoding="utf-8")

    plan = migration.build_migration_plan(dotenv_values(env_path))
    services = {service.id: service for service in plan.services}

    assert set(services) == {
        "general", "reasoning", "rewrite", "kg", "embedding", "rerank"
    }
    assert services["general"].max_concurrency == 12
    assert services["reasoning"].max_concurrency == 12
    assert services["rewrite"].max_concurrency == 12
    assert services["kg"].max_concurrency == 5
    assert services["embedding"].max_concurrency == 3
    assert services["rerank"].max_concurrency == 3
    assert services["embedding"].protocol == "dashscope"
    assert services["rerank"].protocol == "dashscope"
    assert plan.bindings["ask_answer"] == "general"
    assert plan.bindings["query_rewrite"] == "rewrite"
    assert plan.bindings["report_sufficiency"] == "rewrite"
    assert plan.bindings["knowhow_optimize"] == "rewrite"
    assert plan.bindings["knowhow_reformat"] == "rewrite"
    assert plan.bindings["reasoning_agent"] == "reasoning"
    assert plan.bindings["report_section"] == "reasoning"
    assert plan.bindings["kg_extract"] == "kg"
    assert plan.bindings["knowledge_object_embedding"] == "embedding"
    assert plan.bindings["retrieval_rerank"] == "rerank"
    assert set(plan.bindings) == set(WORKLOADS)
    for secret in plan.secrets.values():
        assert secret not in plan.toml_text


def test_cli_is_dry_run_by_default_and_apply_backs_up_then_validates(tmp_path, capsys):
    env_path = tmp_path / ".env"
    original = _legacy_env()
    env_path.write_text(original, encoding="utf-8")
    output_path = tmp_path / "model-services.toml"
    argv = ["--env", str(env_path), "--output", str(output_path)]

    assert migration.main(argv) == 0
    dry_output = capsys.readouterr()
    assert not output_path.exists()
    assert env_path.read_text(encoding="utf-8") == original
    assert "dry-run" in dry_output.out
    assert "general secret#1" not in dry_output.out + dry_output.err

    assert migration.main([*argv, "--apply"]) == 0
    applied_output = capsys.readouterr()
    backup = tmp_path / ".env.pre-system-models.bak"
    assert backup.read_text(encoding="utf-8") == original
    assert output_path.is_file()
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(output_path.stat().st_mode) == 0o600
    assert "general secret#1" not in output_path.read_text(encoding="utf-8")
    assert "general secret#1" not in applied_output.out + applied_output.err

    migrated = dotenv_values(env_path)
    assert migrated["DATABASE_URL"] == "sqlite:///custom.db"
    assert migrated["KG_JOB_CONCURRENCY"] == "9"
    assert migrated["MODEL_SERVICES_CONFIG"] == str(output_path)
    assert migrated["SYSTEM_GENERAL_API_KEY"] == "general secret#1"
    for key in migration.LEGACY_ACTIVATION_KEYS | migration.LEGACY_CAPACITY_KEYS:
        assert key not in migrated

    registry = SystemModelServiceRegistry.load(
        Settings(_env_file=None, model_services_config=str(output_path)),
        {key: value for key, value in migrated.items() if value is not None},
    )
    assert registry.service_for("retrieval_rerank").protocol == "dashscope"
    assert all(registry.service_for(workload_id) is not None for workload_id in WORKLOADS)


def test_deduplicates_one_physical_service_and_keeps_legacy_fallbacks():
    values = {
        "OPENAI_COMPAT_BASE_URL": "https://shared.example/v1",
        "OPENAI_COMPAT_API_KEY": "shared-secret",
        "OPENAI_COMPAT_MODEL": "shared-model",
        "REASONING_LLM_BASE_URL": "https://shared.example/v1/",
        "REASONING_LLM_API_KEY": "shared-secret",
        "REASONING_LLM_MODEL": "shared-model",
        "KG_LLM_BASE_URL": "https://partial.example/v1",
    }

    plan = migration.build_migration_plan(values)

    assert [service.id for service in plan.services] == ["general"]
    assert plan.services[0].roles == ("general", "reasoning")
    assert plan.bindings["reasoning_agent"] == "general"
    assert plan.bindings["kg_extract"] == "general"
    assert any("KG_LLM" in warning for warning in plan.warnings)


def test_dashscope_rerank_keeps_the_legacy_default_base_url():
    plan = migration.build_migration_plan({
        "RERANK_API_KEY": "rerank-secret",
        "RERANK_MODEL": "qwen3-rerank",
    })

    assert len(plan.services) == 1
    assert plan.services[0].protocol == "dashscope"
    assert plan.services[0].base_url == "https://dashscope.aliyuncs.com/api/v1"
    assert plan.bindings == {"retrieval_rerank": "rerank"}


@pytest.mark.parametrize(
    ("style", "protocol"),
    [("openai", "openai"), ("unexpected-old-value", "dashscope")],
)
def test_rerank_preserves_legacy_default_base_and_style_normalization(
    style, protocol
):
    plan = migration.build_migration_plan({
        "RERANK_API_KEY": "rerank-secret",
        "RERANK_MODEL": "qwen3-rerank",
        "RERANK_API_STYLE": style,
    })

    assert plan.services[0].protocol == protocol
    assert plan.services[0].base_url == "https://dashscope.aliyuncs.com/api/v1"
    if style == "unexpected-old-value":
        assert any("matching the retired runtime" in item for item in plan.warnings)


def test_same_physical_service_rejects_conflicting_explicit_capacity():
    values = {
        "OPENAI_COMPAT_BASE_URL": "https://shared.example/v1",
        "OPENAI_COMPAT_API_KEY": "shared-secret",
        "OPENAI_COMPAT_MODEL": "shared-model",
        "REASONING_LLM_BASE_URL": "https://shared.example/v1",
        "REASONING_LLM_API_KEY": "shared-secret",
        "REASONING_LLM_MODEL": "shared-model",
    }

    with pytest.raises(migration.MigrationError, match="same physical service"):
        migration.build_migration_plan(
            values, {"general": 4, "reasoning": 2}
        )


def test_force_replacement_backs_up_existing_toml(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(_legacy_env(), encoding="utf-8")
    output_path = tmp_path / "model-services.toml"
    output_path.write_text("old-config\n", encoding="utf-8")

    assert migration.main([
        "--env", str(env_path), "--output", str(output_path), "--apply"
    ]) == 2
    assert output_path.read_text(encoding="utf-8") == "old-config\n"

    assert migration.main([
        "--env", str(env_path), "--output", str(output_path),
        "--apply", "--force",
    ]) == 0
    backup = tmp_path / "model-services.toml.pre-system-models.bak"
    assert backup.read_text(encoding="utf-8") == "old-config\n"
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_apply_replaces_an_unmodified_packaged_example_without_force(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(_legacy_env(), encoding="utf-8")
    output_path = tmp_path / "model-services.toml"
    example = Path(migration.ROOT) / "model-services.example.toml"
    output_path.write_bytes(example.read_bytes())

    assert migration.main([
        "--env", str(env_path), "--output", str(output_path), "--apply",
    ]) == 0

    backup = tmp_path / "model-services.toml.pre-system-models.bak"
    assert backup.read_bytes() == example.read_bytes()
    assert output_path.read_text(encoding="utf-8").startswith(
        "# Generated by scripts/migrate_legacy_model_env.py."
    )


def test_capacity_override_parser_and_no_complete_service_errors():
    assert migration.parse_capacity_overrides([
        "general=20", "embedding=4"
    ]) == {"general": 20, "embedding": 4}
    with pytest.raises(migration.MigrationError, match="ROLE=N"):
        migration.parse_capacity_overrides(["unknown=2"])
    with pytest.raises(migration.MigrationError, match="no complete"):
        migration.build_migration_plan({"OPENAI_COMPAT_MODEL": "partial"})
