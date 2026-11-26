"use client";

import Image from "next/image";
import { CircuitBoard, ExternalLink, Plus, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { requestCanvasBlob, requestCanvasJson } from "./api-client";
import { toUserMessage } from "./errors";
import { label } from "./vocabulary";

type LibraryKind = "projects" | "components";
type LibraryFilter = "approved" | "pending" | "mine";
type ItemStatus = "approved" | "pending" | "rejected";

interface NotebookSpaceMeta {
  id: string;
  name: string;
  access: string;
  isAdmin: boolean;
}

interface CircuitProjectItem {
  id: string;
  notebookId: string;
  authorId: string;
  authorName: string;
  name: string;
  description: string;
  status: ItemStatus;
  reviewerName: string | null;
  reviewNote: string;
  updatedAt: string;
}

interface CircuitComponentItem {
  id: string;
  notebookId: string;
  authorId: string;
  authorName: string;
  name: string;
  description: string;
  status: ItemStatus;
  capability: string;
  pinCount: number;
  reviewerName: string | null;
  reviewNote: string;
  updatedAt: string;
}

interface PersonalProjectOption {
  id: string;
  name: string;
}

interface PersonalComponentOption {
  id: string;
  name: string;
  author: string;
}

const STATUS_LABEL: Record<ItemStatus, string> = {
  approved: "已入库",
  pending: "待审核",
  rejected: "已驳回",
};

const CAPABILITY_LABEL: Record<string, string> = {
  "with-circuit": "含子电路",
  "symbol-only": "仅符号",
  "needs-repair": "待修复",
};

async function canvasJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  return requestCanvasJson<T>(path, { ...init, tag: "canvas-circuits" });
}

