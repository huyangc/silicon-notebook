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
import os
from pathlib import Path
import subprocess
import sys

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


def test_a_missing_binding_logs_the_message_and_stops_the_process(
    monkeypatch, tmp_path, caplog
):
    """终止进程的那个异常也是日志,所以它和 logger 那行必须同一口径。

    `scripts/prod.sh` / `scripts/backend.sh` 把 uvicorn 的 stderr 追加进后端
    日志文件,一条 traceback 就等于把异常文本写进了日志。预检因此把
    `ModelBindingGapError` 换成 `SystemExit(exc.loggable())`:退出码照旧,
    stderr 只剩脱敏后的那一行。
    """
    monkeypatch.setenv("PREFLIGHT_KEY", "secret")
    config = _config(tmp_path, omit={"kg_glean", "memory_embedding"})
    settings = _settings(config)

    with caplog.at_level(logging.ERROR, logger="silicon_notebook.startup"):
        with pytest.raises(SystemExit) as exc_info:
            _model_bindings_preflight(settings)

    # 运维往上翻要能一眼看到原因,所以两处同样点名到工作负载(封闭词表,可记)。
    message = str(exc_info.value)
    logged = [record.getMessage() for record in caplog.records]
    assert logged == [message]
    assert "知识补充抽取（kg_glean）" in message
    assert "记忆向量（memory_embedding）" in message
    assert "MODEL_SERVICES_CONFIG 指向的文件" in message
    # 两处都不带配置路径;异常链也被切断,原异常不会跟着 traceback 一起出去。
    assert config not in message
    assert str(tmp_path) not in message
    # `from None` 切断异常链:__cause__ 为空且 __suppress_context__ 为真,解释器
    # 因此不会在 stderr 上顺带打印原异常(那条带路径)。真进程的证据见下面的
    # subprocess 用例。
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__ is True


def test_a_stale_binding_alone_is_enough_to_refuse(monkeypatch, tmp_path):
    monkeypatch.setenv("PREFLIGHT_KEY", "secret")
    settings = _settings(_config(tmp_path, extra='graph_chain_verify = "chat"\n'))

    with pytest.raises(SystemExit, match="graph_chain_verify"):
        _model_bindings_preflight(settings)


def test_a_stale_key_that_is_not_shaped_like_an_id_never_reaches_the_log(
    monkeypatch, tmp_path, caplog
):
    """陈旧 id 是配置文件里的任意 TOML 键,不能原样进日志。

    合法形状的键(`ask_anwser`)照记——运维要靠它找到那一行;带引号/空格/换行
    /路径的键会折叠成一个计数占位符,否则一个精心构造的键就能往日志里注入
    标点、换行甚至一条假日志行。stderr 同理:它也会被追加进后端日志。

    `ModelBindingGapError` 本身仍然如实说出文件里写的是什么(见
    `test_model_registry`),只是不让它从 main 逃出去。
    """
    monkeypatch.setenv("PREFLIGHT_KEY", "secret")
    hostile = '/etc/silicon/secret.toml\nERROR fake line'
    settings = _settings(_config(
        tmp_path,
        extra=f'ask_anwser = "chat"\n{json.dumps(hostile)} = "chat"\n',
    ))

    with caplog.at_level(logging.ERROR, logger="silicon_notebook.startup"):
        with pytest.raises(SystemExit) as exc_info:
            _model_bindings_preflight(settings)

    assert hostile not in str(exc_info.value)

    logged = [record.getMessage() for record in caplog.records]
    assert len(logged) == 1
    assert logged[0] == str(exc_info.value)
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


def test_other_model_configuration_errors_get_one_fixed_sentence_and_no_more(
    monkeypatch, tmp_path, caplog
):
    """密钥缺失、TOML 语法错等:同样停进程,但只说一句固定文案。

    这些 loader 异常可能带路径、服务 id、api_key_env 名甚至文件片段,而 stderr
    会被追加进后端日志——所以异常文本一个字都不许出去,日志与 stderr 都只有这
    一句。具体哪一行错了,运维照着 MODEL_SERVICES_CONFIG 去看文件。
    """
    monkeypatch.delenv("PREFLIGHT_KEY", raising=False)
    config = _config(tmp_path)
    settings = _settings(config)

    with caplog.at_level(logging.ERROR, logger="silicon_notebook.startup"):
        with pytest.raises(SystemExit) as exc_info:
            _model_bindings_preflight(settings)

    logged = [record.getMessage() for record in caplog.records]
    assert logged == ["模型服务配置无效（MODEL_SERVICES_CONFIG 指向的文件解析失败）"]
    assert str(exc_info.value) == logged[0]
    assert "PREFLIGHT_KEY" not in logged[0]
    assert config not in logged[0]
    # `from None` 切断异常链:__cause__ 为空且 __suppress_context__ 为真,解释器
    # 因此不会在 stderr 上顺带打印原异常(那条带路径)。真进程的证据见下面的
    # subprocess 用例。
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__ is True


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


