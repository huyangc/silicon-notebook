"""模型服务状态 payload 里的未绑定清单:与启动期 ``model-bindings:`` 告警同源。

状态面板按物理服务组织,未绑定的工作负载不产生任何一行;放行档
(``MODEL_BINDINGS_STRICT=false``)下「服务照跑、功能静默不可用」于是只能翻启动日志。
``ModelStatusService.snapshot(include_binding_gaps=True)`` 现在带一份清单,判定只有
一处:``model_registry.binding_report``,启动告警渲染的也是它。这里的用例因此都把
payload 与同一份设置/provider 上的启动告警**并排比**,而不只是各自断言一个期望值——
状态页另起一套口径(例如只看 chat、不看特性开关、离线时全列)时,两边会对不上。

为什么只跑单后端:清单是纯配置计算(注册表 + 两个 settings 布尔),不读不写存储;
``status_store`` 只给每服务的健康行用,这里用内存替身。
"""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.services.model_provider import RuntimeModelProvider
from app.services.model_registry import (
    WORKLOADS,
    ModelBindingGapError,
    SystemModelServiceRegistry,
    binding_report,
)
from app.services.model_status import ModelStatusService
from app.services.model_work import SchedulerSnapshot
from app.services.startup_warmup import _unbound_workload_warnings

_SERVICES = """[services.chat]
display_name = "chat"
kind = "chat"
context_window_tokens = 1048576
max_output_tokens = 131072
protocol = "openai"
base_url = "https://llm.example/v1"
model = "chat-model"
api_key_env = "GAPS_KEY"
max_concurrency = 2

[services.embed]
display_name = "embed"
kind = "embedding"
protocol = "dashscope"
base_url = "https://embed.example"
model = "embed-model"
api_key_env = "GAPS_KEY"
max_concurrency = 2

[services.rerank]
display_name = "rerank"
kind = "rerank"
protocol = "dashscope"
base_url = "https://rerank.example"
model = "rerank-model"
api_key_env = "GAPS_KEY"
max_concurrency = 2
"""

_BY_KIND = {"chat": "chat", "embedding": "embed", "rerank": "rerank"}

# 汇总行与特性行里的「中文标签（workload id）」;id 是代码里的封闭词表。
_NAMED_ID = re.compile(r"（([a-z0-9_]+)）")


def _write_config(path, *, omit=frozenset(), extra_bindings: str = "") -> str:
    lines = [
        f'{workload_id} = "{_BY_KIND[workload.kind]}"'
        for workload_id, workload in sorted(WORKLOADS.items())
        if workload_id not in omit
    ]
    path.write_text(
        _SERVICES + "\n[bindings]\n" + "\n".join(lines) + "\n" + extra_bindings,
        encoding="utf-8",
    )
    return str(path)


def _settings(config: str = "", *, strict: bool = False, **flags) -> Settings:
    values = dict(agent_profile_enabled=True, retrieval_experience_enabled=True)
    values.update(flags)
    return Settings(
        _env_file=None,
        model_services_config=config,
        model_bindings_strict=strict,
        **values,
    )


class _EventLog:
    def emit(self, *_args, **_kwargs):
        return None


class _MemoryStore:
    def get_all(self):
        return {}

    def record(self, **_values):
        return None


def _status(settings: Settings, registry: SystemModelServiceRegistry | None = None):
    registry = registry if registry is not None else SystemModelServiceRegistry.load(settings)
    provider = RuntimeModelProvider(settings, _EventLog(), registry=registry)
    service = ModelStatusService(
        registry, provider, _MemoryStore(), settings=settings
    )
    return service, provider


def _gaps(payload) -> dict:
    body = payload.model_dump()
    return {
        "unbound": [
            (item["id"], item["feature_enabled"]) for item in body["unbound_workloads"]
        ],
        "stale": body["stale_binding_ids"],
        "hidden": body["stale_binding_hidden_count"],
    }


