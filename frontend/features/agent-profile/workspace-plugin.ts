"use client";

import { createElement } from "react";
import { Sparkles } from "lucide-react";

import type { WorkspaceExtensionProps } from "../extension-sdk/contracts.ts";

/**
 * Thin presentation entry; AgentProfilePanel retains all domain I/O/state.
 *
 * 形态是**一颗小按钮**而不是一整行或一张卡片:宿主把它渲染进来源栏的次要工具排
 * (与「补全论文信息」同排、按宽度折行;落点理由写在壳层的挂载点注释里)。复用 `.button
 * .secondary` 的既有按钮语言,通用类 `.workspace-extension-entry` 只把尺寸收成胶囊,
 * 不写任何颜色字面量——视觉必须走系统色板/边框/圆角（见 `docs/development.md` 的
 * 前端架构约束）。按钮就是动作本身(图标 + 文案),不再另配一颗「打开」按钮。
 */
export function AgentProfileWorkspacePanel({ actions }: WorkspaceExtensionProps) {
  return createElement(
    "button",
    {
      type: "button",
      className: "button secondary workspace-extension-entry",
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
