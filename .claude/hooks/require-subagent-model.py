#!/usr/bin/env python3
"""PreToolUse 硬门：起子代理必须显式选模型，不得默认继承主 agent。

Claude Code 的 `Agent` 工具在不传 `model` 时会继承主 agent 的模型，于是一个
只需要 haiku 的检索任务和一个需要 opus 的评审任务花同样的钱、拿同样的判断力。
本门把「选模型」从可选项变成必答题，判据写在 `CLAUDE.md`「子代理规范」。

放行条件（任一成立即放行）：

1. `tool_input.model` 已显式给出；
2. `subagent_type` 命中仓库/用户的 `.claude/agents/` 定义，且该定义的
   frontmatter 钉了具体模型（`model: inherit` 不算钉，视同未选）；
3. `subagent_type == "fork"` —— fork 语义上必须继承父模型，传 `model` 无效。

否则 deny，并把分层判据回给主 agent，让它补一个 `model` 重发。

失败策略是 **fail-open**：任何内部异常（JSON 读坏、agents 目录读不了）都放行。
这是规范守卫不是安全边界，不该因为自己的 bug 把用户的子代理全堵死。

契约：stdin 收 PreToolUse 事件 JSON，stdout 回 `hookSpecificOutput`。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# 工具在 2.x 里叫 `Agent`，历史别名 `Task` 仍被运行时接受，两个都要认。
SUBAGENT_TOOLS = {"Agent", "Task"}

# fork 继承父模型是语义要求，不是偷懒（见 Agent 工具说明）。
MODEL_EXEMPT_SUBAGENT_TYPES = {"fork"}

DENY_REASON = """本仓库规范：起子代理必须显式选模型，不得默认继承主 agent（见 CLAUDE.md「子代理规范」）。

请给这次 Agent 调用补一个 `model`，按**任务需要多少判断力**选，不是按任务大小：

- `opus`   —— 需要判断力：写实现计划、规格/代码评审、架构取舍、疑难 bug 归因、
              安全审查，以及任何「要能推翻既有结论」的活。
- `sonnet` —— 规格已定死的转录型实现：计划写明了改哪些文件怎么改、机械重构、
              补测试、文档同步、照既定模式扩展。
- `haiku`  —— 纯检索定位清点：找文件、列符号、grep 汇总，只需汇报不需推理。

拿不准就上 `opus`：返工一次的成本远高于模型差价。

或者改用已钉好模型的仓库角色（`subagent_type`），无需再传 `model`：
{roster}"""


def _iter_agent_files(root: Path):
    """列出一个 .claude/agents 目录下的所有定义（含子目录）。"""
    agents_dir = root / ".claude" / "agents"
    if not agents_dir.is_dir():
        return
    yield from sorted(agents_dir.rglob("*.md"))


def _parse_frontmatter(path: Path) -> dict[str, str]:
    """极简 YAML frontmatter 读取：只取顶层 `key: value` 标量。

    不引第三方 YAML 依赖——hook 要在任何解释器下都能跑，而我们只需要
    `name` 和 `model` 两个标量字段。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    lines = text.splitlines()
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if not line or line[0].isspace() or ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip().strip("'\"")
    return fields


def _pinned_agent_models(project_dir: Path) -> dict[str, str]:
    """{subagent_type: model}，只收 frontmatter 钉了具体模型的定义。

    `inherit` 被刻意排除：它就是「跟主 agent 走」，正是本门要拦的行为。

    作用域优先级必须跟 Claude Code 一致——项目级 `.claude/agents/` 压过用户级
    `~/.claude/agents/`。所以名字**一出现就占位**，不管它钉没钉模型：否则
    「项目级同名定义没钉模型 + 用户级同名定义钉了模型」会让本门拿用户级那条
    放行，而真正跑起来的是项目级那条、模型仍然继承主 agent，硬门被绕过。
    """
    pinned: dict[str, str] = {}
    claimed: set[str] = set()
    for root in (project_dir, Path.home()):
        for path in _iter_agent_files(root):
            fields = _parse_frontmatter(path)
            name = fields.get("name") or path.stem
            if name in claimed:
                continue
            claimed.add(name)
            model = fields.get("model", "")
            if model and model != "inherit":
                pinned[name] = model
    return pinned


def _allow() -> None:
    sys.exit(0)


def _deny(reason: str) -> None:
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
        ensure_ascii=False,
    )
    sys.exit(0)


def main() -> None:
    payload = json.load(sys.stdin)

    if payload.get("tool_name") not in SUBAGENT_TOOLS:
        _allow()

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        _allow()

    if str(tool_input.get("model") or "").strip():
        _allow()

    subagent_type = str(tool_input.get("subagent_type") or "").strip()
    if subagent_type in MODEL_EXEMPT_SUBAGENT_TYPES:
        _allow()

    project_dir = Path(
        os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or "."
    )
    pinned = _pinned_agent_models(project_dir)
    if subagent_type in pinned:
        _allow()

    roster = (
        "\n".join(f"  - {name} → {model}" for name, model in sorted(pinned.items()))
        or "  （当前没有已钉模型的角色定义）"
    )
    _deny(DENY_REASON.format(roster=roster))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 —— fail-open，见模块 docstring
        sys.exit(0)
