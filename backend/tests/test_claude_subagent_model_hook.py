"""`.claude/hooks/require-subagent-model.py` 的回归网。

这个 hook 是 `CLAUDE.md`「子代理规范」的执行侧：**起子代理必须显式选模型，
不得默认继承主 agent**。它是一个手写的极简 YAML frontmatter 解析器，六轮评审
里被找出五个绕过口，全靠临时脚本照出来——没有committed 测试的话，下一次改动
会把它们静默重新打开。本仓库自己的规矩就是「加了守卫 ≠ 有效，必须变异验证」，
那这份守卫自己也得有网。

用例分两类，缺一不可：

* **绕过**（`deny` 侧）：一个实际会继承父模型的调用被放行。这是最严重的一类，
  也是历次评审的全部真问题所在。
* **误拦**（`allow` 侧）：把合法调用堵死。守卫误拦会让人绕开它，同样致命。

hook 以子进程方式跑真实脚本（不是 import 内部函数），因为契约就是「stdin 收
JSON、stdout 回 JSON」，绕过进程边界测就测不到 shebang、退出码、编码这些真实
失败面。
"""

from __future__ import annotations

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


def _run(payload: dict, *, project_dir: Path | None = None, home: Path | None = None) -> bool:
    """跑一次 hook，返回「是否放行」。"""
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(project_dir if project_dir is not None else ROOT)
    if home is not None:
        env["HOME"] = str(home)
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"hook 必须始终以 0 退出（拦截靠 stdout 的 JSON）；"
        f"非零会被 PreToolUse 当成拦截信号并附上裸报错。"
        f"rc={proc.returncode} stderr={proc.stderr!r}"
    )
    if not proc.stdout.strip():
        return True
    decision = json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"]
    return decision != "deny"


def _agent_call(**tool_input) -> dict:
    return {"tool_name": "Agent", "tool_input": tool_input}


