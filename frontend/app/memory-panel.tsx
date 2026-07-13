"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { Bot, Check, ChevronDown, Edit3, Search, Sparkles, Trash2, X } from "lucide-react";

import { AnswerMarkdown } from "./answer-markdown";
import { API_BASE, authHeaders, clearToken, getToken } from "./auth";
import {
  canEditMemory,
  memoryListPath,
  memoryOriginMeta,
  memoryProvenanceRows,
  memoryStatusMeta,
  type MemoryScope,
} from "./memory-model";
import { Pagination } from "./Pagination";
import type {
  MemoryOrigin,
  MemoryPreview,
  MemoryRecord,
  MemoryStatus,
  PaginatedMemories,
} from "./workspace-model";
import "./memory-panel.css";

const MEMORY_PAGE_SIZE = 20;

async function memoryApi<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...authHeaders(),
      ...(options.headers || {}),
    },
  });
  if (response.status === 401 && getToken()) {
    clearToken();
    window.location.reload();
  }
  if (!response.ok) {
    let detail = "";
    try {
      const body = await response.clone().json();
      detail = typeof body?.detail === "string" ? body.detail : "";
    } catch {
      detail = await response.text().catch(() => "");
    }
    throw new Error(detail || `Memory request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

type MemoryDraft = { title: string; content_md: string; tags: string };

function draftFor(memory: Pick<MemoryRecord, "title" | "content_md" | "tags">): MemoryDraft {
  return { title: memory.title, content_md: memory.content_md, tags: memory.tags.join(", ") };
}

function tagsFromDraft(value: string): string[] {
  return Array.from(new Set(value.split(/[,，\n]/).map((tag) => tag.trim()).filter(Boolean)));
}

function MemoryBody({ content }: { content: string }) {
  return (
    <div className="memory-markdown">
      <AnswerMarkdown answer={content} onReferenceClick={() => undefined} />
    </div>
  );
}

function MemoryEditor({
  draft,
  setDraft,
}: {
  draft: MemoryDraft;
  setDraft: (draft: MemoryDraft) => void;
}) {
  return (
    <div className="memory-editor">
      <label>
        标题
        <input
          autoFocus
          maxLength={80}
          value={draft.title}
          onChange={(event) => setDraft({ ...draft, title: event.target.value })}
        />
      </label>
      <label>
        内容
        <textarea
          rows={8}
          value={draft.content_md}
          onChange={(event) => setDraft({ ...draft, content_md: event.target.value })}
        />
      </label>
      <label>
        标签（逗号分隔）
        <input
          value={draft.tags}
          onChange={(event) => setDraft({ ...draft, tags: event.target.value })}
        />
      </label>
    </div>
  );
}

export function MemoryPanel({
  scope,
  notebookId,
}: {
  scope: MemoryScope;
  notebookId: string | null;
}) {
  const [items, setItems] = useState<MemoryRecord[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(0);
  const [status, setStatus] = useState<MemoryStatus | "all">("all");
  const [origin, setOrigin] = useState<MemoryOrigin | "all">("all");
  const [queryDraft, setQueryDraft] = useState("");
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState<MemoryDraft>({ title: "", content_md: "", tags: "" });
  const [busyId, setBusyId] = useState<string | null>(null);
  const requestEpochRef = useRef(0);

  useEffect(() => {
    if (scope === "notebook" && !notebookId) return;
    const epoch = ++requestEpochRef.current;
    const controller = new AbortController();
    setLoading(true);
    setError("");
    memoryApi<PaginatedMemories>(memoryListPath({
      scope,
      notebookId,
      status,
      origin,
      query,
      offset: page * MEMORY_PAGE_SIZE,
      limit: MEMORY_PAGE_SIZE,
    }), { signal: controller.signal })
      .then((result) => {
        if (requestEpochRef.current !== epoch) return;
        setItems(result.items);
        setTotal(result.total_count);
      })
      .catch((cause) => {
        if (controller.signal.aborted || requestEpochRef.current !== epoch) return;
        setError(cause instanceof Error ? cause.message : String(cause));
      })
      .finally(() => {
        if (requestEpochRef.current === epoch) setLoading(false);
      });
    return () => {
      controller.abort();
      if (requestEpochRef.current === epoch) requestEpochRef.current += 1;
    };
  }, [notebookId, origin, page, query, refresh, scope, status]);

  function beginEdit(memory: MemoryRecord) {
    setEditingId(memory.id);
    setDraft(draftFor(memory));
  }

  async function updateMemory(memory: MemoryRecord, action: "save" | "confirm" | "reject" | "deprecate") {
    const epoch = requestEpochRef.current;
    setBusyId(memory.id);
    setError("");
    try {
      const payload = {
        title: draft.title.trim(),
        content_md: draft.content_md.trim(),
        tags: tagsFromDraft(draft.tags),
      };
      const path = action === "save"
        ? `/memories/${encodeURIComponent(memory.id)}`
        : `/memories/${encodeURIComponent(memory.id)}/${action}`;
      await memoryApi<MemoryRecord>(path, {
        method: action === "save" ? "PATCH" : "POST",
        body: action === "save" || action === "confirm" ? JSON.stringify(payload) : undefined,
      });
      if (requestEpochRef.current !== epoch) return;
      setEditingId(null);
      setRefresh((value) => value + 1);
    } catch (cause) {
      if (requestEpochRef.current === epoch) {
        setError(cause instanceof Error ? cause.message : String(cause));
      }
    } finally {
      if (requestEpochRef.current === epoch) setBusyId(null);
    }
  }

  function submitSearch(event: FormEvent) {
    event.preventDefault();
    setPage(0);
    setQuery(queryDraft.trim());
  }

  return (
    <section className={`memory-panel memory-panel-${scope}`} aria-label={scope === "global" ? "全部 Memory" : "笔记本 Memory"}>
      <header className="memory-panel-heading">
        <div>
          <span className="memory-eyebrow"><Sparkles size={14} /> PRIVATE MEMORY</span>
          <h1>{scope === "global" ? "Memory" : "Notebook Memory"}</h1>
          <p>{scope === "global" ? "跨笔记本管理你主动保存和待审核的私有记忆。" : "只显示你自己绑定到当前笔记本的私有 Memory。"}</p>
        </div>
        <span className="memory-total">{total} 条</span>
      </header>

      <div className="memory-filterbar">
        <form className="memory-search" onSubmit={submitSearch}>
          <Search size={16} />
          <input
            type="search"
            value={queryDraft}
            placeholder="搜索标题、内容或标签"
            onChange={(event) => setQueryDraft(event.target.value)}
          />
          <button type="submit">搜索</button>
        </form>
        <label>
          状态
          <select value={status} onChange={(event) => { setStatus(event.target.value as MemoryStatus | "all"); setPage(0); }}>
            <option value="all">全部</option>
            <option value="candidate">待确认</option>
            <option value="confirmed">已确认</option>
            <option value="rejected">已拒绝</option>
            <option value="deprecated">已停用</option>
          </select>
        </label>
        <label>
          来源
          <select value={origin} onChange={(event) => { setOrigin(event.target.value as MemoryOrigin | "all"); setPage(0); }}>
            <option value="all">全部</option>
            <option value="ask_answer">Ask 回答</option>
            <option value="external_agent">Agent 提议</option>
          </select>
        </label>
      </div>

      {error && <div className="memory-error" role="alert">{error}</div>}
      {loading ? (
        <div className="memory-empty">正在读取你的 Memory…</div>
      ) : items.length === 0 ? (
        <div className="memory-empty">
          <Sparkles size={26} />
          <strong>还没有符合条件的 Memory</strong>
          <p>可在 Ask 回答下方手动保存，Agent 提议则会先进入待确认状态。</p>
        </div>
      ) : (
        <div className="memory-list">
          {items.map((memory) => {
            const statusMeta = memoryStatusMeta(memory.status);
            const originMeta = memoryOriginMeta(memory.origin);
            const editing = editingId === memory.id;
            const busy = busyId === memory.id;
            const provenanceRows = memoryProvenanceRows(memory);
            return (
              <article className={`memory-card memory-${memory.status}`} key={memory.id}>
                <div className="memory-card-head">
                  <div className="memory-card-badges">
                    <span className={`memory-badge tone-${statusMeta.tone}`}>{statusMeta.label}</span>
                    <span className={`memory-badge tone-${originMeta.tone}`}>
                      {memory.origin === "external_agent" ? <Bot size={13} /> : <Sparkles size={13} />}
                      {originMeta.label}
                    </span>
                  </div>
                  {canEditMemory(memory.status) && !editing && (
                    <button type="button" className="memory-icon-action" onClick={() => beginEdit(memory)}>
                      <Edit3 size={15} /> {memory.status === "candidate" ? "审核" : "编辑"}
                    </button>
                  )}
                </div>

                {editing ? (
                  <MemoryEditor draft={draft} setDraft={setDraft} />
                ) : (
                  <>
                    <h2>{memory.title}</h2>
                    <MemoryBody content={memory.content_md} />
                  </>
                )}

                {memory.tags.length > 0 && !editing && (
                  <div className="memory-tags">{memory.tags.map((tag) => <span key={tag}>#{tag}</span>)}</div>
                )}

                {provenanceRows.length > 0 && (
                  <details className="memory-provenance">
                    <summary><ChevronDown size={14} /> 来源与依据</summary>
                    <dl>
                      {provenanceRows.map(([label, value]) => (
                        <div key={label}><dt>{label}</dt><dd>{value}</dd></div>
                      ))}
                    </dl>
                  </details>
                )}

                <footer className="memory-card-footer">
                  <time dateTime={memory.updated_at}>更新于 {new Date(memory.updated_at).toLocaleString("zh-CN")}</time>
                  {editing && (
                    <div className="memory-actions">
                      <button type="button" disabled={busy} onClick={() => setEditingId(null)}><X size={14} /> 取消</button>
                      {memory.status === "candidate" ? (
                        <>
                          <button type="button" disabled={busy} className="danger" onClick={() => updateMemory(memory, "reject")}><Trash2 size={14} /> 拒绝</button>
                          <button type="button" disabled={busy} onClick={() => updateMemory(memory, "save")}><Edit3 size={14} /> 保存草稿</button>
                          <button type="button" disabled={busy || !draft.title.trim() || !draft.content_md.trim()} className="primary" onClick={() => updateMemory(memory, "confirm")}><Check size={14} /> 确认收录</button>
                        </>
                      ) : (
                        <>
                          <button type="button" disabled={busy} className="danger" onClick={() => updateMemory(memory, "deprecate")}><Trash2 size={14} /> 停用</button>
                          <button type="button" disabled={busy || !draft.title.trim() || !draft.content_md.trim()} className="primary" onClick={() => updateMemory(memory, "save")}><Check size={14} /> 保存</button>
                        </>
                      )}
                    </div>
                  )}
                </footer>
              </article>
            );
          })}
        </div>
      )}
      <Pagination page={page} pageSize={MEMORY_PAGE_SIZE} total={total} busy={loading} onPage={setPage} />
    </section>
  );
}

export function MemorySaveDialog({
  answerId,
  notebookId,
  onClose,
  onSaved,
}: {
  answerId: string;
  notebookId: string;
  onClose: () => void;
  onSaved: (memory: MemoryRecord) => void;
}) {
  const [draft, setDraft] = useState<MemoryDraft>({ title: "", content_md: "", tags: "" });
  const [provenance, setProvenance] = useState<Record<string, unknown>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const requestEpochRef = useRef(0);

  useEffect(() => {
    const epoch = ++requestEpochRef.current;
    const controller = new AbortController();
    setLoading(true);
    memoryApi<MemoryPreview>(`/answers/${encodeURIComponent(answerId)}/memory-preview`, {
      method: "POST",
      signal: controller.signal,
    })
      .then((preview) => {
        if (requestEpochRef.current !== epoch) return;
        setDraft(draftFor(preview));
        setProvenance(preview.provenance_summary);
      })
      .catch((cause) => {
        if (!controller.signal.aborted && requestEpochRef.current === epoch) {
          setError(cause instanceof Error ? cause.message : String(cause));
        }
      })
      .finally(() => {
        if (requestEpochRef.current === epoch) setLoading(false);
      });
    return () => {
      controller.abort();
      if (requestEpochRef.current === epoch) requestEpochRef.current += 1;
    };
  }, [answerId]);

  async function save() {
    if (saving || !draft.title.trim() || !draft.content_md.trim()) return;
    const epoch = requestEpochRef.current;
    setSaving(true);
    setError("");
    try {
      const memory = await memoryApi<MemoryRecord>(`/notebooks/${encodeURIComponent(notebookId)}/memories/from-answer`, {
        method: "POST",
        body: JSON.stringify({
          answer_id: answerId,
          title: draft.title.trim(),
          content_md: draft.content_md.trim(),
          tags: tagsFromDraft(draft.tags),
        }),
      });
      if (requestEpochRef.current === epoch) onSaved(memory);
    } catch (cause) {
      if (requestEpochRef.current === epoch) setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      if (requestEpochRef.current === epoch) setSaving(false);
    }
  }

  return (
    <section className="utility-modal memory-save-modal" role="dialog" aria-modal="true" aria-label="保存回答到 Memory">
      <div className="utility-modal-card memory-save-card">
        <header>
          <div>
            <span className="memory-eyebrow"><Sparkles size={14} /> MANUAL OPT-IN</span>
            <h2>保存到 Memory</h2>
            <p>先预览并编辑；只有点击确认后才会写入你的私有 Memory。</p>
          </div>
          <button type="button" className="memory-close" onClick={onClose} aria-label="关闭"><X size={18} /></button>
        </header>
        {loading ? <div className="memory-empty compact">正在生成预览…</div> : (
          <>
            <MemoryEditor draft={draft} setDraft={setDraft} />
            <div className="memory-preview-provenance">
              <strong>来源摘要</strong>
              <span>证据等级：{String(provenance.evidence_level ?? "未知")}</span>
              <span>引用：{String(provenance.citation_count ?? 0)} 条</span>
            </div>
          </>
        )}
        {error && <div className="memory-error" role="alert">{error}</div>}
        <footer className="memory-dialog-actions">
          <button type="button" onClick={onClose}>取消</button>
          <button type="button" className="primary" disabled={loading || saving || !draft.title.trim() || !draft.content_md.trim()} onClick={() => save()}>
            <Check size={15} /> {saving ? "保存中…" : "确认保存"}
          </button>
        </footer>
      </div>
    </section>
  );
}
