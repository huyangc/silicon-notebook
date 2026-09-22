"""PR-3·T8:启动时点名「没有任何模型服务绑定」的 chat 工作负载。

`_unbound_workload_warnings` 是纯函数(只读注册表与两个 settings 布尔),这里
直接单测它,不经由完整的 `run_startup`/真实模型配置。`run_startup` 侧的接线是
READY 日志之前的一圈 `logger.warning(...)`——它是这个函数**唯一**的消费点,所以
另配一条 AST 守卫钉住那一圈还在、且仍在 READY 之前;没有它,把那五行删掉整仓
依然全绿,这条诊断就只剩一份没人调用的实现。

这条告警存在的理由是一次真实事故:主 checkout 的 `.local/model-services.toml`
生成于 Agentic Memory P1/P2 之前,缺 `agent_profile_consolidate` 与
`retrieval_experience_distill` 两个绑定;注册表对未绑定工作负载返回 ``None``、
全链路 fail-soft,于是「特性开着、作业每次都落 failed:模型未配置」这件事在启动
日志里一个字都没有。
"""
import ast
import inspect
from types import SimpleNamespace

from app.services import startup_warmup
from app.services.model_registry import WORKLOADS
from app.services.startup_warmup import _unbound_workload_warnings


def _settings(**overrides) -> SimpleNamespace:
    base = dict(agent_profile_enabled=True, retrieval_experience_enabled=True)
    base.update(overrides)
    return SimpleNamespace(**base)


def _models(unbound: frozenset[str] | set[str], *, services: int = 1):
    """A provider double with the two read-only members this diagnostic uses."""
    registry = SimpleNamespace(services=lambda: tuple(range(services)))
    return SimpleNamespace(
        registry=registry,
        configured=lambda workload_id: workload_id not in unbound,
    )


CHAT_WORKLOAD_IDS = frozenset(
    workload.id for workload in WORKLOADS.values() if workload.kind == "chat"
)


def test_every_chat_workload_bound_is_silent():
    assert _unbound_workload_warnings(_settings(), _models(frozenset())) == ()


def test_the_two_agentic_memory_workloads_each_get_their_own_line():
    """P1/P2 两个工作负载未绑定且特性开着:汇总一行 + 各自点名一行。"""
    warnings = _unbound_workload_warnings(
        _settings(),
        _models({"agent_profile_consolidate", "retrieval_experience_distill"}),
    )
    assert len(warnings) == 3
    summary, profile_line, experience_line = warnings
    assert "没有绑定" in summary
    assert "库理解整理（agent_profile_consolidate）" in summary
    assert "检索打法总结（retrieval_experience_distill）" in summary
    assert "重新生成 model-services.toml" in summary
    assert profile_line == (
        "model-bindings: 特性已开但模型未绑定："
        "库理解整理（agent_profile_consolidate）"
        "——请在 model-services.toml 的 [bindings] 补绑定或重新生成配置"
    )
    assert experience_line == (
        "model-bindings: 特性已开但模型未绑定："
        "检索打法总结（retrieval_experience_distill）"
        "——请在 model-services.toml 的 [bindings] 补绑定或重新生成配置"
    )


def test_feature_switched_off_leaves_only_the_summary_line():
    """特性关着的未绑定工作负载不升格:它没在「静默失效」,只是没接。"""
    warnings = _unbound_workload_warnings(
        _settings(agent_profile_enabled=False, retrieval_experience_enabled=False),
        _models({"agent_profile_consolidate", "retrieval_experience_distill"}),
    )
    assert len(warnings) == 1
    assert "库理解整理（agent_profile_consolidate）" in warnings[0]
    assert "特性已开但模型未绑定" not in warnings[0]


def test_only_the_feature_that_is_switched_on_gets_promoted():
    warnings = _unbound_workload_warnings(
        _settings(agent_profile_enabled=False),
        _models({"agent_profile_consolidate", "retrieval_experience_distill"}),
    )
    assert len(warnings) == 2
    assert "检索打法总结" in warnings[1]
    assert "库理解整理（agent_profile_consolidate）" not in warnings[1]


def test_deployment_with_no_model_services_at_all_is_exempt():
    """空 MODEL_SERVICES_CONFIG 是受支持的离线运行时(与
    ``primary_unconfigured`` 同一条判据),已有自己的启动提示;在那里逐条列出
    全部 chat 工作负载只是噪音。"""
    assert _unbound_workload_warnings(
        _settings(), _models(CHAT_WORKLOAD_IDS, services=0)
    ) == ()


def test_only_chat_workloads_are_checked():
    """向量/重排工作负载不在这条告警的范围里(它们的缺席由检索侧自己报)。"""
    embedding_ids = {
        workload.id for workload in WORKLOADS.values() if workload.kind != "chat"
    }
    assert embedding_ids  # 目录里确实有非 chat 工作负载,否则这条用例是空断言
    assert _unbound_workload_warnings(_settings(), _models(embedding_ids)) == ()


def test_diagnostic_never_raises_on_a_minimal_double():
    """fail-open 同 `_pool_budget_warning`:告警计算不得改变启动的形状。

    provider 取不到(启动早期/测试替身)→ 一条都不报;settings 取不到 →
    汇总行照报,两条特性行按「读不到就当没开」静默,而不是抛。
    """
    assert _unbound_workload_warnings(_settings(), None) == ()
    settings_less = _unbound_workload_warnings(None, _models({"ask_answer"}))
    assert len(settings_less) == 1
    assert "问答回答（ask_answer）" in settings_less[0]


def _run_startup_tree() -> ast.FunctionDef:
    tree = ast.parse(inspect.getsource(startup_warmup))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "run_startup":
            return node
    raise AssertionError("run_startup 不见了")


def test_run_startup_still_emits_these_warnings_before_the_ready_line():
    """接线守卫:`run_startup` 恰好调用一次这个诊断,且在 READY 之前。

    这个函数只有这一个消费点,删掉那五行整仓仍然全绿——守卫在这里,是因为
    「告警在 READY 之后才打」与「压根没打」对一个只往上翻到就绪行的运维者
    是同一件事,而两者都不会让任何断言变红。
    """
    run_startup = _run_startup_tree()
    calls = [
        node
        for node in ast.walk(run_startup)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_unbound_workload_warnings"
    ]
    assert len(calls) == 1, "唯一消费点:多一处或少一处都要重新想清楚顺序"

    ready_markers = [
        node
        for node in ast.walk(run_startup)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("startup: READY")
    ]
    assert len(ready_markers) == 1
    # 顺序按**源码顺序的节点位置**比,不按行号:行号是诊断元数据,不是身份
    # (仓库策略 test_test_architecture_policy 的 line-number-identity 判据)。
    # ``ast.walk`` 是广度优先、不保证源码顺序,所以自己按子节点顺序做一次前序遍历。
    in_source_order = list(_source_order(run_startup))
    assert in_source_order.index(calls[0]) < in_source_order.index(ready_markers[0]), (
        "告警必须打在 READY 那行之前"
    )


def _source_order(node: ast.AST):
    """Pre-order traversal following ``ast.iter_child_nodes`` -- the order the
    statements appear in the source -- so two nodes can be compared by position
    without ever reading their line numbers."""
    yield node
    for child in ast.iter_child_nodes(node):
        yield from _source_order(child)
