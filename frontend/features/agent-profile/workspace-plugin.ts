"use client";

import { createElement } from "react";
import { Sparkles } from "lucide-react";

import type { WorkspaceExtensionProps } from "../extension-sdk/contracts.ts";

/**
 * Thin presentation entry; AgentProfilePanel retains all domain I/O/state.
 *
 * 形态是**一行入口**而不是一张卡片:宿主把它渲染进来源栏的固定区(落点理由写在
 * 壳层的挂载点注释里),旁边是「添加来源」「整理知识图谱」这一排同级动作。复用 `.button
 * .secondary` 的既有按钮语言,自己那条类只补一行式排版,不写任何颜色字面量——
 * 视觉必须走系统色板/边框/圆角(CLAUDE.md「视觉必须复用系统现有色板…」)。
 * 整行就是动作本身(图标 + 文案),不再另配一颗「打开」按钮:一行里两个可点目标
 * 在 270px 宽的来源栏里只会互相挤。
 */
export function AgentProfileWorkspacePanel({ actions }: WorkspaceExtensionProps) {
  return createElement(
    "button",
    {
      type: "button",
      className: "button secondary agent-profile-workspace-plugin",
      // 可见文案与 aria-label 同字:辅助技术读到的名字就是屏幕上那一行,窄栏下
      // 文案被省略号截断时也仍然完整。
      "aria-label": "AI 对这个库的理解",
      title: "查看并修改 AI 对这个库形成的理解与你的检索心得",
      onClick: () => actions.openUnderstanding(),
    },
    createElement(Sparkles, { size: 15, "aria-hidden": "true" }),
    createElement("span", null, "AI 对这个库的理解"),
  );
}
