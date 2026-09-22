"""Strict startup configuration for system-owned model services."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings
from app.services.model_registry import (
    BINDING_GAP_PREFIX,
    WORKLOADS,
    ModelBindingGapError,
    SystemModelServiceRegistry,
    describe_binding_gap,
    loggable_binding_gap,
)


_EXPECTED_WORKLOADS = {
    "ask_answer": ("chat", "interactive", "问答回答"),
    "plugin_engine": ("chat", "interactive", "扩展问答引擎"),
    "reasoning_agent": ("chat", "interactive", "逐步推理"),
    "query_rewrite": ("chat", "interactive", "查询改写"),
    "gap_consult_query": ("chat", "interactive", "站外来源检索词规划"),
    "external_evidence_answer": ("chat", "interactive", "站外摘要补充"),
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


def _settings(path: Path | str = "", *, strict: bool = False) -> Settings:
    """Settings for a load() test, with the startup binding gate OFF by default.

    Product default is ``MODEL_BINDINGS_STRICT=true``: a non-empty config whose
    ``[bindings]`` misses a workload (or names an id that is not one) refuses to
    start. Nearly every test below is about **parsing** semantics — service
    shapes, secrets, fingerprints, thinking modes — and states its case with a
    two-line binding table on purpose. Those cases opt out so they keep testing
    the one rule they are named after instead of failing on completeness.

    The completeness rule has its own tests, which pass ``strict=True``
    explicitly; the checked-in example and the migration script are guarded
    there.
    """
    return Settings(
        _env_file=None,
        model_services_config=str(path),
        model_bindings_strict=strict,
    )


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


def test_chat_workloads_have_an_exhaustive_analysis_area():
    expected = {
        "ask": {
            "ask_answer",
            "plugin_engine",
            "reasoning_agent",
            "query_rewrite",
            "gap_consult_query",
            "external_evidence_answer",
            "evidence_refine",
        },
        "report": {
            "report_outline",
            "report_sufficiency",
            "report_section",
            "report_summary",
        },
        "source": {
            "source_summary",
            "notebook_metadata",
            "paper_metadata",
            "chunk_question_generation",
        },
        "knowledge": {
            "kg_extract",
            "kg_refine",
            "kg_glean",
            "kg_merge_review",
            "kg_concept_description",
            "kg_community_summary",
            "kg_conflict_review",
            "schema_induction",
        },
        "memory": {"memory_preview", "agent_profile_consolidate"},
        "knowhow": {"knowhow_optimize", "knowhow_reformat", "knowhow_complete"},
        "retrieval": {"retrieval_experience_distill"},
    }

    actual = {
        area: {
            key
            for key, workload in WORKLOADS.items()
            if workload.kind == "chat" and workload.analysis_area == area
        }
        for area in expected
    }
    assert actual == expected


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
    """放行档(MODEL_BINDINGS_STRICT=false)的升级兼容行为。

    按旧示例生成的部署配置里还留着已退役的 graph_chain_verify 绑定/思考模式
    (graph 模式退役前它是合法词表)。关掉严格闸之后 load() 接受并丢弃它们,而
    不是当未知 workload 拒绝;退役条目甚至可以指向一个已不存在的 service——丢弃
    发生在一切校验之前。

    严格档(产品默认)反过来点名它们,见
    ``test_strict_load_names_stale_ids_including_retired_ones``:用户裁决要求
    「多配置了哪些」也报出来,这条兼容路径因此只在显式放行时存在。"""
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
    ("thinking", "match", "strict"),
    [
        # 未知 id 现在由严格闸的合并诊断点名(与 [bindings] 里的未知 id 同一条
        # 消息),放行档只丢弃它;其余两条是 [thinking] 自己的形状校验,与闸无关。
        ('not_a_workload = "disabled"', "not_a_workload", True),
        ('retrieval_query_embedding = "disabled"', "only valid for chat", False),
        ('ask_answer = "high"', "invalid thinking mode", False),
    ],
)
def test_registry_rejects_invalid_thinking_configuration(
    tmp_path, thinking, match, strict
):
    path = _write_config(
        tmp_path / "models.toml",
        _service() + "\n[thinking]\n" + thinking,
    )

    with pytest.raises(ValueError, match=match):
        SystemModelServiceRegistry.load(
            _settings(path, strict=strict), {"GENERAL_KEY": "secret"}
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
    """守卫:示例配置必须过得了产品默认的严格闸(strict=True)。

    新增一个 workload 却忘了在 example 里补绑定,这条会红——那正是我们要阻止的
    事故形态:部署照着 example 生成配置,新工作负载天生未绑定、静默降级。
    """
    root = Path(__file__).resolve().parents[2]
    template = root / "model-services.example.toml"
    contents = template.read_text(encoding="utf-8")
    registry = SystemModelServiceRegistry.load(
        _settings(template, strict=True),
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
    ("bindings", "match", "strict"),
    [
        ('ask_answer = "missing"', "missing", False),
        ('ask_answer = "embedding"', "kind", False),
        # 不是 workload 的 id 不再就地抛:它进合并诊断,由严格闸一次报全。
        ('not_a_workload = "general"', "not_a_workload", True),
    ],
)
def test_rejects_unknown_or_mismatched_bindings(tmp_path, bindings, match, strict):
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
            _settings(path, strict=strict),
            {"GENERAL_KEY": "general", "EMBED_KEY": "embed"},
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


# ---------------------------------------------------------------------------
# PR-4:模型绑定启动期硬校验(MODEL_BINDINGS_STRICT,默认 true)
#
# 用户裁决:「不配模型会静默降级,影响最终用户的使用。应该在服务启动的时候 check
# 模型配置文件,如果有没有配置的,则直接失败并报错(哪些没配置,或者多配置了哪些)。」
# ---------------------------------------------------------------------------


_KIND_SERVICES = {"chat": "general", "embedding": "embedding", "rerank": "rerank"}


def _all_services() -> str:
    return "\n".join((
        _service(),
        _service(
            service_id="embedding",
            kind="embedding",
            protocol="dashscope",
            model="embedding-model",
            api_key_env="EMBED_KEY",
        ),
        _service(
            service_id="rerank",
            kind="rerank",
            protocol="dashscope",
            model="rerank-model",
            api_key_env="RERANK_KEY",
        ),
    ))


_ALL_KEYS = {"GENERAL_KEY": "g", "EMBED_KEY": "e", "RERANK_KEY": "r"}


def _bindings(*, omit: frozenset[str] | set[str] = frozenset(), extra: str = "") -> str:
    lines = ["[bindings]"]
    for workload_id, workload in sorted(WORKLOADS.items()):
        if workload_id in omit:
            continue
        lines.append(f'{workload_id} = "{_KIND_SERVICES[workload.kind]}"')
    return "\n".join(lines) + extra


def test_strict_load_accepts_a_complete_binding_table(tmp_path):
    path = _write_config(
        tmp_path / "models.toml", _all_services() + "\n" + _bindings()
    )

    registry = SystemModelServiceRegistry.load(
        _settings(path, strict=True), _ALL_KEYS
    )

    assert all(registry.service_for(workload_id) for workload_id in WORKLOADS)
    assert registry.unknown_bindings() == ()


def test_strict_load_names_every_unbound_workload_of_every_kind(tmp_path):
    """三个种类各缺一个:消息按 id 字典序列全,带中文标签、配置路径与放行开关。"""
    omitted = {"agent_profile_consolidate", "chunk_embedding", "retrieval_rerank"}
    path = _write_config(
        tmp_path / "models.toml", _all_services() + "\n" + _bindings(omit=omitted)
    )

    with pytest.raises(ValueError) as exc_info:
        SystemModelServiceRegistry.load(_settings(path, strict=True), _ALL_KEYS)

    message = str(exc_info.value)
    assert message.startswith("model-bindings: 缺少绑定的工作负载：")
    assert (
        "库理解整理（agent_profile_consolidate），来源分块向量（chunk_embedding），"
        "检索结果重排（retrieval_rerank）" in message
    )
    assert "已退役" not in message and "拼写错误" not in message
    assert str(path) in message
    assert "MODEL_BINDINGS_STRICT=false" in message
    # 诊断只带 id 与标签:密钥与 endpoint 都不出现。
    assert "GENERAL_KEY" not in message and "llm.example" not in message
    assert message.count("\n") == 0


def test_strict_load_names_stale_ids_including_retired_ones(tmp_path):
    """已退役与拼错的 id 分两段、各自带落点,因为运维要做的事相反。

    「已退役」= 你的文件早于这次升级,删掉那一行;「未知」= 多半是拼错了,还得
    自己找。落点([bindings] / [thinking] / 两者)必须写出来:只出现在 [thinking]
    的陈旧 id 在 [bindings] 里翻遍了也找不到。

    放行档仍然丢弃它们(见
    ``test_registry_drops_retired_workload_entries_instead_of_failing``);默认
    档点名,因为一个指向不存在工作负载的绑定就是运维以为配了、实际没配。
    """
    path = _write_config(
        tmp_path / "models.toml",
        _all_services()
        + "\n"
        + _bindings(
            extra='\ngraph_chain_verify = "general"\nask_anwser = "general"\n'
        )
        + '\n[thinking]\ngraph_chain_verify = "enabled"\n'
        'stale_thinking_id = "enabled"\n',
    )

    with pytest.raises(ValueError) as exc_info:
        SystemModelServiceRegistry.load(_settings(path, strict=True), _ALL_KEYS)

    message = str(exc_info.value)
    assert "缺少绑定的工作负载" not in message
    # 退役 id 在两张表里都出现过,落点合并成一条。
    assert (
        "已退役、升级后请删除的绑定：graph_chain_verify（[bindings]/[thinking]）"
        in message
    )
    assert (
        "未知、疑似拼写错误的绑定：ask_anwser（[bindings]），"
        "stale_thinking_id（[thinking]）" in message
    )
    assert "的 [bindings]/[thinking] 补齐/删除后重启" in message


def test_strict_load_reports_both_halves_in_one_message(tmp_path):
    """缺与多同时存在时一条消息两段都在,运维一次改完而不是逐次重启试错。"""
    path = _write_config(
        tmp_path / "models.toml",
        _all_services()
        + "\n"
        + _bindings(
            omit={"kg_glean"}, extra='\ngraph_chain_verify = "general"\n'
        ),
    )

    with pytest.raises(ValueError) as exc_info:
        SystemModelServiceRegistry.load(_settings(path, strict=True), _ALL_KEYS)

    message = str(exc_info.value)
    assert "知识补充抽取（kg_glean）" in message
    assert "已退役、升级后请删除的绑定：graph_chain_verify（[bindings]）" in message
    assert message.index("缺少绑定") < message.index("已退役")


def test_empty_config_stays_offline_mode_even_under_the_strict_default():
    """空 MODEL_SERVICES_CONFIG 是受支持的离线模式,严格闸对它没有意见。

    `scripts/check.sh` 的每条泳道都跑在这个形态下;把它也判成「缺绑定」会把整
    仓门禁和所有开发者的本机启动一起搞挂。
    """
    registry = SystemModelServiceRegistry.load(_settings(strict=True), {})

    assert registry.services() == ()
    assert registry.service_for("ask_answer") is None
    assert registry.unknown_bindings() == ()


def test_non_strict_load_keeps_the_stale_ids_for_the_startup_warning(tmp_path):
    """放行档不抛,但要把被忽略的 id 留给启动告警点名——丢弃不等于无声。"""
    path = _write_config(
        tmp_path / "models.toml",
        _all_services()
        + "\n"
        + _bindings(extra='\ngraph_chain_verify = "general"\n'),
    )

    registry = SystemModelServiceRegistry.load(_settings(path), _ALL_KEYS)

    assert registry.unknown_bindings() == ("graph_chain_verify",)
    assert registry.service_for("ask_answer") is not None


def test_the_deployment_docs_quote_the_refusal_message_verbatim():
    """zh/en 两份部署文档里的样例必须与**运维实际看到的那一行**逐字相同。

    钉的是 `loggable_binding_gap`,不是 `describe_binding_gap`:后端以
    `uvicorn … >>"$BACKEND_LOG" 2>&1` 启动,stderr 就是日志文件,所以 main 的预检
    不让带路径的 `ModelBindingGapError` 逃出去,而是 `SystemExit(exc.loggable())`
    ——日志与 stderr 拿到的是同一行脱敏文本。文档要引的就是那一行;贴带路径的
    版本会让运维照着一个他根本看不到的形态去对。

    这条消息是运维遇到拒启时唯一的指引,文档抄错一个字(少一段、连接符不同、
    落点标注漏了)就会把人引到错误的表上去改。样例拿 `==` 比,不是 `in`。
    """
    produced = loggable_binding_gap(
        ["agent_profile_consolidate", "retrieval_experience_distill"],
        {
            "graph_chain_verify": {"[bindings]", "[thinking]"},
            "ask_anwser": {"[bindings]"},
        },
    )
    root = Path(__file__).resolve().parents[2] / "docs"
    for name in (
        "deployment-and-configuration_zh.md",
        "deployment-and-configuration.md",
    ):
        quoted = [
            line
            for line in (root / name).read_text(encoding="utf-8").splitlines()
            if line.startswith(BINDING_GAP_PREFIX)
        ]
        assert quoted == [produced], name


def test_the_log_rendering_drops_the_path_and_any_key_it_cannot_vouch_for():
    """日志那一面:无路径,且来自配置文件的键名必须过形状校验才原样出现。

    AGENTS.md 禁止 private path 与异常文本进日志。工作负载 id 与中文标签是代码
    里的封闭词表,照记;陈旧 id 是部署文件里的任意 TOML 键,只有长得像 workload
    id 才可信——否则一个构造出来的键就能往日志里塞进路径、标点甚至换行。
    """
    unknown = {
        "graph_chain_verify": {"[bindings]"},
        "ask_anwser": {"[bindings]"},
        "/etc/silicon/secret.toml\nERROR fake": {"[thinking]"},
        "Has Spaces": {"[thinking]"},
    }
    logged = loggable_binding_gap(["kg_glean"], unknown)

    assert logged.startswith(BINDING_GAP_PREFIX)
    assert "知识补充抽取（kg_glean）" in logged
    assert "已退役、升级后请删除的绑定：graph_chain_verify（[bindings]）" in logged
    assert (
        "未知、疑似拼写错误的绑定：ask_anwser（[bindings]），"
        "<无法显示的键名>×2（[thinking]）" in logged
    )
    assert "请在 MODEL_SERVICES_CONFIG 指向的文件的 [bindings]/[thinking] " in logged
    assert "/etc/silicon" not in logged
    assert "ERROR fake" not in logged
    assert "Has Spaces" not in logged
    assert "\n" not in logged


def test_the_exception_rendering_keeps_the_path_and_the_file_s_own_spelling():
    """异常那一面反过来:它要带路径、要原样说出文件里写的是什么。

    终止进程的异常不是日志,运维正是要靠它去改那个文件——把键名折叠掉,人就
    找不到那一行了。两面的差别由 `main._model_bindings_preflight` 落实。
    """
    unknown = {"Has Spaces": {"[thinking]"}}
    raised = describe_binding_gap(
        ["kg_glean"], unknown, "/etc/silicon/model-services.toml"
    )

    assert "Has Spaces（[thinking]）" in raised
    assert "/etc/silicon/model-services.toml" in raised
    assert "无法显示的键名" not in raised


def test_the_strict_refusal_is_its_own_exception_type_carrying_both_renderings(
    tmp_path,
):
    """拒启用专门的异常类型,启动期不靠匹配消息文本来分流。

    仍然是 ValueError 的子类,所以既有的「模型配置坏了」处理一个都不用改。
    """
    path = _write_config(
        tmp_path / "models.toml",
        _all_services() + "\n" + _bindings(omit={"kg_glean"}),
    )

    with pytest.raises(ModelBindingGapError) as exc_info:
        SystemModelServiceRegistry.load(_settings(path, strict=True), _ALL_KEYS)

    error = exc_info.value
    assert isinstance(error, ValueError)
    assert str(error) != error.loggable()
    assert str(path) in str(error)
    assert str(path) not in error.loggable()
    assert "知识补充抽取（kg_glean）" in error.loggable()