def _write_agent(directory: Path, filename: str, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(body, encoding="utf-8")


@pytest.fixture
def scopes(tmp_path: Path) -> tuple[Path, Path]:
    """一对空的 project / home 作用域，各自带 `.claude/agents/`。"""
    project = tmp_path / "proj"
    home = tmp_path / "home"
    for root in (project, home):
        (root / ".claude" / "agents").mkdir(parents=True)
    return project, home


# --------------------------------------------------------------- 基本闸门语义


def test_call_without_model_is_denied(scopes):
    """头号拦截对象：不带 model 的通用子代理调用。

    这条同时是整份用例的 sanity 基线——它若变绿，说明测试自己打空了，
    后面所有 `allow` 断言都不再有意义。
    """
    project, home = scopes
    assert not _run(
        _agent_call(prompt="x", subagent_type="general-purpose"),
        project_dir=project,
        home=home,
    )


def test_explicit_model_is_allowed(scopes):
    project, home = scopes
    assert _run(
        _agent_call(prompt="x", subagent_type="general-purpose", model="haiku"),
        project_dir=project,
        home=home,
    )


def test_legacy_task_tool_name_is_also_gated(scopes):
    """工具在 2.x 里叫 `Agent`，运行时仍接受历史别名 `Task`。"""
    project, home = scopes
    assert not _run(
        {"tool_name": "Task", "tool_input": {"subagent_type": "Explore"}},
        project_dir=project,
        home=home,
    )


def test_other_tools_are_untouched(scopes):
    project, home = scopes
    assert _run(
        {"tool_name": "Bash", "tool_input": {"command": "ls"}},
        project_dir=project,
        home=home,
    )


def test_explicit_fork_is_exempt(scopes):
    """fork 必须继承父模型，传 model 无效，所以豁免。"""
    project, home = scopes
    assert _run(_agent_call(subagent_type="fork"), project_dir=project, home=home)


def test_omitted_subagent_type_is_not_treated_as_fork(scopes):
    """省略 `subagent_type` 等于默认 general-purpose，不是隐式 fork。

    codex 第 2 轮建议把它当 fork 豁免，被驳回：那正是本门的头号拦截对象，
    照做等于把门整个掏空。这条钉住那个决定。
    """
    project, home = scopes
    assert not _run(_agent_call(prompt="x"), project_dir=project, home=home)


# ----------------------------------------------------- model 值：什么不算「选择」


@pytest.mark.parametrize(
    "model",
    ["inherit", "INHERIT", "  inherit  ", "", "   ", "null", "none", "~"],
)
def test_non_choice_model_values_are_denied(scopes, model):
    """填了但没做选择，等同于没填。

    `inherit` 保留的正是本门要禁的继承语义；YAML 的三种空值写法同理。
    """
    project, home = scopes
    assert not _run(_agent_call(model=model), project_dir=project, home=home)


@pytest.mark.parametrize("model", ["opus", "  opus  ", "sonnet", "haiku"])
def test_real_model_values_are_allowed(scopes, model):
    project, home = scopes
    assert _run(_agent_call(model=model), project_dir=project, home=home)


# ------------------------------------------------------ 角色定义：钉没钉住模型


def test_repo_roles_are_pinned_and_need_no_model():
    """仓库自带的三个角色不必再传 model —— 模型钉在定义里。

    走真实仓库目录，所以这条同时守住「有人把 impl-task 的 model 删了或改成
    inherit」这类改动。
    """
    for role in REPO_PINNED_ROLES:
        assert _run(_agent_call(subagent_type=role)), f"{role} 应免 model 放行"


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
    project, home = scopes
    _write_agent(
        project / ".claude" / "agents",
        "role.md",
        f"---\nname: role\n{model_line}\n---\n正文\n",
    )
    assert not _run(_agent_call(subagent_type="role"), project_dir=project, home=home)


@pytest.mark.parametrize(
    "model_line",
    ["model: opus", 'model: "sonnet"', "model: sonnet   # 转录型够用", 'model: "opus" # 带注释'],
)
def test_definition_with_a_real_pin_is_allowed(scopes, model_line):
    project, home = scopes
    _write_agent(
        project / ".claude" / "agents",
        "role.md",
        f"---\nname: role\n{model_line}\n---\n正文\n",
    )
    assert _run(_agent_call(subagent_type="role"), project_dir=project, home=home)


def test_definition_is_keyed_by_frontmatter_name_not_filename(scopes):
    """`name` 是真名，文件名不是；嵌套子目录也要能发现。"""
    project, home = scopes
    _write_agent(
        project / ".claude" / "agents" / "nested" / "deep",
        "whatever.md",
        "---\nname: renamed-role\nmodel: opus\n---\n正文\n",
    )
    assert _run(_agent_call(subagent_type="renamed-role"), project_dir=project, home=home)
    assert not _run(_agent_call(subagent_type="whatever"), project_dir=project, home=home)


def test_unknown_subagent_type_is_denied(scopes):
    project, home = scopes
    assert not _run(
        _agent_call(subagent_type="not-a-real-agent"), project_dir=project, home=home
    )


# ------------------------------------------------------------- 作用域优先级


def test_project_scope_shadows_a_pinned_user_definition(scopes):
    """项目级定义压过用户级同名定义 —— 与 Claude Code 的解析顺序一致。

    项目级没钉模型时必须拦：真正跑起来的是项目级那条，仍然继承主 agent。
    只按「钉了模型才记名字」来收集，就会拿用户级那条放行。
    """
    project, home = scopes
    _write_agent(
        project / ".claude" / "agents", "collide.md", "---\nname: collide\nmodel: inherit\n---\n"
    )
    _write_agent(
        home / ".claude" / "agents", "collide.md", "---\nname: collide\nmodel: opus\n---\n"
    )
    assert not _run(_agent_call(subagent_type="collide"), project_dir=project, home=home)


def test_user_scope_pin_is_honored_when_project_has_no_such_role(scopes):
    project, home = scopes
    _write_agent(
        home / ".claude" / "agents", "user-only.md", "---\nname: user-only\nmodel: opus\n---\n"
    )
    assert _run(_agent_call(subagent_type="user-only"), project_dir=project, home=home)


def test_unreadable_name_in_project_scope_voids_lower_scope_pins(scopes):
    """高优先级作用域里有读不出真名的定义时，不再采信低优先级作用域的 pin。

    退回文件名占位只在 stem 恰好等于真名时才堵得住；stem 不等于真名时，真名
    漏占位，用户级同名已钉定义就会顶上来放行，而实际跑的是项目级这份没钉
    模型的定义。宁可误拦。
    """
    project, home = scopes
    _write_agent(
        project / ".claude" / "agents",
        "aaa.md",
        "---\nname: >-\n  helper\nmodel: opus\n---\n",
    )
    _write_agent(
        home / ".claude" / "agents", "helper.md", "---\nname: helper\nmodel: opus\n---\n"
    )
    assert not _run(_agent_call(subagent_type="helper"), project_dir=project, home=home)


# --------------------------------------------------------- fail-open 的爆炸半径


def test_malformed_event_json_fails_open(scopes):
    """守卫是规范守卫不是安全边界：自身读不懂输入时放行，不堵死用户。"""
    project, _ = scopes
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(project)
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input="not json at all",
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0
    assert not proc.stdout.strip()


def test_non_dict_tool_input_fails_open(scopes):
    project, home = scopes
    assert _run({"tool_name": "Agent", "tool_input": "oops"}, project_dir=project, home=home)


@pytest.mark.parametrize("scope", ["project", "home"])
def test_one_undecodable_definition_does_not_disable_the_gate(scopes, scope):
    """一份定义读坏只让它自己不算数，绝不能升级成整道门静默关闭。

    角色定义正文是中文，任何一份被存成 GBK，`read_text(encoding="utf-8")` 抛的是
    `UnicodeDecodeError`（`ValueError`，不是 `OSError`）。它若逃出 per-file 容错、
    冒到顶层兜底，所有不带 model 的调用就全部放行，而且 stdout/stderr 皆空、
    退出码为 0 —— 零信号，没人会发现门已经不在了。
    """
    project, home = scopes
    target = (project if scope == "project" else home) / ".claude" / "agents"
    target.mkdir(parents=True, exist_ok=True)
    (target / "broken.md").write_bytes(
        "---\nname: broken\nmodel: opus\ndescription: 中文说明\n---\n".encode("gbk")
    )
    assert not _run(
        _agent_call(subagent_type="general-purpose"), project_dir=project, home=home
    ), "一份非 UTF-8 的角色定义不得让整道门失效"


# ------------------------------------------------------------------ 拦截文案


def test_deny_reason_carries_the_tiering_criteria(scopes):
    """被拦时要给得出判据，否则主 agent 只会随便补一个 model 了事。"""
    project, home = scopes
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(ROOT)
    env["HOME"] = str(home)
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(_agent_call(prompt="x")),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    reason = json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    for token in ("opus", "sonnet", "haiku", "CLAUDE.md", *REPO_PINNED_ROLES):
        assert token in reason, f"拦截文案缺少判据：{token}"
