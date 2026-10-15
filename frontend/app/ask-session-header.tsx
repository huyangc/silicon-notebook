"use client";

import { MessageSquareText, Plus } from "lucide-react";


type AskSessionHeaderActionsProps = {
  sessionCount: number;
  sessionPanelOpen: boolean;
  onToggleSessionPanel: () => void;
  onStartNewSession: () => void;
  /**
   * 收起来源栏后历史会话与「新会话」都平铺在左栏;页面上同一时刻只留一处会话入口,
   * 这里的两颗按钮随之隐藏。
   */
  showSessionEntries?: boolean;
};


export function AskSessionHeaderActions({
  sessionCount,
  sessionPanelOpen,
  onToggleSessionPanel,
  onStartNewSession,
  showSessionEntries = true,
}: AskSessionHeaderActionsProps) {
  if (!showSessionEntries) return null;
  return (
    <>
      <button
        className={`chat-session-toggle ${sessionPanelOpen ? "active" : ""}`}
        type="button"
        aria-expanded={sessionPanelOpen}
        aria-controls="ask-session-manager"
        onClick={onToggleSessionPanel}
      >
        <MessageSquareText size={15} />
        历史 {sessionCount}
      </button>
      <button
        className="icon-button compact"
        type="button"
        aria-label="新会话"
        title="新会话"
        onClick={onStartNewSession}
      >
        <Plus size={18} />
      </button>
    </>
  );
}
