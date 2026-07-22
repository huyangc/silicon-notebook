"""`.claude/hooks/require-subagent-model.py` 的回归网。

这个 hook 是 `CLAUDE.md`「子代理规范」的执行侧：**起子代理必须显式选模型，
不得默认继承主 agent**。它是一个手写的极简 YAML frontmatter 解析器，评审里被
找出五个绕过口，全靠临时脚本照出来——没有 committed 测试的话，下一次改动会把
它们静默重新打开。本仓库自己的规矩就是「加了守卫 ≠ 有效，必须变异验证」，那这
份守卫自己也得有网。

用例分两类，缺一不可：

* **绕过**（`deny` 侧）：一个实际会继承父模型的调用被放行。这是最严重的一类，
  也是历次评审的全部真问题所在。
* **误拦**（`allow` 侧）：把合法调用堵死。守卫误拦会让人绕开它，同样致命。

判定矩阵**在进程内**跑 `evaluate()`。早先的版本让每条用例都起一个解释器，45 次
进程冷启动往后端泳道里灌了 16 CPU 秒，把同批跑的 `test_diag_db.py` 里那条只给
「起解释器 + 跑完脚本」0.25 秒预算的用例挤爆——CI 因此变红。进程契约（stdin/
stdout 形状、退出码、编码）另有少量子进程用例专门守，那是真需要跨进程才测得到
的部分。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / ".claude" / "hooks" / "require-subagent-model.py"

# 仓库自带的三个角色，模型钉在各自 frontmatter 里。
REPO_PINNED_ROLES = ("impl-task", "spec-review", "code-quality-review")


def _load_hook():
    """按路径加载 hook 模块（文件名带连字符，不能直接 import）。"""
    spec = importlib.util.spec_from_file_location("require_subagent_model", HOOK)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = _load_hook()


def _allows(payload: dict, project_dir: Path) -> bool:
    """判定是否放行。`evaluate()` 返回 None 即放行，否则返回拦截理由。"""
    return hook.evaluate(payload, project_dir) is None


def _agent_call(**tool_input) -> dict:
    return {"tool_name": "Agent", "tool_input": tool_input}


def _write_agent(directory: Path, filename: str, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(body, encoding="utf-8")


@pytest.fixture
def scopes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """一个空的项目作用域，并把 HOME 指向同样干净的用户作用域。

    hook 会同时扫项目级和用户级 `.claude/agents/`，用例必须两边都掌控，
    否则开发者本机真实的 `~/.claude/agents/` 会渗进断言。
    """
    project = tmp_path / "proj"
    home = tmp_path / "home"
    for root in (project, home):
        (root / ".claude" / "agents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return project


@pytest.fixture
def home_agents(tmp_path: Path) -> Path:
    return tmp_path / "home" / ".claude" / "agents"


# --------------------------------------------------------------- 基本闸门语义


def test_call_without_model_is_denied(scopes):
    """头号拦截对象：不带 model 的通用子代理调用。

    这条同时是整份用例的 sanity 基线——它若变绿，说明测试自己打空了，
    后面所有 `allow` 断言都不再有意义。
    """
    assert not _allows(_agent_call(prompt="x", subagent_type="general-purpose"), scopes)


def test_explicit_model_is_allowed(scopes):
    assert _allows(
        _agent_call(prompt="x", subagent_type="general-purpose", model="haiku"), scopes
    )


def test_legacy_task_tool_name_is_also_gated(scopes):
    """工具在 2.x 里叫 `Agent`，运行时仍接受历史别名 `Task`。"""
    assert not _allows({"tool_name": "Task", "tool_input": {"subagent_type": "Explore"}}, scopes)


def test_other_tools_are_untouched(scopes):
    assert _allows({"tool_name": "Bash", "tool_input": {"command": "ls"}}, scopes)


def test_explicit_fork_is_exempt(scopes):
    """fork 必须继承父模型，传 model 无效，所以豁免。"""
    assert _allows(_agent_call(subagent_type="fork"), scopes)


def test_omitted_subagent_type_is_not_treated_as_fork(scopes):
    """省略 `subagent_type` 等于默认 general-purpose，不是隐式 fork。

    评审里有过「把它当 fork 豁免」的建议，被驳回：那正是本门的头号拦截对象，
    照做等于把门整个掏空。这条钉住那个决定。
    """
    assert not _allows(_agent_call(prompt="x"), scopes)


# ----------------------------------------------------- model 值：什么不算「选择」


@pytest.mark.parametrize(
    "model",
    ["inherit", "INHERIT", "  inherit  ", "", "   ", "null", "none", "~"],
)
def test_non_choice_model_values_are_denied(scopes, model):
    """填了但没做选择，等同于没填。

    `inherit` 保留的正是本门要禁的继承语义；YAML 的三种空值写法同理。
    """
    assert not _allows(_agent_call(model=model), scopes)


@pytest.mark.parametrize("model", ["opus", "  opus  ", "sonnet", "haiku"])
def test_real_model_values_are_allowed(scopes, model):
    assert _allows(_agent_call(model=model), scopes)


# ------------------------------------------------------ 角色定义：钉没钉住模型


def test_repo_roles_are_pinned_and_need_no_model():
    """仓库自带的三个角色不必再传 model —— 模型钉在定义里。

    走真实仓库目录，所以这条同时守住「有人把 impl-task 的 model 删了或改成
    inherit」这类改动。
    """
    for role in REPO_PINNED_ROLES:
        assert _allows(_agent_call(subagent_type=role), ROOT), f"{role} 应免 model 放行"


@pytest.mark.parametrize(
    "model_line",
    [
        "model: inherit",
        "model: inherit # 跟主 agent 走",
        "model:",
        "model: ~",
        "model: null",
        "model: INHERIT",
        # 块标量、锚、别名、标签、流式集合：本解析器读不懂的形态一律按未选处理。
        # `model: >-` 后接缩进的 inherit 是合法 YAML，真解析器看到的是 inherit。
        "model: >-\n  inherit",
        "model: |-\n  sonnet",
        "model: &m opus",
        "model: !!str opus",
        "model: [opus]",
        "model: {a: opus}",
    ],
)
def test_definition_without_a_real_pin_is_denied(scopes, model_line):
    _write_agent(
        scopes / ".claude" / "agents", "role.md", f"---\nname: role\n{model_line}\n---\n正文\n"
    )
    assert not _allows(_agent_call(subagent_type="role"), scopes)


@pytest.mark.parametrize(
    "model_line",
    ["model: opus", 'model: "sonnet"', "model: sonnet   # 转录型够用", 'model: "opus" # 带注释'],
)
def test_definition_with_a_real_pin_is_allowed(scopes, model_line):
    _write_agent(
        scopes / ".claude" / "agents", "role.md", f"---\nname: role\n{model_line}\n---\n正文\n"
    )
    assert _allows(_agent_call(subagent_type="role"), scopes)


def test_definition_is_keyed_by_frontmatter_name_not_filename(scopes):
    """`name` 是真名，文件名不是；嵌套子目录也要能发现。"""
    _write_agent(
        scopes / ".claude" / "agents" / "nested" / "deep",
        "whatever.md",
        "---\nname: renamed-role\nmodel: opus\n---\n正文\n",
    )
    assert _allows(_agent_call(subagent_type="renamed-role"), scopes)
    assert not _allows(_agent_call(subagent_type="whatever"), scopes)


def test_unknown_subagent_type_is_denied(scopes):
    assert not _allows(_agent_call(subagent_type="not-a-real-agent"), scopes)


# ------------------------------------------------------------- 作用域优先级


def test_project_scope_shadows_a_pinned_user_definition(scopes, home_agents):
    """项目级定义压过用户级同名定义 —— 与 Claude Code 的解析顺序一致。

    项目级没钉模型时必须拦：真正跑起来的是项目级那条，仍然继承主 agent。
    只按「钉了模型才记名字」来收集，就会拿用户级那条放行。
    """
    _write_agent(
        scopes / ".claude" / "agents", "collide.md", "---\nname: collide\nmodel: inherit\n---\n"
    )
    _write_agent(home_agents, "collide.md", "---\nname: collide\nmodel: opus\n---\n")
    assert not _allows(_agent_call(subagent_type="collide"), scopes)


def test_user_scope_pin_is_honored_when_project_has_no_such_role(scopes, home_agents):
    _write_agent(home_agents, "user-only.md", "---\nname: user-only\nmodel: opus\n---\n")
    assert _allows(_agent_call(subagent_type="user-only"), scopes)


def test_unreadable_name_in_project_scope_voids_lower_scope_pins(scopes, home_agents):
    """高优先级作用域里有读不出真名的定义时，不再采信低优先级作用域的 pin。

    退回文件名占位只在 stem 恰好等于真名时才堵得住；stem 不等于真名时，真名
    漏占位，用户级同名已钉定义就会顶上来放行，而实际跑的是项目级这份没钉
    模型的定义。宁可误拦。
    """
    _write_agent(
        scopes / ".claude" / "agents", "aaa.md", "---\nname: >-\n  helper\nmodel: opus\n---\n"
    )
    _write_agent(home_agents, "helper.md", "---\nname: helper\nmodel: opus\n---\n")
    assert not _allows(_agent_call(subagent_type="helper"), scopes)


# --------------------------------------------------------- fail-open 的爆炸半径


def test_non_dict_payload_fails_open(scopes):
    assert _allows({"tool_name": "Agent", "tool_input": "oops"}, scopes)
    assert hook.evaluate("not a dict at all", scopes) is None


@pytest.mark.parametrize("scope", ["project", "home"])
def test_one_undecodable_definition_does_not_disable_the_gate(scopes, home_agents, scope):
    """一份定义读坏只让它自己不算数，绝不能升级成整道门静默关闭。

    角色定义正文是中文，任何一份被存成 GBK，`read_text(encoding="utf-8")` 抛的是
    `UnicodeDecodeError`（`ValueError`，不是 `OSError`）。它若逃出 per-file 容错、
    冒到顶层兜底，所有不带 model 的调用就全部放行，而且 stdout/stderr 皆空、
    退出码为 0 —— 零信号，没人会发现门已经不在了。
    """
    target = scopes / ".claude" / "agents" if scope == "project" else home_agents
    target.mkdir(parents=True, exist_ok=True)
    (target / "broken.md").write_bytes(
        "---\nname: broken\nmodel: opus\ndescription: 中文说明\n---\n".encode("gbk")
    )
    assert not _allows(
        _agent_call(subagent_type="general-purpose"), scopes
    ), "一份非 UTF-8 的角色定义不得让整道门失效"


# ------------------------------------------------------------------ 拦截文案


def test_deny_reason_carries_the_tiering_criteria(scopes):
    """被拦时要给得出判据，否则主 agent 只会随便补一个 model 了事。"""
    reason = hook.evaluate(_agent_call(prompt="x"), ROOT)
    assert reason
    for token in ("opus", "sonnet", "haiku", "CLAUDE.md", *REPO_PINNED_ROLES):
        assert token in reason, f"拦截文案缺少判据：{token}"


# ----------------------------------------------------- 进程契约（少量子进程用例）
#
# 只有这一小节真的起解释器：契约是「stdin 收 JSON、stdout 回 JSON、始终以 0 退出」，
# 这三件事非跨进程测不出来。判定矩阵在上面已经进程内跑完，不必在这里重复。


def _run_hook(stdin_text: str, project_dir: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=stdin_text,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def test_process_contract_allow_is_silent_and_zero(scopes):
    """放行 = 退出码 0 且 stdout 为空。"""
    proc = _run_hook(json.dumps(_agent_call(model="haiku")), scopes)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_process_contract_deny_shape(scopes):
    """拦截 = 退出码仍为 0，靠 stdout 的 JSON 表达。

    退出码必须是 0：PreToolUse 的退出码 2 语义是「拦截」并把 stderr 当理由，
    真让脚本非零退出，用户看到的会是一段裸 Python 报错。
    """
    proc = _run_hook(json.dumps(_agent_call(prompt="x")), scopes)
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)["hookSpecificOutput"]
    assert payload["hookEventName"] == "PreToolUse"
    assert payload["permissionDecision"] == "deny"
    assert payload["permissionDecisionReason"].strip()


def test_process_contract_malformed_stdin_fails_open(scopes):
    """守卫是规范守卫不是安全边界：自身读不懂输入时放行，不堵死用户。"""
    proc = _run_hook("not json at all", scopes)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_process_contract_reason_is_utf8_not_escaped(scopes):
    """理由是给人看的中文，不能被 JSON 转义成 \\uXXXX。"""
    proc = _run_hook(json.dumps(_agent_call(prompt="x")), scopes)
    assert "判断力" in proc.stdout
