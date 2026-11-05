"use client";

import { useEffect, useId, useRef, useState } from "react";
import { getToken } from "./auth-session";
import { signInLocation } from "./auth-return-location";
import { notebookHash } from "./memory-model";
import { requestJson } from "./api-client";
import { toUserMessage, httpErrorStatus } from "./errors";
import type { NotebookSummary } from "./workspace-model";
import { savePublicShare, type PublicShareKind, type PublicShareSaveResult } from "./public-share-save-api";

export function PublicShareSave({ kind, token, title: initialTitle, turnIndex, expectedRevision }: {
  kind: PublicShareKind; token: string; title: string; turnIndex?: number; expectedRevision: string;
}) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const [notebooks, setNotebooks] = useState<NotebookSummary[]>([]);
  const [notebookId, setNotebookId] = useState("");
  const [newName, setNewName] = useState("");
  const [title, setTitle] = useState(initialTitle);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<PublicShareSaveResult | null>(null);
  const [saved, setSaved] = useState(false);
  const flight = useRef(false);
  const resumeKey = turnIndex === undefined ? "all" : String(turnIndex);

  function signInToSave() {
    const url = new URL(window.location.href);
    url.searchParams.set("save", resumeKey);
    window.history.replaceState(null, "", url);
    window.location.assign(signInLocation());
  }

  useEffect(() => {
    if (!saved) return;
    const timer = window.setTimeout(() => setSaved(false), 1800);
    return () => window.clearTimeout(timer);
  }, [saved]);

  async function begin() {
    if (flight.current) return;
    if (!getToken()) {
      signInToSave();
      return;
    }
    flight.current = true; setBusy(true); setOpen(true); setError("");
    try {
      const rows = await requestJson<NotebookSummary[]>("/notebooks", { tag: "public-share-save", unauthorized: "preserve" });
      const owned = rows.filter((row) => row.access === "owner");
      setNotebooks(owned); setNotebookId(owned[0]?.id || "new");
    } catch (cause) {
      if (httpErrorStatus(cause) === 401) { signInToSave(); return; }
      setError(toUserMessage(cause, "笔记本加载失败，请重试。"));
    } finally { flight.current = false; setBusy(false); }
  }

  useEffect(() => {
    const url = new URL(window.location.href);
    if (url.searchParams.get("save") !== resumeKey || !getToken()) return;
    url.searchParams.delete("save"); window.history.replaceState(null, "", url);
    void begin();
    // Resume exactly once after authentication returns to this public page.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function save() {
    if (flight.current || !title.trim() || !notebookId || (notebookId === "new" && !newName.trim())) return;
    flight.current = true; setBusy(true); setError("");
    try {
      let target = notebookId;
      if (target === "new") {
        const notebook = await requestJson<NotebookSummary>("/notebooks", {
          tag: "public-share-save", unauthorized: "preserve", method: "POST", body: JSON.stringify({ name: newName.trim() }),
        });
        target = notebook.id; setNotebookId(target); setNotebooks((rows) => [...rows, notebook]);
      }
      const savedResult = await savePublicShare(kind, token, {
        notebook_id: target, title: title.trim(), expected_revision: expectedRevision,
        ...(turnIndex === undefined ? {} : { turn_index: turnIndex }),
      });
      setResult(savedResult); setSaved(true); setOpen(false);
    } catch (cause) {
      if (httpErrorStatus(cause) === 401) { signInToSave(); return; }
      setError(toUserMessage(cause, "保存失败，请重试。"));
    } finally { flight.current = false; setBusy(false); }
  }

  return <div className="public-share-save">
    <button type="button" className="report-action" disabled={busy} onClick={() => void begin()}>
      {busy ? "处理中…" : saved ? "已保存" : turnIndex === undefined ? "保存到我的笔记本" : "保存本轮"}
    </button>
    {result && <a className="link-button inline-action" href={`/${notebookHash(result.notebook_id, result.source_id)}`}>打开笔记本</a>}
    {open && <div role="group" aria-label="保存到我的笔记本" className="public-share-save-form">
      <label htmlFor={`${id}-notebook`}>目标笔记本</label>
      <select id={`${id}-notebook`} value={notebookId} disabled={busy} onChange={(event) => setNotebookId(event.target.value)}>
        {notebooks.map((row) => <option key={row.id} value={row.id}>{row.name}</option>)}
        <option value="new">新建笔记本</option>
      </select>
      {notebookId === "new" && <label>笔记本名称<input value={newName} disabled={busy} onChange={(event) => setNewName(event.target.value)} /></label>}
      <label>保存标题<input value={title} disabled={busy} onChange={(event) => setTitle(event.target.value)} /></label>
      <p className="tool-hint">保存当前公开内容、引用原文及图片。副本保存后独立保留。</p>
      <button type="button" className="report-action" disabled={busy || !title.trim() || !notebookId || (notebookId === "new" && !newName.trim())} onClick={() => void save()}>{busy ? "保存中…" : "保存"}</button>
      <button type="button" className="report-action" disabled={busy} onClick={() => setOpen(false)}>取消</button>
    </div>}
    <p role="status" className="public-share-save-status">{error || (saved ? result?.reused ? "此内容已保存。" : "已保存，内容将在笔记本中处理。" : "")}</p>
  </div>;
}
