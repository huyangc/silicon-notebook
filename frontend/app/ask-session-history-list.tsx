import { Check, Edit3, FileText, Share2, Trash2, X } from "lucide-react";

import { linkedReportLabel } from "./linked-report.ts";
import { formatRelativeTime } from "./relative-time.ts";
import { Pagination } from "./Pagination";
import { useClientPagination } from "./use-client-pagination.ts";
import type { ConversationSummary } from "./workspace-model";

/** 会话清单整份返回,界面每页显示的会话数。 */
const ASK_SESSION_HISTORY_PAGE_SIZE = 20;

/**
 * 历史会话清单 + 分页。两处落点,page.tsx 同一时刻只挂其中一处:
 * - `grid`(默认):「历史」弹窗里的卡片网格,只在 `sessionPanelOpen` 时渲染;
 * - `sidebar`:收起来源栏后左侧平铺的会话栏,一行一个标题,操作键悬停/聚焦时出现。
 * 两处都是关闭(收起)即卸载,所以每次重新打开都从第一页开始,不需要 resetKey。
 */
export function AskSessionHistoryList({
  sessions,
  conversationId,
  renamingSessionId,
  sessionTitleDraft,
  sessionTitleOverLimit,
  strictLabel,
  onUpdateTitleDraft,
  onCommitRename,
  onCancelRename,
  onOpenSession,
  onBeginRename,
  onShare,
  onDelete,
  variant = "grid",
}: {
  sessions: ConversationSummary[];
  conversationId: string | null;
  renamingSessionId: string | null;
  sessionTitleDraft: string;
  sessionTitleOverLimit: string | null;
  strictLabel: string;
  onUpdateTitleDraft: (value: string) => void;
  onCommitRename: (id: string) => void;
  onCancelRename: () => void;
  onOpenSession: (id: string) => void;
  onBeginRename: (session: ConversationSummary) => void;
  onShare: (session: ConversationSummary) => void;
  onDelete: (session: ConversationSummary) => void;
  variant?: "grid" | "sidebar";
}) {
  const sidebar = variant === "sidebar";
  const page = useClientPagination(sessions, ASK_SESSION_HISTORY_PAGE_SIZE);
  if (sessions.length === 0) return <div className="chat-session-empty">还没有历史会话。</div>;
  return (
    <>
      {page.pageItems.map((session) => (
        <article
          className={`${sidebar ? "chat-session-row" : "chat-session-card"} ${session.id === conversationId ? "active" : ""}`}
          key={session.id}
        >
          {renamingSessionId === session.id ? (
            <div className="chat-session-rename">
              {/* 刻意没有 maxLength:那把尺数的是 UTF-16 code unit,与后端
                  Pydantic 的码点口径对不上,而且它是**静默裁剪**——两条都
                  是红线(codex #525 R2/R3)。超限时输入框保持可编辑,用户
                  正要做的就是把它改短。 */}
              <input
                autoFocus
                value={sessionTitleDraft}
                aria-invalid={sessionTitleOverLimit !== null}
                onChange={(event) => onUpdateTitleDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !sessionTitleOverLimit) onCommitRename(session.id);
                  if (event.key === "Escape") onCancelRename();
                }}
              />
              <button type="button" title="保存" disabled={sessionTitleOverLimit !== null} onClick={() => onCommitRename(session.id)}><Check size={15} /></button>
              <button type="button" title="取消" onClick={onCancelRename}><X size={15} /></button>
              {/* 此刻唯一挡着保存键的东西,不写出来用户只看到按钮变灰。 */}
              {sessionTitleOverLimit && (
                <span className="chat-session-rename-hint">{sessionTitleOverLimit}</span>
              )}
            </div>
          ) : (
            <>
              <button
                className="chat-session-card-main"
                type="button"
                aria-current={session.id === conversationId ? "true" : undefined}
                title={sidebar
                  ? [
                    session.title || "未命名会话",
                    formatRelativeTime(session.updated_at),
                    `${session.turn_count} 轮`,
                    ...(session.linked_report ? [linkedReportLabel(session.linked_report.title ?? "")] : []),
                  ].join(" · ")
                  : undefined}
                onClick={() => onOpenSession(session.id)}
              >
                {/* 会话最后一轮挂着关联报告时的标记。左栏一行只放得下标题,标记缩成标题前的
                    小图标,题目进悬停提示;卡片里与「逐步推理」徽标并排。 */}
                <span>
                  {sidebar && session.linked_report && (
                    <FileText size={12} className="chat-session-report-mark" aria-label="关联报告" />
                  )}
                  {session.title || "未命名会话"}
                </span>
                {!sidebar && <small>
                  {formatRelativeTime(session.updated_at)} · {session.turn_count} 轮
                  {session.used_reasoning && <span className="chat-session-reasoning-badge">{`✦ ${strictLabel}`}</span>}
                  {session.linked_report && (
                    <span
                      className="chat-session-report-badge"
                      title={linkedReportLabel(session.linked_report.title ?? "")}
                    >
                      关联报告
                    </span>
                  )}
                </small>}
              </button>
              <div className="chat-session-card-actions">
                <button type="button" title="分享" onClick={() => onShare(session)}><Share2 size={14} /></button>
                <button type="button" title="重命名" onClick={() => onBeginRename(session)}><Edit3 size={14} /></button>
                <button type="button" title="删除" onClick={() => onDelete(session)}><Trash2 size={14} /></button>
              </div>
            </>
          )}
        </article>
      ))}
      <Pagination page={page.page} pageSize={ASK_SESSION_HISTORY_PAGE_SIZE} total={page.total} onPage={page.setPage} label="历史会话分页" compact={sidebar} />
    </>
  );
}