# ---------------------------------------------------------------------------
# 真实子进程:caplog 只看得到 logger,看不到逃逸的异常。而 `scripts/prod.sh` 与
# `scripts/backend.sh` 都用 `>>"$BACKEND_LOG" 2>&1` 启 uvicorn,所以 **stderr
# 就是后端日志**——一条 traceback 等于把异常文本写进了日志。下面两条用真进程
# 断言 stderr 的实际字节,是这条边界唯一不靠推理的证据。
# ---------------------------------------------------------------------------


_BACKEND_DIR = Path(__file__).resolve().parents[1]


def _boot(config: str, **overrides: str) -> subprocess.CompletedProcess:
    """`import app.main`(= `uvicorn app.main:app` 走的那条路)跑在真子进程里。"""
    environment = {
        "PATH": os.defpath,
        "PYTHONPATH": str(_BACKEND_DIR),
        "PYTHONIOENCODING": "utf-8",
        "SILICON_NOTEBOOK_ENV_FILE": "",
        "ALLOW_NO_ENV_FILE": "1",
        "EXTENSIONS_CONFIG": "",
        "MINERU_MODE": "off",
        "MINERU_API_TOKEN": "",
        "MODEL_SERVICES_CONFIG": config,
        "PREFLIGHT_KEY": "secret",
        **overrides,
    }
    return subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=str(_BACKEND_DIR),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_missing_binding_stops_the_real_process_without_a_traceback(tmp_path):
    """真进程:退出码 1,stderr 只有脱敏那一行。

    `SystemExit(str)` 让解释器打印消息并以 1 退出,不打印 traceback。断言的是
    「没有 Traceback、没有 ModelBindingGapError 这个类名、没有配置路径」——
    三者任何一个出现在 stderr,都等于出现在了生产的后端日志文件里。
    """
    config = _config(tmp_path, omit={"kg_glean"}, extra='graph_chain_verify = "chat"\n')

    result = _boot(config)

    assert result.returncode == 1
    assert "model-bindings: " in result.stderr
    assert "知识补充抽取（kg_glean）" in result.stderr
    assert "graph_chain_verify（[bindings]）" in result.stderr
    assert "MODEL_SERVICES_CONFIG 指向的文件" in result.stderr
    assert "Traceback" not in result.stderr
    assert "ModelBindingGapError" not in result.stderr
    assert config not in result.stderr
    assert str(tmp_path) not in result.stderr


def test_a_hostile_stale_key_cannot_forge_a_line_in_the_backend_log(tmp_path):
    """真进程:构造出来的键名既进不了 stderr,也伪造不出额外的日志行。"""
    hostile = "/etc/silicon/secret.toml\nERROR forged line"
    config = _config(
        tmp_path, extra=f'{json.dumps(hostile)} = "chat"\n'
    )

    result = _boot(config)

    assert result.returncode == 1
    assert "<无法显示的键名>×1（[bindings]）" in result.stderr
    assert "/etc/silicon" not in result.stderr
    assert "forged line" not in result.stderr


def test_other_configuration_errors_print_only_the_fixed_sentence(tmp_path):
    """真进程:密钥缺失同样停进程,stderr 只有那一句,不含 api_key_env 名。"""
    config = _config(tmp_path)

    result = _boot(config, PREFLIGHT_KEY="")

    assert result.returncode == 1
    assert result.stderr.strip().endswith(
        "模型服务配置无效（MODEL_SERVICES_CONFIG 指向的文件解析失败）"
    )
    assert "Traceback" not in result.stderr
    assert "ValueError" not in result.stderr
    assert "PREFLIGHT_KEY" not in result.stderr
    assert config not in result.stderr


def test_a_complete_binding_table_lets_the_real_process_import_cleanly(tmp_path):
    """对照组:同一条路径在配置正确时照常 import 成功,退出码 0。

    没有它,上面三条也会在「预检根本没跑」时全绿。
    """
    result = _boot(_config(tmp_path))

    assert result.returncode == 0, result.stderr
    assert "model-bindings: " not in result.stderr
