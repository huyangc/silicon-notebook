import { Check, Edit3, Share2, Trash2, X } from "lucide-react";

import { formatRelativeTime } from "./relative-time.ts";
import { Pagination } from "./Pagination";
import { useClientPagination } from "./use-client-pagination.ts";
import type { ConversationSummary } from "./workspace-model";

/** 「其余清单」共用的页大小(20)——接口整份返回、契约上不分页,分页只发生在界面。 */
const ASK_SESSION_HISTORY_PAGE_SIZE = 20;

/**
 * Ask 会话管理弹窗里的历史会话清单 + 分页。单独抽成组件,让分页状态随弹窗一起
 * 挂载/卸载——弹窗只在 `chatMode === "ask" && sessionPanelOpen` 时才渲染这个组件,
 * 关闭时它从树上卸载、状态随之丢弃,下次重新打开自然落在第一页,不需要额外的
 * resetKey。
 *
 * 单独成文件是 Next.js App Router 的硬约束:`app/page.tsx` 是路由文件,只能有
 * `default` 等少数几个白名单导出,多导出一个具名组件会让 `next build` 的类型检查
 * (`.next/types/app/page.ts`)报错——这个组件需要被组件测试直接渲染,只能挪出来。
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
}) {
  const page = useClientPagination(sessions, ASK_SESSION_HISTORY_PAGE_SIZE);
  if (sessions.length === 0) return <div className="chat-session-empty">还没有历史会话。</div>;
  return (
    <>
      {page.pageItems.map((session) => (
        <article className={`chat-session-card ${session.id === conversationId ? "active" : ""}`} key={session.id}>
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
              <button className="chat-session-card-main" type="button" onClick={() => onOpenSession(session.id)}>
                <span>{session.title || "未命名会话"}</span>
                <small>
                  {formatRelativeTime(session.updated_at)} · {session.turn_count} 轮
                  {session.used_reasoning && <span className="chat-session-reasoning-badge">{`✦ ${strictLabel}`}</span>}
                </small>
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
      <Pagination page={page.page} pageSize={ASK_SESSION_HISTORY_PAGE_SIZE} total={page.total} onPage={page.setPage} label="历史会话分页" />
    </>
  );
}
