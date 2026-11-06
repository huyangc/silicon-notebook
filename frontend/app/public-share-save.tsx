"use client";

import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ArrowUpRight, BookOpen, Check, ChevronDown, FileText, Loader2, MessageSquare, Plus, X } from "lucide-react";
import { getToken } from "./auth-session";
import { signInLocation } from "./auth-return-location";
import { notebookHash } from "./memory-model";
import { requestJson } from "./api-client";
import { toUserMessage, httpErrorStatus } from "./errors";
import { FloatingModalCard } from "./floating-modal-card";
import { useDialogFocus } from "./use-dialog-focus";
import { useRootModalCoordinator, type RootModalCloseReason, type RootModalView } from "./use-root-modal-coordinator";
import type { NotebookSummary } from "./workspace-model";
import { savePublicShare, type PublicShareKind, type PublicShareSaveResult } from "./public-share-save-api";

type PublicShareSaveProps = {
  kind: PublicShareKind; token: string; title: string; expectedRevision: string;
};

export function PublicShareSave(props: PublicShareSaveProps) {
  const [result, setResult] = useState<PublicShareSaveResult | null>(null);
  const [saved, setSaved] = useState(false);
  // Public pages have no workspace; their single save dialog belongs to this reader.
  const rootModals = useRootModalCoordinator({ actorId: "public-share-reader", sourceId: null, onClosed: () => {} });
  const modalView = rootModals.view("public-share-save");

  function signInToSave() {
    const url = new URL(window.location.href);
    url.searchParams.set("save", "all");
    window.history.replaceState(null, "", url);
    window.location.assign(signInLocation());
  }

  function begin() {
    if (!getToken()) { signInToSave(); return; }
    setSaved(false);
    rootModals.open("public-share-save", rootModals.captureActorOwner());
  }

  useEffect(() => {
    if (!saved) return;
    const timer = window.setTimeout(() => setSaved(false), 1800);
    return () => window.clearTimeout(timer);
  }, [saved]);

  useEffect(() => {
    const url = new URL(window.location.href);
    if (!url.searchParams.has("save") || !getToken()) return;
    url.searchParams.delete("save"); window.history.replaceState(null, "", url);
    begin();
    // Restore the page-level save flow once after authentication, including old turn-specific return URLs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return <div className="public-share-save">
    <div className="public-share-save-actions">
      <button type="button" className="sort-button public-share-save-trigger" disabled={modalView.open} onClick={begin}>
        {saved ? <Check size={16} aria-hidden="true" /> : <BookOpen size={16} aria-hidden="true" />}
        {saved ? "已保存" : "保存到我的笔记本"}
      </button>
      {result && <a className="link-button inline-action public-share-saved-link" href={`/${notebookHash(result.notebook_id, result.source_id)}`}>
        打开笔记本 <ArrowUpRight size={14} aria-hidden="true" />
      </a>}
    </div>
    <p role="status" className="public-share-save-status">{saved ? result?.reused ? "此内容已保存。" : "已保存，内容将在笔记本中处理。" : ""}</p>
    {modalView.open && createPortal(<PublicShareSaveDialog {...props} view={modalView} onSignIn={signInToSave}
      onClose={(reason) => rootModals.requestClose("public-share-save", reason)}
      onSaved={(value) => {
        setResult(value); setSaved(true);
        rootModals.requestClose("public-share-save", "button");
      }} />, document.body)}
  </div>;
}

function PublicShareSaveDialog({ kind, token, title: initialTitle, expectedRevision, view, onClose, onSaved, onSignIn }: PublicShareSaveProps & {
  view: RootModalView;
  onClose: (reason: Extract<RootModalCloseReason, "button" | "backdrop" | "escape">) => void;
  onSaved: (result: PublicShareSaveResult) => void;
  onSignIn: () => void;
}) {
  const id = useId();
  const dialogRef = useRef<HTMLElement>(null);
  const [notebooks, setNotebooks] = useState<NotebookSummary[]>([]);
  const [notebookId, setNotebookId] = useState("");
  const [newName, setNewName] = useState("");
  const [title, setTitle] = useState(initialTitle);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const flight = useRef(false);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useDialogFocus({ containerRef: dialogRef, active: view.topmost, initialFocusRef: dialogRef,
    onEscape: () => { if (!flight.current) onClose("escape"); } });

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setLoadError("");
    void requestJson<NotebookSummary[]>("/notebooks", { tag: "public-share-save", unauthorized: "preserve", signal: controller.signal })
      .then((rows) => {
        if (controller.signal.aborted) return;
        const owned = rows.filter((row) => row.access === "owner");
        setNotebooks(owned); setNotebookId(owned[0]?.id || "new");
      }).catch((cause) => {
        if (controller.signal.aborted) return;
        if (httpErrorStatus(cause) === 401) { onSignIn(); return; }
        setLoadError(toUserMessage(cause, "笔记本加载失败，请重试。"));
      }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
    // Only explicit retries reload the notebook choices.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [retry]);

  async function save() {
    if (flight.current || loading || loadError || !title.trim() || !notebookId || (notebookId === "new" && !newName.trim())) return;
    flight.current = true; setBusy(true); setError("");
    try {
      let target = notebookId;
      if (target === "new") {
        const notebook = await requestJson<NotebookSummary>("/notebooks", {
          tag: "public-share-save", unauthorized: "preserve", method: "POST", body: JSON.stringify({ name: newName.trim() }),
        });
        target = notebook.id;
        if (!alive.current) return;
        setNotebookId(target); setNotebooks((rows) => [...rows, notebook]);
      }
      const result = await savePublicShare(kind, token, { notebook_id: target, title: title.trim(), expected_revision: expectedRevision });
      if (alive.current) onSaved(result);
    } catch (cause) {
      if (!alive.current) return;
      if (httpErrorStatus(cause) === 401) { onSignIn(); return; }
      setError(toUserMessage(cause, "保存失败，请重试。"));
    } finally {
      flight.current = false;
      if (alive.current) setBusy(false);
    }
  }

  const ContentIcon = kind === "conversations" ? MessageSquare : FileText;
  return <section ref={dialogRef} tabIndex={-1} className="utility-modal public-share-save-modal" role="dialog" aria-modal={view.topmost}
    aria-labelledby={`${id}-heading`} aria-describedby={`${id}-description`} aria-hidden={!view.topmost}
    inert={view.topmost ? undefined : true} style={{ zIndex: view.zIndex }}
    onClick={(event) => { if (event.target === event.currentTarget && !flight.current) onClose("backdrop"); }}>
    <FloatingModalCard storageKey="publicShareSave.window" className="utility-modal-card narrow public-share-save-card">
      {(floating) => <>
        <div className="source-modal-header" {...floating.dragHandleProps}>
          <div>
            <h2 id={`${id}-heading`}>保存到我的笔记本</h2>
            <p id={`${id}-description`}>将分享内容留在自己的资料中，随时阅读和引用。</p>
          </div>
          <button type="button" className="icon-button" disabled={busy} aria-label="关闭" onClick={() => onClose("button")}><X size={18} aria-hidden="true" /></button>
        </div>
        <form id={`${id}-form`} className="edit-form public-share-save-body" onSubmit={(event) => { event.preventDefault(); void save(); }}>
          <div className="public-share-save-preview">
            <span className="public-share-save-content-icon"><ContentIcon size={20} aria-hidden="true" /></span>
            <div><span className="public-share-save-content-type">{kind === "conversations" ? "分享问答 · 全部公开轮次" : "分享报告 · 完整公开正文"}</span>
              <p>{initialTitle}</p></div>
          </div>
          {loading ? <div className="public-share-save-loading" role="status"><Loader2 className="busy-spin" size={18} aria-hidden="true" />正在加载笔记本…</div>
            : loadError ? <p className="public-share-save-error" role="alert">{loadError} <button type="button" className="link-button inline-action" onClick={() => setRetry((value) => value + 1)}>重新加载</button></p>
              : <>
                <div className="public-share-save-field">
                  <div className="public-share-save-field-heading"><label htmlFor={`${id}-notebook`}>目标笔记本</label>
                    {notebookId !== "new" && <button type="button" className="link-button inline-action" disabled={busy} onClick={() => setNotebookId("new")}><Plus size={14} aria-hidden="true" />新建笔记本</button>}</div>
                  <div className="public-share-save-select"><BookOpen size={17} aria-hidden="true" />
                    <select id={`${id}-notebook`} value={notebookId} disabled={busy} onChange={(event) => setNotebookId(event.target.value)}>
                      {notebooks.map((row) => <option key={row.id} value={row.id}>{row.name}</option>)}
                      <option value="new">新建笔记本</option>
                    </select><ChevronDown size={16} aria-hidden="true" />
                  </div>
                  {notebooks.length === 0 && <p className="public-share-save-hint">还没有自己的笔记本，为这份内容建一本吧。</p>}
                </div>
                {notebookId === "new" && <label>笔记本名称<input value={newName} disabled={busy} placeholder="为新笔记本起个名字" onChange={(event) => setNewName(event.target.value)} /></label>}
                <label>保存标题<input value={title} disabled={busy} onChange={(event) => setTitle(event.target.value)} /></label>
              </>}
          <p className="public-share-save-hint">正文、引用原文及图片将一起保存。副本独立保留，分享链接失效后仍可阅读。</p>
          {error && <p role="alert" className="public-share-save-error">{error}</p>}
        </form>
        <div className="modal-actions padded">
          <button type="button" className="sort-button" disabled={busy} onClick={() => onClose("button")}>取消</button>
          <button type="submit" form={`${id}-form`} className="new-pill public-share-save-submit" disabled={loading || !!loadError || busy || !title.trim() || !notebookId || (notebookId === "new" && !newName.trim())}>
            {busy ? <Loader2 className="busy-spin" size={16} aria-hidden="true" /> : <BookOpen size={16} aria-hidden="true" />}{busy ? "保存中…" : "确认保存"}
          </button>
        </div>
      </>}
    </FloatingModalCard>
  </section>;
}