function formatTime(value: string): string {
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function CircuitPreviewThumb({
  notebookId,
  projectId,
  alt,
}: {
  notebookId: string;
  projectId: string;
  alt: string;
}) {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    let objectUrl: string | null = null;
    requestCanvasBlob(
      `notebook/notebooks/${encodeURIComponent(notebookId)}/projects/${encodeURIComponent(projectId)}/preview.svg`,
      { tag: "canvas-circuit-preview" },
    )
      .then((blob) => {
        if (!active || !blob) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch(() => undefined);
    return () => {
      active = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [notebookId, projectId]);

  if (!url) {
    return (
      <div className="circuit-lib-thumb-placeholder">
        <CircuitBoard size={26} aria-hidden="true" />
      </div>
    );
  }
  return (
    <Image
      className="circuit-lib-thumb-img"
      src={url}
      alt={alt}
      width={320}
      height={180}
      unoptimized
    />
  );
}

export function NotebookCircuitsPanel({
  notebookId,
  notebookName,
}: {
  notebookId: string;
  notebookName: string;
}) {
  const [kind, setKind] = useState<LibraryKind>("projects");
  const [filter, setFilter] = useState<LibraryFilter>("approved");
  const [meta, setMeta] = useState<NotebookSpaceMeta | null>(null);
  const [projects, setProjects] = useState<CircuitProjectItem[]>([]);
  const [components, setComponents] = useState<CircuitComponentItem[]>([]);
  const [pendingProjectCount, setPendingProjectCount] = useState(0);
  const [pendingComponentCount, setPendingComponentCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [reviewNotes, setReviewNotes] = useState<Record<string, string>>({});

  // Inline import from user's personal Canvas Cloud Projects / User Components
  const [importOpen, setImportOpen] = useState(false);
  const [importLoading, setImportLoading] = useState(false);
  const [importLoadingMore, setImportLoadingMore] = useState(false);
  const [personalProjects, setPersonalProjects] = useState<PersonalProjectOption[]>([]);
  const [personalComponents, setPersonalComponents] = useState<PersonalComponentOption[]>([]);
  const [componentQuery, setComponentQuery] = useState("");
  const [componentNextCursor, setComponentNextCursor] = useState<string | null>(null);
  const [pickedSourceId, setPickedSourceId] = useState("");
  const [importDesc, setImportDesc] = useState("");
  const loadSeqRef = useRef(0);
  const importSeqRef = useRef(0);

  const canAdmin = Boolean(meta?.isAdmin);

  const loadLibrary = useCallback(async () => {
    const seq = ++loadSeqRef.current;
    setLoading(true);
    setProjects([]);
    setComponents([]);
    setError("");
    try {
      const [projData, compData] = await Promise.all([
        canvasJson<{
          notebook: NotebookSpaceMeta;
          projects: CircuitProjectItem[];
          pendingCount?: number;
        }>(
          `notebook/notebooks/${encodeURIComponent(notebookId)}/projects?status=${encodeURIComponent(filter)}`,
        ),
        canvasJson<{
          notebook: NotebookSpaceMeta;
          components: CircuitComponentItem[];
          pendingCount?: number;
        }>(
          `notebook/notebooks/${encodeURIComponent(notebookId)}/components?status=${encodeURIComponent(filter)}`,
        ),
      ]);
      if (loadSeqRef.current !== seq) return;
      setMeta(projData.notebook);
      setProjects(projData.projects);
      setComponents(compData.components);
      setPendingProjectCount(projData.pendingCount ?? 0);
      setPendingComponentCount(compData.pendingCount ?? 0);
    } catch (err) {
      if (loadSeqRef.current !== seq) return;
      setError(toUserMessage(err, "无法加载电路库，请确认电路画布服务已启动。"));
    } finally {
      if (loadSeqRef.current === seq) setLoading(false);
    }
  }, [notebookId, filter]);

  useEffect(() => {
    void loadLibrary();
  }, [loadLibrary]);

  useEffect(() => {
    if (!canAdmin && filter === "pending") setFilter("approved");
  }, [canAdmin, filter]);

  useEffect(() => {
    if (!importOpen) return;
    const seq = ++importSeqRef.current;
    setImportLoading(true);
    setComponentNextCursor(null);
    setError("");
    if (kind === "projects") {
      canvasJson<{ projects: PersonalProjectOption[] }>("projects")
        .then((res) => {
          if (importSeqRef.current !== seq) return;
          setPersonalProjects(res.projects);
          setPickedSourceId(res.projects[0]?.id ?? "");
        })
        .catch((err) => {
          if (importSeqRef.current === seq) {
            setError(toUserMessage(err, "加载个人云端项目失败"));
          }
        })
        .finally(() => {
          if (importSeqRef.current === seq) setImportLoading(false);
        });
    } else {
      const qs = new URLSearchParams({ view: "summary", limit: "30" });
      if (componentQuery.trim()) qs.set("q", componentQuery.trim());
      canvasJson<{ entries: PersonalComponentOption[]; nextCursor?: string | null }>(
        `components?${qs.toString()}`,
      )
        .then((res) => {
          if (importSeqRef.current !== seq) return;
          setPersonalComponents(res.entries);
          setComponentNextCursor(res.nextCursor ?? null);
          setPickedSourceId((current) =>
            res.entries.some((entry) => entry.id === current)
              ? current
              : (res.entries[0]?.id ?? ""),
          );
        })
        .catch((err) => {
          if (importSeqRef.current === seq) {
            setError(toUserMessage(err, "加载自定义器件失败"));
          }
        })
        .finally(() => {
          if (importSeqRef.current === seq) setImportLoading(false);
        });
    }
  }, [importOpen, kind, componentQuery]);

  async function handleLoadMoreComponents() {
    if (!componentNextCursor || importLoadingMore) return;
    const seq = importSeqRef.current;
    setImportLoadingMore(true);
    try {
      const qs = new URLSearchParams({
        view: "summary",
        limit: "30",
        cursor: componentNextCursor,
      });
      if (componentQuery.trim()) qs.set("q", componentQuery.trim());
      const res = await canvasJson<{
        entries: PersonalComponentOption[];
        nextCursor?: string | null;
      }>(`components?${qs.toString()}`);
      if (importSeqRef.current !== seq) return;
      setPersonalComponents((prev) => {
        const seen = new Set(prev.map((item) => item.id));
        return [...prev, ...res.entries.filter((item) => !seen.has(item.id))];
      });
      setComponentNextCursor(res.nextCursor ?? null);
    } catch (err) {
      if (importSeqRef.current === seq) {
        setError(toUserMessage(err, "加载更多器件失败"));
      }
    } finally {
      if (importSeqRef.current === seq) setImportLoadingMore(false);
    }
  }

  async function handleImportSubmit() {
    if (!pickedSourceId) return;
    setBusyId("import");
    setError("");
    setNotice("");
    try {
      if (kind === "projects") {
        const res = await canvasJson<{ project: CircuitProjectItem }>(
          `notebook/notebooks/${encodeURIComponent(notebookId)}/projects`,
          {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({
              sourceProjectId: pickedSourceId,
              description: importDesc.trim(),
            }),
          },
        );
        setNotice(
          res.project.status === "approved"
            ? `电路工程「${res.project.name}」已直接入库到本笔记本。`
            : `电路工程「${res.project.name}」已提交，等待管理员审核入库。`,
        );
      } else {
        const res = await canvasJson<{ component: CircuitComponentItem }>(
          `notebook/notebooks/${encodeURIComponent(notebookId)}/components`,
          {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({
              sourceComponentId: pickedSourceId,
              description: importDesc.trim(),
            }),
          },
        );
        setNotice(
          res.component.status === "approved"
            ? `器件「${res.component.name}」已直接入库到本笔记本。`
            : `器件「${res.component.name}」已提交，等待管理员审核入库。`,
        );
      }
      setImportOpen(false);
      setImportDesc("");
      await loadLibrary();
    } catch (err) {
      setError(toUserMessage(err, "提交失败"));
    } finally {
      setBusyId(null);
    }
  }

  async function handleReview(
    itemKind: LibraryKind,
    id: string,
    name: string,
    decision: "approve" | "reject",
  ) {
    setBusyId(id);
    setError("");
    setNotice("");
    try {
      await canvasJson(
        `notebook/notebooks/${encodeURIComponent(notebookId)}/${itemKind}/${encodeURIComponent(id)}/review`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ decision, note: reviewNotes[id] ?? "" }),
        },
      );
      setNotice(decision === "approve" ? `已审核通过「${name}」并入库。` : `已驳回「${name}」。`);
      await loadLibrary();
    } catch (err) {
      setError(toUserMessage(err, "审核操作失败"));
    } finally {
      setBusyId(null);
    }
  }

  async function handleDelete(itemKind: LibraryKind, id: string, name: string, status: ItemStatus) {
    setBusyId(id);
    setError("");
    setNotice("");
    try {
      await canvasJson(
        `notebook/notebooks/${encodeURIComponent(notebookId)}/${itemKind}/${encodeURIComponent(id)}`,
        { method: "DELETE" },
      );
      setNotice(status === "approved" ? `已移除「${name}」。` : `已撤回「${name}」。`);
      await loadLibrary();
    } catch (err) {
      setError(toUserMessage(err, "操作失败"));
    } finally {
      setBusyId(null);
    }
  }

  const openCanvasUrl = (params: Record<string, string>) => {
    const qs = new URLSearchParams({
      notebook: notebookId,
      notebookName,
      ...params,
    });
    window.location.assign(`/canvas/?${qs.toString()}`);
  };

  const pendingTotal =
    kind === "projects" ? pendingProjectCount : pendingComponentCount;

  return (
    <div className="circuit-lib-panel">
      <div className="circuit-lib-head">
        <div>
          <h3>本笔记本电路库</h3>
          <p>管理与共享「{notebookName}」中的模拟电路工程与自定义器件库。</p>
        </div>
        <div className="circuit-lib-head-actions">
          <button
            type="button"
            className="button secondary"
            onClick={() => setImportOpen((open) => !open)}
          >
            <Plus size={15} aria-hidden="true" />
            <span>{importOpen ? "收起导入" : "从我的画布导入"}</span>
          </button>
          <button
            type="button"
            className="button primary"
            onClick={() =>
              openCanvasUrl(kind === "components" ? { view: "components" } : {})
            }
          >
            <ExternalLink size={15} aria-hidden="true" />
            <span>打开电路画布</span>
          </button>
        </div>
      </div>

      <div className="circuit-lib-bar">
        <div className="circuit-lib-kind-tabs" role="tablist" aria-label="电路库类型">
          <button
            type="button"
            role="tab"
            className={`circuit-lib-pill${kind === "projects" ? " active" : ""}`}
            aria-selected={kind === "projects"}
            onClick={() => {
              setKind("projects");
              setImportOpen(false);
            }}
          >
            电路工程（{projects.length}）
          </button>
          <button
            type="button"
            role="tab"
            className={`circuit-lib-pill${kind === "components" ? " active" : ""}`}
            aria-selected={kind === "components"}
            onClick={() => {
              setKind("components");
              setImportOpen(false);
            }}
          >
            器件库（{components.length}）
          </button>
        </div>

        <div className="circuit-lib-filter-tabs" role="tablist" aria-label="入库状态">
          <button
            type="button"
            role="tab"
            className={`circuit-lib-pill${filter === "approved" ? " active" : ""}`}
            aria-selected={filter === "approved"}
            onClick={() => setFilter("approved")}
          >
            已入库
          </button>
          {canAdmin && (
            <button
              type="button"
              role="tab"
              className={`circuit-lib-pill${filter === "pending" ? " active" : ""}`}
              aria-selected={filter === "pending"}
              onClick={() => setFilter("pending")}
            >
              待审核
              {pendingTotal > 0 && <span className="circuit-lib-badge">{pendingTotal}</span>}
            </button>
          )}
          <button
            type="button"
            role="tab"
            className={`circuit-lib-pill${filter === "mine" ? " active" : ""}`}
            aria-selected={filter === "mine"}
            onClick={() => setFilter("mine")}
          >
            我的提交
          </button>
          <button
            type="button"
            className="icon-button compact"
            title="刷新"
            aria-label="刷新电路库"
            onClick={() => { void loadLibrary(); }}
          >
            <RefreshCw size={14} aria-hidden="true" />
          </button>
        </div>
      </div>

      {importOpen && (
        <div className="circuit-lib-import-box">
          {kind === "components" && (
            <div className="circuit-lib-import-row">
              <input
                type="search"
                className="circuit-lib-input"
                placeholder="搜索自定义器件名称…"
                aria-label="搜索可导入的自定义器件"
                value={componentQuery}
                onChange={(e) => setComponentQuery(e.target.value)}
              />
              {componentNextCursor && (
                <button
                  type="button"
                  className="button secondary"
                  disabled={importLoading || importLoadingMore}
                  onClick={() => { void handleLoadMoreComponents(); }}
                >
                  {importLoadingMore ? "加载中…" : "加载更多器件"}
                </button>
              )}
            </div>
          )}
          {importLoading ? (
            <p className="tool-hint">正在读取你在电路画布中保存的条目…</p>
          ) : (kind === "projects" ? personalProjects : personalComponents).length === 0 ? (
            <p className="tool-hint">
              {kind === "projects" ? (
                <>
                  个人云端项目库暂无可导入电路，可先
                  <button
                    type="button"
                    className="link-button inline-action"
                    onClick={() => openCanvasUrl({})}
                  >
                    打开电路画布
                  </button>
                  绘制并保存。
                </>
              ) : componentQuery.trim() ? (
                "未找到匹配的自定义器件，请调整关键词后重试。"
              ) : (
                <>
                  暂无可导入的自定义器件，可先
                  <button
                    type="button"
                    className="link-button inline-action"
                    onClick={() => openCanvasUrl({ view: "components" })}
                  >
                    打开电路画布
                  </button>
                  创建并发布器件。
                </>
              )}
            </p>
          ) : (
            <>
              <div className="circuit-lib-import-row">
                <select
                  className="circuit-lib-select"
                  value={pickedSourceId}
                  onChange={(e) => setPickedSourceId(e.target.value)}
                >
                  {kind === "projects"
                    ? personalProjects.map((item) => (
                        <option key={item.id} value={item.id}>
                          {item.name || "未命名电路"}
                        </option>
                      ))
                    : personalComponents.map((item) => (
                        <option key={item.id} value={item.id}>
                          {item.name}（{item.author}）
                        </option>
                      ))}
                </select>
                <input
                  type="text"
                  className="circuit-lib-input"
                  placeholder="补充说明（可选）"
                  maxLength={500}
                  value={importDesc}
                  onChange={(e) => setImportDesc(e.target.value)}
                />
                <button
                  type="button"
                  className="button primary"
                  disabled={busyId !== null || !pickedSourceId}
                  onClick={() => { void handleImportSubmit(); }}
                >
                  {busyId === "import" ? "提交中…" : "确认提交"}
                </button>
              </div>
              <p className="tool-hint">
                {canAdmin
                  ? "你是本笔记本的管理者，提交后将直接入库。"
                  : "提交后将进入待审核队列，需等待管理员审核通过后才能入库。"}
              </p>
            </>
          )}
        </div>
      )}

      {notice && <div className="circuit-lib-notice" role="status">{notice}</div>}
      {error && <div className="circuit-lib-error" role="alert">{error}</div>}

      {loading ? (
        <p className="tool-hint">正在加载电路库…</p>
      ) : kind === "projects" ? (
        projects.length === 0 ? (
          <div className="circuit-lib-empty">
            <CircuitBoard size={28} aria-hidden="true" />
            <strong>
              {filter === "approved"
                ? "这个笔记本还没有已入库的电路工程"
                : filter === "pending"
                  ? "当前没有待审核的电路工程"
                  : "你还没有向本笔记本提交过电路工程"}
            </strong>
            <p>
              可以
              <button
                type="button"
                className="link-button inline-action"
                onClick={() => openCanvasUrl({})}
              >
                打开电路画布
              </button>
              绘制并发布到本笔记本，或直接
              <button
                type="button"
                className="link-button inline-action"
                onClick={() => setImportOpen(true)}
              >
                从我的画布导入
              </button>
              。
            </p>
          </div>
        ) : (
          <div className="circuit-lib-grid">
            {projects.map((item) => (
              <article className="circuit-lib-card" key={item.id}>
                <div className="circuit-lib-thumb">
                  <CircuitPreviewThumb
                    notebookId={notebookId}
                    projectId={item.id}
                    alt={item.name}
                  />
                </div>
                <div className="circuit-lib-card-head">
                  <h4>{item.name}</h4>
                  <span className={`circuit-lib-status status-${item.status}`}>
                    {STATUS_LABEL[item.status]}
                  </span>
                </div>
                <p className="circuit-lib-meta">
                  作者：{item.authorName} · {formatTime(item.updatedAt)}
                </p>
                {item.description && <p className="circuit-lib-desc">{item.description}</p>}
                {item.reviewNote && (
                  <p className="circuit-lib-review-note">
                    审核意见（{item.reviewerName ?? "管理员"}）：{item.reviewNote}
                  </p>
                )}
                {filter === "pending" && canAdmin && (
                  <input
                    type="text"
                    className="circuit-lib-input"
                    placeholder="审核备注（可选）"
                    maxLength={500}
                    value={reviewNotes[item.id] ?? ""}
                    onChange={(e) =>
                      setReviewNotes((prev) => ({ ...prev, [item.id]: e.target.value }))
                    }
                  />
                )}
                <div className="circuit-lib-card-actions">
                  <button
                    type="button"
                    className="button primary compact"
                    onClick={() => openCanvasUrl({ notebookProject: item.id })}
                  >
                    {item.status === "pending" ? "画布预览" : "在画布打开"}
                  </button>
                  {filter === "pending" && canAdmin && (
                    <>
                      <button
                        type="button"
                        className="button secondary compact"
                        disabled={busyId !== null}
                        onClick={() => { void handleReview("projects", item.id, item.name, "approve"); }}
                      >
                        通过入库
                      </button>
                      <button
                        type="button"
                        className="button secondary compact danger"
                        disabled={busyId !== null}
                        onClick={() => { void handleReview("projects", item.id, item.name, "reject"); }}
                      >
                        驳回
                      </button>
                    </>
                  )}
                  {(canAdmin || item.status !== "approved") && filter !== "pending" && (
                    <button
                      type="button"
                      className="button secondary compact danger"
                      disabled={busyId !== null}
                      onClick={() => { void handleDelete("projects", item.id, item.name, item.status); }}
                    >
                      {item.status === "approved" ? "移除" : "撤回"}
                    </button>
                  )}
                </div>
              </article>
            ))}
          </div>
        )
      ) : components.length === 0 ? (
        <div className="circuit-lib-empty">
          <CircuitBoard size={28} aria-hidden="true" />
          <strong>
            {filter === "approved"
              ? "这个笔记本还没有已入库的自定义器件"
              : filter === "pending"
                ? "当前没有待审核的器件提交"
                : "你还没有向本笔记本提交过自定义器件"}
          </strong>
          <p>在电路画布中创建自定义器件后，可以发布到本笔记本供成员复用。</p>
        </div>
      ) : (
        <div className="circuit-lib-grid">
          {components.map((item) => (
            <article className="circuit-lib-card" key={item.id}>
              <div className="circuit-lib-card-head">
                <h4>{item.name}</h4>
                <span className={`circuit-lib-status status-${item.status}`}>
                  {STATUS_LABEL[item.status]}
                </span>
              </div>
              <p className="circuit-lib-meta">
                作者：{item.authorName} · {label(CAPABILITY_LABEL, item.capability, "自定义器件")} · {item.pinCount} 引脚
              </p>
              {item.description && <p className="circuit-lib-desc">{item.description}</p>}
              {item.reviewNote && (
                <p className="circuit-lib-review-note">
                  审核意见（{item.reviewerName ?? "管理员"}）：{item.reviewNote}
                </p>
              )}
              {filter === "pending" && canAdmin && (
                <input
                  type="text"
                  className="circuit-lib-input"
                  placeholder="审核备注（可选）"
                  maxLength={500}
                  value={reviewNotes[item.id] ?? ""}
                  onChange={(e) =>
                    setReviewNotes((prev) => ({ ...prev, [item.id]: e.target.value }))
                  }
                />
              )}
              <div className="circuit-lib-card-actions">
                <button
                  type="button"
                  className="button primary compact"
                  onClick={() => openCanvasUrl({ view: "components" })}
                >
                  前往画布使用
                </button>
                {filter === "pending" && canAdmin && (
                  <>
                    <button
                      type="button"
                      className="button secondary compact"
                      disabled={busyId !== null}
                      onClick={() => { void handleReview("components", item.id, item.name, "approve"); }}
                    >
                      通过入库
                    </button>
                    <button
                      type="button"
                      className="button secondary compact danger"
                      disabled={busyId !== null}
                      onClick={() => { void handleReview("components", item.id, item.name, "reject"); }}
                    >
                      驳回
                    </button>
                  </>
                )}
                {(canAdmin || item.status !== "approved") && filter !== "pending" && (
                  <button
                    type="button"
                    className="button secondary compact danger"
                    disabled={busyId !== null}
                    onClick={() => { void handleDelete("components", item.id, item.name, item.status); }}
                  >
                    {item.status === "approved" ? "移除" : "撤回"}
                  </button>
                )}
              </div>
            </article>
          ))}
        </div>
      )}
    </div>
  );
}
