"""PR-4:模型绑定的启动期硬校验接在 `create_app` 上,失败可读且进程起不来。

用户裁决:「不配模型会静默降级,影响最终用户的使用。应该在服务启动的时候 check
模型配置文件,如果有没有配置的,则直接失败并报错(哪些没配置,或者多配置了哪些)。」

为什么预检放在 `create_app` 而不是等仓库构造:`run_startup` 把任何异常都收进
readiness,进程照样活着、对外 503,日志里只剩一句被脱敏成「database
initialization failed」的 ValueError——运维既拿不到原因,也拿不到非零退出码。
`create_app` 抛出去则是 `uvicorn app.main:app` 直接起不来并非零退出,这里的两条
守卫钉住这个接线:少调一次(或挪到 FastAPI 构造之后)整仓依然全绿,而那正是回到
「静默降级」的路。
"""
import ast
import inspect
import json
import logging

import pytest

from app import main as main_module
from app.core.config import Settings
from app.main import _model_bindings_preflight
from app.services.model_registry import WORKLOADS


_SERVICE = """[services.chat]
display_name = "chat"
kind = "chat"
protocol = "openai"
base_url = "https://llm.example/v1"
model = "chat-model"
api_key_env = "PREFLIGHT_KEY"
max_concurrency = 2

[services.embed]
display_name = "embed"
kind = "embedding"
protocol = "dashscope"
base_url = "https://embed.example"
model = "embed-model"
api_key_env = "PREFLIGHT_KEY"
max_concurrency = 2

[services.rerank]
display_name = "rerank"
kind = "rerank"
protocol = "dashscope"
base_url = "https://rerank.example"
model = "rerank-model"
api_key_env = "PREFLIGHT_KEY"
max_concurrency = 2
"""

_BY_KIND = {"chat": "chat", "embedding": "embed", "rerank": "rerank"}


def _config(tmp_path, *, omit: set[str] = frozenset(), extra: str = "") -> str:
    lines = [
        f'{workload_id} = "{_BY_KIND[workload.kind]}"'
        for workload_id, workload in sorted(WORKLOADS.items())
        if workload_id not in omit
    ]
    path = tmp_path / "model-services.toml"
    path.write_text(
        _SERVICE + "\n[bindings]\n" + "\n".join(lines) + "\n" + extra,
        encoding="utf-8",
    )
    return str(path)


def _settings(path: str = "", *, strict: bool = True) -> Settings:
    return Settings(
        _env_file=None,
        model_services_config=path,
        model_bindings_strict=strict,
    )


def test_a_complete_binding_table_passes_the_preflight(monkeypatch, tmp_path):
    monkeypatch.setenv("PREFLIGHT_KEY", "secret")

    assert _model_bindings_preflight(_settings(_config(tmp_path))) is None


def test_empty_config_passes_because_offline_mode_is_a_supported_runtime():
    """`scripts/check.sh` 与本机开发都跑在这个形态下;它不是配错。"""
    assert _model_bindings_preflight(_settings()) is None


def test_a_missing_binding_logs_the_message_and_refuses_to_build_the_app(
    monkeypatch, tmp_path, caplog
):
    """异常带路径、日志不带:两个面同样可读,披露范围不同。

    AGENTS.md 禁止 private path 与 exception text 进日志。终止进程的异常不是
    日志,它要带上文件路径好让运维直接去改;写进日志的那行只说设置项名。
    """
    monkeypatch.setenv("PREFLIGHT_KEY", "secret")
    config = _config(tmp_path, omit={"kg_glean", "memory_embedding"})
    settings = _settings(config)

    with caplog.at_level(logging.ERROR, logger="silicon_notebook.startup"):
        with pytest.raises(ValueError) as exc_info:
            _model_bindings_preflight(settings)

    message = str(exc_info.value)
    assert "知识补充抽取（kg_glean）" in message
    assert "记忆向量（memory_embedding）" in message
    assert config in message

    # 运维往上翻要能一眼看到原因,所以日志里同样点名到工作负载(封闭词表,可记)。
    logged = [record.getMessage() for record in caplog.records]
    assert len(logged) == 1
    assert "知识补充抽取（kg_glean）" in logged[0]
    assert "记忆向量（memory_embedding）" in logged[0]
    assert "MODEL_SERVICES_CONFIG 指向的文件" in logged[0]
    # 日志既不带配置路径,也不带异常整句。
    assert config not in logged[0]
    assert str(tmp_path) not in logged[0]
    assert message not in logged[0]


def test_a_stale_binding_alone_is_enough_to_refuse(monkeypatch, tmp_path):
    monkeypatch.setenv("PREFLIGHT_KEY", "secret")
    settings = _settings(_config(tmp_path, extra='graph_chain_verify = "chat"\n'))

    with pytest.raises(ValueError, match="graph_chain_verify"):
        _model_bindings_preflight(settings)


