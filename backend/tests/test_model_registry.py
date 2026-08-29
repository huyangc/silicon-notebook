"""Strict startup configuration for system-owned model services."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings
from app.services.model_registry import (
    WORKLOADS,
    SystemModelServiceRegistry,
)


_EXPECTED_WORKLOADS = {
    "ask_answer": ("chat", "interactive", "问答回答"),
    "plugin_engine": ("chat", "interactive", "扩展问答引擎"),
    "reasoning_agent": ("chat", "interactive", "逐步推理"),
    "query_rewrite": ("chat", "interactive", "查询改写"),
    "evidence_refine": ("chat", "interactive", "证据筛选"),
    "report_outline": ("chat", "report", "报告提纲"),
    "report_sufficiency": ("chat", "report", "报告充分性判断"),
    "report_section": ("chat", "report", "报告章节生成"),
    "report_summary": ("chat", "report", "报告摘要"),
    "source_summary": ("chat", "background", "来源摘要"),
    "notebook_metadata": ("chat", "background", "笔记本信息提取"),
    "paper_metadata": ("chat", "background", "论文元数据提取"),
    "chunk_question_generation": ("chat", "background", "分块问题生成"),
    "kg_extract": ("chat", "background", "知识抽取"),
    "kg_refine": ("chat", "background", "知识细化"),
    "kg_glean": ("chat", "background", "知识补充抽取"),
    "kg_merge_review": ("chat", "background", "知识合并审核"),
    "kg_concept_description": ("chat", "background", "概念描述生成"),
    "kg_community_summary": ("chat", "background", "知识社区摘要"),
    "kg_conflict_review": ("chat", "background", "知识冲突审核"),
    "schema_induction": ("chat", "interactive", "知识类型归纳"),
    "memory_preview": ("chat", "interactive", "记忆预览"),
    "knowhow_optimize": ("chat", "interactive", "经验表述优化"),
    "knowhow_reformat": ("chat", "interactive", "经验格式整理"),
    "knowhow_complete": ("chat", "interactive", "经验空列补全"),
    "agent_profile_consolidate": ("chat", "background", "库理解整理"),
    "retrieval_experience_distill": ("chat", "background", "检索打法总结"),
    "retrieval_query_embedding": ("embedding", "interactive", "检索查询向量"),
    "source_element_embedding": ("embedding", "background", "来源元素向量"),
    "chunk_embedding": ("embedding", "background", "来源分块向量"),
    "knowledge_object_embedding": ("embedding", "background", "知识对象向量"),
    "relation_embedding": ("embedding", "background", "知识关系向量"),
    "memory_embedding": ("embedding", "interactive", "记忆向量"),
    "knowhow_embedding": ("embedding", "background", "经验表格向量"),
    "retrieval_rerank": ("rerank", "interactive", "检索结果重排"),
}


def _write_config(path: Path, body: str) -> Path:
    path.write_text(body.strip() + "\n", encoding="utf-8")
    return path


def _service(
    service_id: str = "general",
    *,
    kind: str = "chat",
    protocol: str = "openai",
    base_url: str = "https://llm.example/v1",
    model: str = "general-model",
    api_key_env: str = "GENERAL_KEY",
    max_concurrency: int = 2,
    extra: str = "",
) -> str:
    return f'''[services.{service_id}]
display_name = "通用模型"
kind = "{kind}"
protocol = "{protocol}"
base_url = "{base_url}"
model = "{model}"
api_key_env = "{api_key_env}"
max_concurrency = {max_concurrency}
{extra}'''


def _settings(path: Path | str = "") -> Settings:
    return Settings(_env_file=None, model_services_config=str(path))


def test_workload_catalog_is_exact_and_has_fixed_chinese_labels():
    assert {
        key: (value.kind, value.default_priority, value.display_label)
        for key, value in WORKLOADS.items()
    } == _EXPECTED_WORKLOADS


def test_chat_workloads_make_an_exhaustive_thinking_default_choice():
    enabled = {
        "ask_answer",
        "reasoning_agent",
        "report_outline",
        "report_sufficiency",
        "schema_induction",
        "agent_profile_consolidate",
        "retrieval_experience_distill",
    }
    chat_workloads = {
        key for key, workload in WORKLOADS.items() if workload.kind == "chat"
    }

    assert {
        key
        for key in chat_workloads
        if WORKLOADS[key].default_thinking_mode == "enabled"
    } == enabled
    assert all(
        WORKLOADS[key].default_thinking_mode == "disabled"
        for key in chat_workloads - enabled
    )


def test_empty_config_path_keeps_deterministic_offline_mode():
    registry = SystemModelServiceRegistry.load(_settings(), {})

    assert registry.service_for("ask_answer") is None
    assert registry.workload("ask_answer") is WORKLOADS["ask_answer"]
    assert registry.workloads_for("missing") == ()


def test_registry_binds_one_physical_service_to_many_workloads(tmp_path):
    path = _write_config(
        tmp_path / "models.toml",
        _service() + '''
[bindings]
ask_answer = "general"
source_summary = "general"''',
    )
    registry = SystemModelServiceRegistry.load(_settings(path), {"GENERAL_KEY": "secret"})

    assert registry.service_for("ask_answer") is registry.service_for("source_summary")
    assert registry.service("general").max_concurrency == 2
    assert registry.workloads_for("general") == (
        WORKLOADS["ask_answer"],
        WORKLOADS["source_summary"],
    )


def test_registry_accepts_per_workload_thinking_overrides(tmp_path):
    path = _write_config(
        tmp_path / "models.toml",
        _service() + '''
[bindings]
ask_answer = "general"
[thinking]
ask_answer = "disabled"
report_outline = "provider_default"''',
    )
    registry = SystemModelServiceRegistry.load(
        _settings(path), {"GENERAL_KEY": "secret"}
    )

    assert registry.thinking_mode_for("ask_answer") == "disabled"
    assert (
        registry.thinking_mode_for("report_outline")
        == "provider_default"
    )
    assert registry.thinking_mode_for("source_summary") == "disabled"


def test_registry_drops_retired_workload_entries_instead_of_failing(tmp_path):
    """升级兼容:按旧示例生成的部署配置里还留着已退役的 graph_chain_verify
    绑定/思考模式(graph 模式退役前它是合法词表),load() 必须接受并丢弃,
    而不是当未知 workload 拒绝——退役能力不许把既有部署的启动搞挂。
    退役条目甚至可以指向一个已不存在的 service:丢弃发生在一切校验之前。"""
    path = _write_config(
        tmp_path / "models.toml",
        _service() + '''
[bindings]
ask_answer = "general"
graph_chain_verify = "general"
[thinking]
graph_chain_verify = "enabled"''',
    )
    registry = SystemModelServiceRegistry.load(
        _settings(path), {"GENERAL_KEY": "secret"}
    )

    assert registry.service_for("ask_answer") is not None
    assert registry.service_for("graph_chain_verify") is None
    assert registry.workloads_for("general") == (WORKLOADS["ask_answer"],)

    stale_service = _write_config(
        tmp_path / "stale.toml",
        _service() + '''
[bindings]
graph_chain_verify = "service_that_no_longer_exists"''',
    )
    SystemModelServiceRegistry.load(_settings(stale_service), {"GENERAL_KEY": "secret"})


@pytest.mark.parametrize(
    ("thinking", "match"),
    [
        ('not_a_workload = "disabled"', "not_a_workload"),
        ('retrieval_query_embedding = "disabled"', "only valid for chat"),
        ('ask_answer = "high"', "invalid thinking mode"),
    ],
)
def test_registry_rejects_invalid_thinking_configuration(
    tmp_path, thinking, match
):
    path = _write_config(
        tmp_path / "models.toml",
        _service() + "\n[thinking]\n" + thinking,
    )

    with pytest.raises(ValueError, match=match):
        SystemModelServiceRegistry.load(
            _settings(path), {"GENERAL_KEY": "secret"}
        )


def test_chat_service_accepts_fixed_top_p_and_fingerprints_it(tmp_path):
    first_path = _write_config(
        tmp_path / "first.toml",
        _service(extra="top_p = 0.95") + '\n[bindings]\nask_answer = "general"',
    )
    second_path = _write_config(
        tmp_path / "second.toml",
        _service(extra="top_p = 0.9") + '\n[bindings]\nask_answer = "general"',
    )

    first = SystemModelServiceRegistry.load(
        _settings(first_path), {"GENERAL_KEY": "secret"}
    ).service("general")
    second = SystemModelServiceRegistry.load(
        _settings(second_path), {"GENERAL_KEY": "secret"}
    ).service("general")

    assert first.top_p == 0.95
    assert first.fingerprint != second.fingerprint


@pytest.mark.parametrize(
    "service",
    [
        _service(extra="top_p = 1.1"),
        _service(extra='top_p = "0.95"'),
        _service(kind="embedding", extra="top_p = 0.95"),
    ],
)
def test_registry_rejects_invalid_or_non_chat_top_p(tmp_path, service):
    path = _write_config(tmp_path / "models.toml", service)

    with pytest.raises(ValueError, match="top_p"):
        SystemModelServiceRegistry.load(_settings(path), {"GENERAL_KEY": "secret"})


def test_registry_accepts_legacy_dashscope_rerank_protocol(tmp_path):
    path = _write_config(
        tmp_path / "models.toml",
        _service(
            service_id="rerank",
            kind="rerank",
            protocol="dashscope",
            api_key_env="RERANK_KEY",
        ) + '\n[bindings]\nretrieval_rerank = "rerank"',
    )

    registry = SystemModelServiceRegistry.load(
        _settings(path), {"RERANK_KEY": "secret"}
    )

    assert registry.service_for("retrieval_rerank").protocol == "dashscope"


def test_checked_in_example_is_credential_free_and_loads_when_keys_are_supplied():
    root = Path(__file__).resolve().parents[2]
    template = root / "model-services.example.toml"
    contents = template.read_text(encoding="utf-8")
    registry = SystemModelServiceRegistry.load(
        _settings(template),
        {
            "SYSTEM_GENERAL_API_KEY": "general-secret",
            "SYSTEM_REASONING_API_KEY": "reasoning-secret",
            "SYSTEM_EMBEDDING_API_KEY": "embedding-secret",
            "SYSTEM_RERANK_API_KEY": "rerank-secret",
        },
    )

    assert "general-secret" not in contents
    assert registry.service_for("ask_answer").id == "general"
    assert registry.service_for("plugin_engine").id == "general"
    assert registry.thinking_mode_for("plugin_engine") == "disabled"
    assert registry.service_for("retrieval_query_embedding").id == "embedding"
    assert registry.service_for("retrieval_rerank").id == "rerank"
    assert all(registry.service_for(workload_id) is not None for workload_id in WORKLOADS)


def test_resolves_secret_from_supplied_environment_without_exposing_it(tmp_path):
    path = _write_config(tmp_path / "models.toml", _service())

    registry = SystemModelServiceRegistry.load(_settings(path), {"GENERAL_KEY": "a-secret"})

    assert registry.service("general").api_key == "a-secret"
    assert "a-secret" not in repr(registry.service("general"))


def test_relative_config_path_is_anchored_to_repository_root(monkeypatch, tmp_path):
    import app.core.config as config_module

    _write_config(tmp_path / "models.toml", _service())
    monkeypatch.setattr(config_module, "_ROOT_DIR", tmp_path)

    registry = SystemModelServiceRegistry.load(_settings("models.toml"), {"GENERAL_KEY": "secret"})

    assert registry.service("general").base_url == "https://llm.example/v1"


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (_service(extra="unexpected = true"), "unexpected"),
        (_service(service_id="General"), "service id"),
        (_service(kind="unsupported"), "kind"),
        (_service(protocol="dashscope"), "protocol"),
        (_service(max_concurrency=0), "max_concurrency"),
    ],
)
def test_rejects_invalid_service_definitions(tmp_path, body, match):
    path = _write_config(tmp_path / "models.toml", body)

    with pytest.raises(ValueError, match=match):
        SystemModelServiceRegistry.load(_settings(path), {"GENERAL_KEY": "secret"})


def test_rejects_unknown_top_level_toml_key(tmp_path):
    path = _write_config(tmp_path / "models.toml", _service() + "\n[unknown]\na = 1")

    with pytest.raises(ValueError, match="unknown"):
        SystemModelServiceRegistry.load(_settings(path), {"GENERAL_KEY": "secret"})


def test_missing_secret_names_only_the_environment_variable(tmp_path):
    path = _write_config(tmp_path / "models.toml", _service())

    with pytest.raises(ValueError) as exc_info:
        SystemModelServiceRegistry.load(_settings(path), {})

    assert "GENERAL_KEY" in str(exc_info.value)
    assert "secret" not in str(exc_info.value).lower()


@pytest.mark.parametrize(
    ("bindings", "match"),
    [
        ('ask_answer = "missing"', "missing"),
        ('ask_answer = "embedding"', "kind"),
        ('not_a_workload = "general"', "not_a_workload"),
    ],
)
def test_rejects_unknown_or_mismatched_bindings(tmp_path, bindings, match):
    embedding = _service(
        service_id="embedding",
        kind="embedding",
        protocol="dashscope",
        api_key_env="EMBED_KEY",
    )
    path = _write_config(
        tmp_path / "models.toml",
        _service() + "\n" + embedding + "\n[bindings]\n" + bindings,
    )

    with pytest.raises(ValueError, match=match):
        SystemModelServiceRegistry.load(
            _settings(path), {"GENERAL_KEY": "general", "EMBED_KEY": "embed"}
        )


def test_rejects_duplicate_physical_service_definitions_without_leaking_key(tmp_path):
    path = _write_config(
        tmp_path / "models.toml",
        _service() + "\n" + _service(service_id="duplicate"),
    )

    with pytest.raises(ValueError) as exc_info:
        SystemModelServiceRegistry.load(_settings(path), {"GENERAL_KEY": "duplicate-secret"})

    assert "duplicate" in str(exc_info.value)
    assert "duplicate-secret" not in str(exc_info.value)


@pytest.mark.parametrize(
    ("changed", "value"),
    [
        ("base_url", "https://other.example/v1"),
        ("model", "other-model"),
        ("api_key", "other-secret"),
        ("max_concurrency", "3"),
    ],
)
def test_fingerprint_changes_when_identity_material_changes(tmp_path, changed, value):
    path = _write_config(tmp_path / "models.toml", _service())
    baseline = SystemModelServiceRegistry.load(_settings(path), {"GENERAL_KEY": "secret"})
    if changed == "api_key":
        alternate = SystemModelServiceRegistry.load(_settings(path), {"GENERAL_KEY": value})
    else:
        changed_service = _service(**{changed: int(value) if changed == "max_concurrency" else value})
        alternate_path = _write_config(tmp_path / "alternate.toml", changed_service)
        alternate = SystemModelServiceRegistry.load(_settings(alternate_path), {"GENERAL_KEY": "secret"})

    assert alternate.service("general").fingerprint != baseline.service("general").fingerprint


@pytest.mark.parametrize(
    "legacy_var",
    [
        "OPENAI_COMPAT_BASE_URL",
        "OPENAI_COMPAT_API_KEY",
        "OPENAI_COMPAT_MODEL",
        "REASONING_LLM_BASE_URL",
        "REASONING_LLM_API_KEY",
        "REASONING_LLM_MODEL",
        "REWRITE_LLM_BASE_URL",
        "REWRITE_LLM_API_KEY",
        "REWRITE_LLM_MODEL",
        "KG_LLM_BASE_URL",
        "KG_LLM_API_KEY",
        "KG_LLM_MODEL",
        "EMBED_PROVIDER",
        "EMBED_BASE_URL",
        "EMBED_API_KEY",
        "EMBED_MODEL",
        "RERANK_BASE_URL",
        "RERANK_API_KEY",
        "RERANK_MODEL",
        "RERANK_API_STYLE",
    ],
)
def test_empty_config_rejects_real_legacy_endpoint_without_leaking_value(monkeypatch, legacy_var):
    monkeypatch.setenv(legacy_var, "https://legacy.example/secret")

    with pytest.raises(ValueError) as exc_info:
        SystemModelServiceRegistry.load(_settings())

    assert legacy_var in str(exc_info.value)
    assert "https://legacy.example/secret" not in str(exc_info.value)


def test_empty_config_rejects_legacy_rerank_identity_without_base_url(monkeypatch):
    monkeypatch.setenv("RERANK_MODEL", "legacy-rerank-model")
    monkeypatch.setenv("RERANK_API_KEY", "rerank-secret")
    monkeypatch.delenv("RERANK_BASE_URL", raising=False)

    with pytest.raises(ValueError) as exc_info:
        SystemModelServiceRegistry.load(_settings())

    error = str(exc_info.value)
    assert "RERANK_MODEL" in error
    assert "legacy-rerank-model" not in error
    assert "rerank-secret" not in error


def test_empty_process_variable_suppresses_stale_legacy_value_from_active_env_file(
    monkeypatch, tmp_path
):
    env_file = _write_config(
        tmp_path / ".env",
        "OPENAI_COMPAT_BASE_URL=https://stale.example/secret",
    )
    monkeypatch.setitem(Settings.model_config, "env_file", env_file)
    settings = Settings(_env_file=env_file, model_services_config="")

    registry = SystemModelServiceRegistry.load(settings, {"OPENAI_COMPAT_BASE_URL": ""})

    assert registry.service_for("ask_answer") is None


def test_legacy_value_from_active_env_file_is_detected_without_leaking_it(monkeypatch, tmp_path):
    env_file = _write_config(
        tmp_path / ".env",
        "OPENAI_COMPAT_BASE_URL=https://stale.example/secret",
    )
    monkeypatch.setitem(Settings.model_config, "env_file", env_file)
    settings = Settings(_env_file=env_file, model_services_config="")

    with pytest.raises(ValueError) as exc_info:
        SystemModelServiceRegistry.load(settings, {})

    assert "OPENAI_COMPAT_BASE_URL" in str(exc_info.value)
    assert "https://stale.example/secret" not in str(exc_info.value)
