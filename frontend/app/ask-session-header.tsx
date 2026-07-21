"use client";

import { MessageSquareText, Plus } from "lucide-react";


type AskSessionHeaderActionsProps = {
  sessionCount: number;
  sessionPanelOpen: boolean;
  onToggleSessionPanel: () => void;
  onStartNewSession: () => void;
};


export function AskSessionHeaderActions({
  sessionCount,
  sessionPanelOpen,
  onToggleSessionPanel,
  onStartNewSession,
}: AskSessionHeaderActionsProps) {
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