def test_a_stale_key_that_is_not_shaped_like_an_id_never_reaches_the_log(
    monkeypatch, tmp_path, caplog
):
    """陈旧 id 是配置文件里的任意 TOML 键,不能原样进日志。

    合法形状的键(`ask_anwser`)照记——运维要靠它找到那一行;带引号/空格/换行
    /路径的键会折叠成一个计数占位符,否则一个精心构造的键就能往日志里注入
    标点、换行甚至一条假日志行。异常那一面不做这个折叠:它要如实说出文件里
    写的是什么。
    """
    monkeypatch.setenv("PREFLIGHT_KEY", "secret")
    hostile = '/etc/silicon/secret.toml\nERROR fake line'
    settings = _settings(_config(
        tmp_path,
        extra=f'ask_anwser = "chat"\n{json.dumps(hostile)} = "chat"\n',
    ))

    with caplog.at_level(logging.ERROR, logger="silicon_notebook.startup"):
        with pytest.raises(ValueError) as exc_info:
            _model_bindings_preflight(settings)

    assert hostile in str(exc_info.value)

    logged = [record.getMessage() for record in caplog.records]
    assert len(logged) == 1
    assert "ask_anwser（[bindings]）" in logged[0]
    assert "<无法显示的键名>×1（[bindings]）" in logged[0]
    assert "secret.toml" not in logged[0]
    assert "ERROR fake line" not in logged[0]
    assert "\n" not in logged[0]


def test_the_escape_hatch_downgrades_the_refusal_to_a_started_service(
    monkeypatch, tmp_path
):
    """MODEL_BINDINGS_STRICT=false 只是放行,不是「配置变对了」。

    放行之后由 `startup_warmup._unbound_workload_warnings` 在 READY 之前点名,
    见 `test_model_binding_warnings`。
    """
    monkeypatch.setenv("PREFLIGHT_KEY", "secret")
    settings = _settings(_config(tmp_path, omit={"kg_glean"}), strict=False)

    assert _model_bindings_preflight(settings) is None


def test_other_model_configuration_errors_stay_on_their_existing_path(
    monkeypatch, tmp_path, caplog
):
    """本次改动不扩大拒启范围:密钥缺失仍由既有路径决定失败时机。

    预检只记一行**固定文案**就放行——重新抛出的只有绑定表的问题。这里的异常
    文本可能带路径、服务 id、api_key_env 名甚至文件片段,所以一个字都不进日志
    (AGENTS.md);完整原因随异常走既有路径。把「TOML 语法错」「密钥没填」也在
    这里拒掉会改变一批既有部署的失败形态,属于另一件事。
    """
    monkeypatch.delenv("PREFLIGHT_KEY", raising=False)
    config = _config(tmp_path)
    settings = _settings(config)

    with caplog.at_level(logging.ERROR, logger="silicon_notebook.startup"):
        assert _model_bindings_preflight(settings) is None

    logged = [record.getMessage() for record in caplog.records]
    assert logged == [
        "模型服务配置无效（MODEL_SERVICES_CONFIG 指向的文件解析失败），详情见异常信息"
    ]
    assert "PREFLIGHT_KEY" not in logged[0]
    assert config not in logged[0]


def _create_app_tree() -> ast.FunctionDef:
    tree = ast.parse(inspect.getsource(main_module))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "create_app":
            return node
    raise AssertionError("create_app 不见了")


def _statement_index(function: ast.FunctionDef, target: ast.AST) -> int:
    """目标节点所在语句在函数里的执行序号——刻意不用 ``.lineno``。

    行号是源码位置,不是顺序语义:上面插一行注释就会改变断言里的数字,而语句的
    先后关系一点没变。仓库的架构策略因此把「拿 .lineno 当身份用」列为违规
    (``tests/architecture/policy.py`` 的 ``line-number-identity``)。语句序号表达
    的正是这条守卫要说的话——「这一步在那一步之前」。

    前序遍历里,包含目标的语句从外到内依次出现,取最大下标 = 最内层的那条语句;
    两个目标若埋在同一个块里,只比顶层下标会得到两个相同的数字。
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


def test_create_app_runs_the_preflight_before_it_builds_anything():
    """接线守卫:恰好调用一次、不在 try 里、且在 FastAPI(...) 构造之前。

    删掉那一行整仓仍然全绿——守卫在这里,是因为「校验在 app 建好之后才跑」与
    「压根没跑」对一个配错了绑定的部署是同一件事:两者都会让服务先起来。

    「不在 try 里」是同一条要求的第三种绕法,也是最容易在后续改动里无意中发生
    的一种:`create_app` 里已经有别的 best-effort try/except(日志归档提交),
    把这一行挪进任何一个 except 能吞掉异常的块,拒启就静默失效了。
    """
    create_app = _create_app_tree()
    calls = [
        node
        for node in ast.walk(create_app)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_model_bindings_preflight"
    ]
    assert len(calls) == 1

    guarded = [
        node
        for node in ast.walk(create_app)
        if isinstance(node, (ast.Try, ast.TryStar))
        and any(child is calls[0] for child in ast.walk(node))
    ]
    assert guarded == [], "预检不能被 try/except 包住,否则拒启会被吞掉"

    built = [
        node
        for node in ast.walk(create_app)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "FastAPI"
    ]
    assert len(built) == 1
    assert _statement_index(create_app, calls[0]) < _statement_index(
        create_app, built[0]
    ), "预检必须跑在 FastAPI(...) 构造之前"