def _warning_ids(lines: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """(汇总行点名的 id, 特性行点名的 id),按出现顺序。"""
    summary = [line for line in lines if "没有绑定" in line]
    feature = [line for line in lines if "特性已开但模型未绑定" in line]
    assert len(summary) <= 1
    return (
        _NAMED_ID.findall(summary[0]) if summary else [],
        [_NAMED_ID.findall(line)[0] for line in feature],
    )


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("GAPS_KEY", "secret")


def test_lenient_missing_bindings_are_listed_exactly_as_the_startup_warning_names_them(
    tmp_path,
):
    """放行档缺绑定:payload 列出全部三类工作负载,特性已开的那条单独标出。"""
    omitted = {"agent_profile_consolidate", "kg_glean", "chunk_embedding", "retrieval_rerank"}
    settings = _settings(_write_config(tmp_path / "m.toml", omit=omitted))
    service, provider = _status(settings)
    try:
        payload = service.snapshot(include_binding_gaps=True)
        warnings = _unbound_workload_warnings(settings, provider)
    finally:
        provider.close()

    gaps = _gaps(payload)
    catalogue_order = [workload_id for workload_id in WORKLOADS if workload_id in omitted]
    assert gaps["unbound"] == [
        (workload_id, workload_id == "agent_profile_consolidate")
        for workload_id in catalogue_order
    ]
    assert gaps["stale"] == [] and gaps["hidden"] == 0
    labels = {item.id: item.label for item in payload.unbound_workloads}
    assert labels == {workload_id: WORKLOADS[workload_id].display_label for workload_id in omitted}

    # 同源:启动告警点名的 id、顺序、特性升格与 payload 一一对应。
    summary_ids, feature_ids = _warning_ids(warnings)
    assert summary_ids == [workload_id for workload_id, _ in gaps["unbound"]]
    assert feature_ids == [workload_id for workload_id, enabled in gaps["unbound"] if enabled]
    # 每服务行不受影响。
    assert [item.service_id for item in payload.services] == ["chat", "embed", "rerank"]


def test_feature_switch_off_keeps_the_workload_listed_but_not_promoted(tmp_path):
    """特性关着的未绑定工作负载照列(绑定不因开关豁免),只是不标「功能已开启」。"""
    omitted = {"agent_profile_consolidate", "retrieval_experience_distill"}
    settings = _settings(
        _write_config(tmp_path / "m.toml", omit=omitted),
        agent_profile_enabled=False,
    )
    service, provider = _status(settings)
    try:
        gaps = _gaps(service.snapshot(include_binding_gaps=True))
        warnings = _unbound_workload_warnings(settings, provider)
    finally:
        provider.close()

    assert gaps["unbound"] == [
        ("agent_profile_consolidate", False),
        ("retrieval_experience_distill", True),
    ]
    assert _warning_ids(warnings) == (
        ["agent_profile_consolidate", "retrieval_experience_distill"],
        ["retrieval_experience_distill"],
    )


def test_strict_gate_leaves_nothing_to_list(tmp_path):
    """严格档(默认):能起来的注册表一定没有缺口,所以清单为空;缺绑定根本建不出注册表。"""
    complete = _settings(_write_config(tmp_path / "ok.toml"), strict=True)
    service, provider = _status(complete)
    try:
        gaps = _gaps(service.snapshot(include_binding_gaps=True))
        assert _unbound_workload_warnings(complete, provider) == ()
    finally:
        provider.close()
    assert gaps == {"unbound": [], "stale": [], "hidden": 0}

    broken = _settings(
        _write_config(tmp_path / "bad.toml", omit={"kg_glean"}, extra_bindings='ask_anwser = "chat"\n'),
        strict=True,
    )
    with pytest.raises(ModelBindingGapError):
        SystemModelServiceRegistry.load(broken)


def test_offline_mode_lists_nothing_just_like_the_single_startup_line():
    """空配置是受支持的离线模式:启动期只打一行「当前没有可用的模型服务」、不点名
    任何工作负载,payload 同样不把它报成「全都未绑定」。"""
    settings = _settings("")
    service, provider = _status(settings)
    try:
        payload = service.snapshot(include_binding_gaps=True)
        warnings = _unbound_workload_warnings(settings, provider)
    finally:
        provider.close()

    assert payload.services == []
    assert _gaps(payload) == {"unbound": [], "stale": [], "hidden": 0}
    assert len(warnings) == 1 and "当前没有可用的模型服务" in warnings[0]


def test_offline_mode_also_suppresses_stale_ids():
    """离线判定先于一切:启动告警在离线时连陈旧 id 那行都不打,payload 同口径。"""
    registry = SimpleNamespace(
        services=lambda: (), unknown_bindings=lambda: ("ask_anwser",)
    )
    provider = SimpleNamespace(
        registry=registry,
        configured=lambda _workload_id: False,
        scheduler_snapshot=lambda _service_id: None,
    )
    service = ModelStatusService(registry, provider, _MemoryStore(), settings=_settings())

    assert _gaps(service.snapshot(include_binding_gaps=True)) == {
        "unbound": [], "stale": [], "hidden": 0,
    }
    assert len(_unbound_workload_warnings(_settings(), provider)) == 1


def test_stale_ids_are_listed_with_the_log_lines_sanitiser(tmp_path):
    """放行档下被忽略的未知/已退役 id:能当 id 显示的照列,其它只计数——与日志那行
    `<无法显示的键名>×N` 同一把脱敏。"""
    settings = _settings(_write_config(
        tmp_path / "m.toml",
        extra_bindings=(
            'ask_anwser = "chat"\n'
            'graph_chain_verify = "chat"\n'
            '"Private/Path.toml" = "chat"\n'
        ),
    ))
    service, provider = _status(settings)
    try:
        payload = service.snapshot(include_binding_gaps=True)
        warnings = _unbound_workload_warnings(settings, provider)
    finally:
        provider.close()

    gaps = _gaps(payload)
    assert gaps == {
        "unbound": [],
        "stale": ["ask_anwser", "graph_chain_verify"],
        "hidden": 1,
    }
    assert "Private" not in payload.model_dump_json()
    assert len(warnings) == 1
    assert "ask_anwser，graph_chain_verify，<无法显示的键名>×1" in warnings[0]


def test_gaps_are_opt_in_per_call(tmp_path):
    """非管理员的缺口和容量诊断为空，其余每服务状态不变。"""
    settings = _settings(_write_config(
        tmp_path / "m.toml", omit={"kg_glean"}, extra_bindings='ask_anwser = "chat"\n'
    ))
    service, provider = _status(settings)
    try:
        plain = service.snapshot()
        full = service.snapshot(include_binding_gaps=True)
    finally:
        provider.close()

    assert _gaps(plain) == {"unbound": [], "stale": [], "hidden": 0}
    assert _gaps(full)["unbound"] == [("kg_glean", False)]
    capacity_fields = {"context_window_tokens", "max_output_tokens", "max_input_tokens"}
    assert all(row.context_window_tokens is None for row in plain.services)
    assert [row.model_dump(exclude=capacity_fields) for row in plain.services] == [
        row.model_dump(exclude=capacity_fields) for row in full.services
    ]


def test_test_all_keeps_the_gaps_in_the_replacement_snapshot(tmp_path):
    """「测试全部」是管理员整页替换快照的入口:它返回的快照不能把清单清空。"""
    settings = _settings(_write_config(tmp_path / "m.toml", omit={"kg_glean"}))
    service, provider = _status(settings)
    provider.probe = lambda service_id, **_kwargs: SimpleNamespace(
        service_id=service_id, config_fingerprint="other"
    )
    try:
        payload = service.test_all(actor_id="admin")
    finally:
        provider.close()

    assert _gaps(payload)["unbound"] == [("kg_glean", False)]


def test_hot_reload_is_reflected_without_a_restart(tmp_path):
    """清单每次读现场注册表:放行档下热加载补齐绑定后,面板随之清空。"""
    path = tmp_path / "m.toml"
    settings = _settings(_write_config(path, omit={"kg_glean"}))
    provider = RuntimeModelProvider(settings, _EventLog())
    service = ModelStatusService(
        provider.registry, provider, _MemoryStore(), settings=settings
    )
    try:
        assert _gaps(service.snapshot(include_binding_gaps=True))["unbound"] == [
            ("kg_glean", False)
        ]
        _write_config(path)
        assert provider.reload_if_changed(force=True) is True
        assert _gaps(service.snapshot(include_binding_gaps=True))["unbound"] == []
    finally:
        provider.close()


def test_a_registry_that_cannot_answer_fails_open_without_breaking_the_panel(tmp_path):
    """fail-open 同启动诊断:算不出清单只丢清单,每服务行照常返回。"""

    class _Broken(SystemModelServiceRegistry):
        def unknown_bindings(self):
            raise RuntimeError("diagnostic reader exploded")

    loaded = SystemModelServiceRegistry.load(
        _settings(_write_config(tmp_path / "m.toml", omit={"kg_glean"}))
    )
    registry = _Broken(
        {service.id: service for service in loaded.services()},
        {
            workload_id: service.id
            for service in loaded.services()
            for workload_id in (workload.id for workload in loaded.workloads_for(service.id))
        },
    )
    provider = SimpleNamespace(
        registry=registry,
        scheduler_snapshot=lambda _service_id: SchedulerSnapshot(
            active=0, maximum=1, queued=0, oldest_wait_ms=0,
            breaker_state="closed", busy=False,
        ),
    )
    service = ModelStatusService(registry, provider, _MemoryStore(), settings=_settings())

    payload = service.snapshot(include_binding_gaps=True)
    assert [item.service_id for item in payload.services] == ["chat", "embed", "rerank"]
    assert _gaps(payload) == {"unbound": [], "stale": [], "hidden": 0}


class _SwappingProvider:
    """A provider whose ``.registry`` is rebound by a "hot reload" mid-read.

    The first ``reads`` reads of ``.registry`` return ``first``; every later
    read returns ``second`` — exactly what a reload thread publishing
    ``self.registry = candidate`` between two attribute reads looks like.
    ``configured`` reads through the same attribute, as the real provider does.
    """

    def __init__(self, first, second, *, reads: int = 1) -> None:
        self._first, self._second, self._left = first, second, reads
        self.reads = 0

    @property
    def registry(self):
        self.reads += 1
        if self._left > 0:
            self._left -= 1
            return self._first
        return self._second

    def configured(self, workload_id):
        return self.registry.service_for(workload_id) is not None

    def scheduler_snapshot(self, _service_id):
        return SchedulerSnapshot(
            active=0, maximum=2, queued=0, oldest_wait_ms=0,
            breaker_state="closed", busy=False,
        )


def test_the_report_comes_from_one_registry_generation(tmp_path):
    """热重载在离线判定之后换上一份空注册表:报告仍整份来自读到的那一代,
    不会把全部工作负载报成未绑定(规格明令禁止的「全都未绑定」)。启动告警走同一个
    函数,一并钉住。"""
    settings = _settings(_write_config(tmp_path / "m.toml", omit={"kg_glean"}))
    populated = SystemModelServiceRegistry.load(settings)
    emptied = SystemModelServiceRegistry({}, {})

    provider = _SwappingProvider(populated, emptied)
    report = binding_report(settings, provider)
    assert [workload.id for workload in report.unbound] == ["kg_glean"]
    assert provider.reads == 1

    provider = _SwappingProvider(populated, emptied)
    summary_ids, _ = _warning_ids(_unbound_workload_warnings(settings, provider))
    assert summary_ids == ["kg_glean"]


def test_snapshot_rows_and_gaps_share_one_registry_generation(tmp_path):
    """同一次快照里每服务行(含「使用位置」)与缺口来自同一份注册表。"""
    settings = _settings(_write_config(tmp_path / "m.toml", omit={"kg_glean"}))
    populated = SystemModelServiceRegistry.load(settings)
    provider = _SwappingProvider(populated, SystemModelServiceRegistry({}, {}))
    service = ModelStatusService(populated, provider, _MemoryStore(), settings=settings)

    payload = service.snapshot(include_binding_gaps=True)

    assert provider.reads == 1
    assert [item.service_id for item in payload.services] == ["chat", "embed", "rerank"]
    assert all(item.workloads for item in payload.services)
    assert _gaps(payload)["unbound"] == [("kg_glean", False)]
