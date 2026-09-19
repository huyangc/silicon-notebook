import { Bot, Check, Loader2, MessageSquare, Pencil, Plus, Trash2, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { conversationTitleLimitHint } from "../ask-api.ts";
import { toUserMessage } from "../errors.ts";
import { deleteGlobalConversation, renameGlobalConversation, type GlobalConversation } from "../global-ask-api.ts";

export function GlobalConversationList({ items, activeId, disabled, more, loadingMore, error, onLoadMore, onOpen, onNew, onUpdated, onDeleted }: {
  items: GlobalConversation[]; activeId: string; disabled: boolean; more: boolean;
  loadingMore: boolean; error: string; onLoadMore: () => void;
  onOpen: (id: string) => void; onNew: () => void;
  onUpdated: (item: GlobalConversation) => void; onDeleted: (id: string) => void;
}) {
  const [editing, setEditing] = useState("");
  const [title, setTitle] = useState("");
  const [confirmId, setConfirmId] = useState("");
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState<{ id: string; text: string } | null>(null);
  const pending = useRef(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => setNotice(null), 4000);
    return () => clearTimeout(timer);
  }, [notice]);

  async function mutate(item: GlobalConversation, action: "rename" | "delete") {
    if (pending.current || disabled) return;
    const hint = action === "rename" ? conversationTitleLimitHint(title.trim()) : null;
    if (hint || (action === "rename" && !title.trim())) { setNotice({ id: item.id, text: hint || "请填写对话名称" }); return; }
    pending.current = true;
    setBusy(item.id);
    try {
      if (action === "rename") {
        const changed = await renameGlobalConversation(item.id, title.trim());
        if (!mounted.current) return;
        onUpdated(changed);
        setEditing("");
        setNotice({ id: item.id, text: "名称已更新" });
      } else {
        await deleteGlobalConversation(item.id);
        if (!mounted.current) return;
        onDeleted(item.id);
        setConfirmId("");
      }
    } catch (cause) {
      if (mounted.current) setNotice({ id: item.id, text: toUserMessage(cause, "操作失败，请重试") });
    } finally {
      pending.current = false;
      if (mounted.current) setBusy("");
    }
  }

  return <aside className="global-history" aria-label="全局对话历史">
    <button className="new-pill global-new" type="button" disabled={disabled} onClick={onNew}><Plus size={17} />新建对话</button>
    <div className="global-history-heading"><span>最近对话</span><small>{items.length}{more ? "+" : ""}</small></div>
    <div className="global-history-items">
      {!items.length && <div className="global-history-empty"><MessageSquare size={22} /><p>从一个问题开始</p><small>对话会自动保存在这里</small></div>}
      {items.map((item) => <article key={item.id} className={`global-history-row${activeId === item.id ? " active" : ""}`}>
        {editing === item.id ? <form className="global-history-edit" onSubmit={(event) => { event.preventDefault(); void mutate(item, "rename"); }}>
          <input autoFocus aria-label="对话名称" value={title} disabled={Boolean(busy)} onChange={(event) => setTitle(event.target.value)} />
          <button type="submit" className="icon-button" aria-label={busy === item.id ? "正在保存名称" : "保存名称"} disabled={Boolean(busy) || disabled}>{busy === item.id ? <Loader2 className="global-spin" size={15} /> : <Check size={15} />}</button>
          <button type="button" className="icon-button" aria-label="取消重命名" disabled={Boolean(busy)} onClick={() => setEditing("")}><X size={15} /></button>
        </form> : <>
          <button className="global-history-open" type="button" aria-current={activeId === item.id ? "page" : undefined} disabled={disabled} onClick={() => onOpen(item.id)}>
            {item.submitted_via === "mcp" ? <Bot size={15} /> : <MessageSquare size={15} />}<span>{item.title || "未命名对话"}</span>
          </button>
          <div className="global-history-actions">
            <button type="button" className="icon-button" aria-label={`重命名 ${item.title}`} disabled={Boolean(busy) || disabled} onClick={() => { setEditing(item.id); setTitle(item.title); setConfirmId(""); }}><Pencil size={13} /></button>
            <button type="button" className="icon-button" aria-label={`删除 ${item.title}`} disabled={Boolean(busy) || disabled} onClick={() => { setConfirmId(item.id); setEditing(""); }}><Trash2 size={13} /></button>
          </div>
        </>}
        {confirmId === item.id && <div className="global-history-confirm"><span>删除此对话？</span><button className="global-text-button" type="button" disabled={Boolean(busy) || disabled} onClick={() => { void mutate(item, "delete"); }}>{busy === item.id ? "删除中…" : "确认删除"}</button><button className="global-text-button" type="button" disabled={Boolean(busy)} onClick={() => setConfirmId("")}>取消</button></div>}
        {notice?.id === item.id && <p className="global-history-notice" role="status">{notice.text}</p>}
      </article>)}
      {more && <button type="button" className="global-history-more sort-button" disabled={loadingMore || disabled} onClick={onLoadMore}>{loadingMore ? "正在加载…" : "加载更早的对话"}</button>}
      {error && <p className="global-inline-error" role="alert">{error}<button className="global-text-button" type="button" disabled={loadingMore || disabled} onClick={onLoadMore}>{loadingMore ? "正在加载…" : "重试加载"}</button></p>}
    </div>
    <div className="global-history-foot"><Bot size={19} aria-hidden="true" /><span>网页与 Agent 对话<br /><small>在这里接着聊</small></span></div>
  </aside>;
}
