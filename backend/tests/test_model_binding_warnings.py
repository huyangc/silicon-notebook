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


def test_deployment_with_no_model_services_at_all_gets_exactly_one_line():
    """空 MODEL_SERVICES_CONFIG 是受支持的离线运行时(与 ``primary_unconfigured``
    同一条判据):逐条列出全部 chat 工作负载只是噪音,但「所有需要模型的功能都在
    走确定性回复」这件事必须在 READY 之前说一次——PR-4 之前它一个字都不打,而
    严格闸刻意不管这个形态(它是选出来的降级,不是配错)。"""
    warnings = _unbound_workload_warnings(
        _settings(), _models(CHAT_WORKLOAD_IDS, services=0)
    )

    assert len(warnings) == 1
    assert "当前没有可用的模型服务" in warnings[0]
    assert "降级为确定性回复" in warnings[0]
    # 文案只说现象:判据是「注册表里没有可用服务」,不猜成因,所以不提配置项名。
    assert "MODEL_SERVICES_CONFIG" not in warnings[0]
    # 离线模式不点名任何工作负载,也不提严格闸。
    assert "ask_answer" not in warnings[0]
    assert "MODEL_BINDINGS_STRICT" not in warnings[0]


def test_stale_binding_ids_get_their_own_line_naming_them():
    """放行档下被丢弃的未知/已退役 id 必须在启动日志里点名——丢弃≠无声。"""
    models = _models(frozenset())
    models.registry.unknown_bindings = lambda: ("ask_anwser", "graph_chain_verify")

    warnings = _unbound_workload_warnings(_settings(), models)

    assert len(warnings) == 1
    # 分隔符与拒启消息同口径(全角逗号)。
    assert "ask_anwser，graph_chain_verify" in warnings[0]
    assert "MODEL_BINDINGS_STRICT" in warnings[0]


def test_a_stale_key_that_is_not_shaped_like_an_id_never_reaches_this_line():
    """陈旧 id 是配置文件里的任意 TOML 键,放行档的告警行同样不许原样吐出来。

    这条告警是严格闸被关掉时的替代诊断,脱敏口径必须和拒启那条一致(两处共用
    `model_registry.loggable_id_list`):带路径的键会把私有路径写进日志,带转义
    换行的键能在日志里伪造出一整行,而运维看到的将是一条不存在的记录。
    """
    hostile = "/etc/silicon/secret.toml\nERROR forged line"
    models = _models(frozenset())
    models.registry.unknown_bindings = lambda: ("ask_anwser", hostile, "x" * 80)

    warnings = _unbound_workload_warnings(_settings(), models)

    assert len(warnings) == 1
    line = warnings[0]
    assert "ask_anwser" in line
    assert "<无法显示的键名>×2" in line
    assert "/etc/silicon" not in line
    assert "ERROR forged line" not in line
    assert "\n" not in line


def test_a_registry_without_the_stale_id_reader_still_gets_the_other_lines():
    """兼容读法:老 provider 替身没有 unknown_bindings,只丢这一行,不丢全部。"""
    warnings = _unbound_workload_warnings(
        _settings(), _models({"agent_profile_consolidate"})
    )

    assert len(warnings) == 2
    assert "库理解整理（agent_profile_consolidate）" in warnings[0]


def test_every_workload_kind_is_checked_not_just_chat():
    """向量/重排工作负载同样点名:告警口径必须与严格闸一致。

    PR-3 只看 chat。PR-4 之后这条告警是「严格闸被关掉时」的替代诊断,一个比它
    所替代的拒启更窄的告警,会让一个其实缺着向量绑定的部署以为自己没事——未绑定
    的 embedding/rerank 让检索静默降级,和未绑定的 chat 一样严重。
    """
    non_chat_ids = {
        workload.id for workload in WORKLOADS.values() if workload.kind != "chat"
    }
    assert non_chat_ids  # 目录里确实有非 chat 工作负载,否则这条用例是空断言

    warnings = _unbound_workload_warnings(_settings(), _models(non_chat_ids))

    assert len(warnings) == 1
    assert "来源分块向量（chunk_embedding）" in warnings[0]
    assert "检索结果重排（retrieval_rerank）" in warnings[0]
    # 汇总行不再自称只管 chat。
    assert "chat" not in warnings[0]
    # 两条特性行仍然只对那两个 chat 工作负载升格。
    assert "特性已开但模型未绑定" not in warnings[0]


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


def _statement_index(function: ast.FunctionDef, target: ast.AST) -> int:
    """目标节点所在语句在函数里的执行序号——刻意不用 ``.lineno``。

    行号是源码位置,不是顺序语义:上面插一行注释就会改变断言里的数字,而语句的
    先后关系一点没变。仓库的架构策略因此把「拿 .lineno 当身份用」列为违规
    (``tests/architecture/policy.py`` 的 ``line-number-identity``)。语句序号表达
    的正是这条守卫要说的话——「这一步在那一步之前」——并且对重排敏感、对排版
    不敏感。

    前序遍历里,包含目标的语句从外到内依次出现,所以取最大下标 = 最内层的那条
    语句。``run_startup`` 的两个目标都埋在同一个 ``try:`` 里,只比顶层下标会得
    到两个相同的数字。
    """
    ordered: list[ast.stmt] = []

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                ordered.append(child)
            visit(child)

    visit(function)
    containing = [
        index
        for index, statement in enumerate(ordered)
        if any(node is target for node in ast.walk(statement))
    ]
    assert containing, "目标节点不在这个函数里"
    return max(containing)


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

    ready = [
        node
        for node in ast.walk(run_startup)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("startup: READY")
    ]
    assert len(ready) == 1
    assert _statement_index(run_startup, calls[0]) < _statement_index(
        run_startup, ready[0]
    ), "告警必须打在 READY 那句之前"
