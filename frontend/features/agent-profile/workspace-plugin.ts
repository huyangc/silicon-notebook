"use client";

import { createElement } from "react";
import { Sparkles } from "lucide-react";

import type { WorkspaceExtensionProps } from "../extension-sdk/contracts.ts";

/** Thin presentation entry; AgentProfilePanel retains all domain I/O/state. */
export function AgentProfileWorkspacePanel({ actions }: WorkspaceExtensionProps) {
  return createElement(
    "section",
    { className: "agent-profile-workspace-plugin", "aria-label": "AI 对这个库的理解" },
    createElement("div", { className: "agent-profile-workspace-plugin-icon", "aria-hidden": "true" },
      createElement(Sparkles, { size: 18 })),
    createElement("div", null,
      createElement("strong", null, "AI 对这个库的理解"),
      createElement("p", null, "查看 AI 已形成的库级理解、依据与检索心得。")),
    createElement("button", {
      type: "button",
      className: "button secondary",
      onClick: () => actions.openUnderstanding(),
    }, "打开理解面板"),
  );
}
