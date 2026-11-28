"use client";

import Image from "next/image";
import { CircuitBoard, ExternalLink, Globe, RefreshCw, Users } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { requestCanvasBlob, requestCanvasJson } from "./api-client";
import { toUserMessage } from "./errors";

type CircuitsScope = "personal" | "groups" | "public";

interface PersonalCircuitSummary {
  id: string;
  name: string;
  updatedAt: string;
  revision: number;
  favorite?: boolean;
}

interface CanvasGroupSummary {
  id: string;
  name: string;
  kind: string;
  description: string;
  isPublic?: boolean;
  isAdmin: boolean;
  memberCount: number;
  approvedProjectCount: number;
  approvedComponentCount: number;
}

interface GroupCircuitSummary {
  id: string;
  groupId?: string;
  authorId: string;
  authorName: string;
  name: string;
  description: string;
  status: string;
  updatedAt: string;
}

function formatCircuitTime(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "";
  return parsed.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function CircuitThumb({ previewPath, alt }: { previewPath: string; alt: string }) {
  const [url, setUrl] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    let objectUrl: string | null = null;
    requestCanvasBlob(previewPath, { tag: "home-circuit-preview" })
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
  }, [previewPath]);

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

export function HomeCircuitsView({
  isSystemAdmin,
  onOpenGroups,
}: {
  isSystemAdmin: boolean;
  onOpenGroups: () => void;
}) {
  const [scope, setScope] = useState<CircuitsScope>("personal");
  const [personalProjects, setPersonalProjects] = useState<PersonalCircuitSummary[]>([]);
  const [groups, setGroups] = useState<CanvasGroupSummary[]>([]);
  const [selectedPrivateGroupId, setSelectedPrivateGroupId] = useState("");
  const [selectedPublicGroupId, setSelectedPublicGroupId] = useState("");
  const [groupProjects, setGroupProjects] = useState<GroupCircuitSummary[]>([]);
  const [loadingBase, setLoadingBase] = useState(true);
  const [loadingGroupItems, setLoadingGroupItems] = useState(false);
  const [error, setError] = useState("");
  const [groupError, setGroupError] = useState("");
  const [reloadTick, setReloadTick] = useState(0);
  const baseSeqRef = useRef(0);
  const groupSeqRef = useRef(0);

  const privateGroups = useMemo(
    () => groups.filter((group) => !group.isPublic),
    [groups],
  );
  const publicGroups = useMemo(
    () => groups.filter((group) => group.isPublic === true),
    [groups],
  );

  const activeGroupId =
    scope === "public" ? selectedPublicGroupId : selectedPrivateGroupId;
  const activeGroup = useMemo(
    () =>
      (scope === "public" ? publicGroups : privateGroups).find(
        (group) => group.id === activeGroupId,
      ) ?? null,
    [scope, publicGroups, privateGroups, activeGroupId],
  );

  const loadBase = useCallback(async () => {
    const seq = ++baseSeqRef.current;
    setLoadingBase(true);
    setError("");
    try {
      const [projRes, groupsRes] = await Promise.all([
        requestCanvasJson<{ projects: PersonalCircuitSummary[] }>("projects", {
          tag: "home-circuits-personal",
        }),
        requestCanvasJson<{ groups: CanvasGroupSummary[] }>("notebook/groups", {
          tag: "home-circuits-groups",
        }),
      ]);
      if (baseSeqRef.current !== seq) return;
      const nextProjects = Array.isArray(projRes.projects) ? projRes.projects : [];
      const nextGroups = Array.isArray(groupsRes.groups) ? groupsRes.groups : [];
      setPersonalProjects(nextProjects);
      setGroups(nextGroups);
      const nextPrivate = nextGroups.filter((group) => !group.isPublic);
      const nextPublic = nextGroups.filter((group) => group.isPublic === true);
      setSelectedPrivateGroupId((prev) =>
        prev && nextPrivate.some((group) => group.id === prev)
          ? prev
          : (nextPrivate[0]?.id ?? ""),
      );
      setSelectedPublicGroupId((prev) =>
        prev && nextPublic.some((group) => group.id === prev)
          ? prev
          : (nextPublic[0]?.id ?? ""),
      );
    } catch (err) {
      if (baseSeqRef.current !== seq) return;
      setError(toUserMessage(err, "无法加载电路库，请确认电路画布服务已启动。"));
    } finally {
      if (baseSeqRef.current === seq) setLoadingBase(false);
    }
  }, []);

  useEffect(() => {
    void loadBase();
  }, [loadBase, reloadTick]);

  useEffect(() => {
    if (scope === "personal") {
      setGroupProjects([]);
      setGroupError("");
      setLoadingGroupItems(false);
      return;
    }
    if (!activeGroupId) {
      setGroupProjects([]);
      setGroupError("");
      setLoadingGroupItems(false);
      return;
    }
    const seq = ++groupSeqRef.current;
    setLoadingGroupItems(true);
    setGroupError("");
    requestCanvasJson<{ projects: GroupCircuitSummary[] }>(
      `notebook/groups/${encodeURIComponent(activeGroupId)}/projects?status=approved`,
      { tag: "home-circuits-group-projects" },
    )
      .then((res) => {
        if (groupSeqRef.current !== seq) return;
        setGroupProjects(Array.isArray(res.projects) ? res.projects : []);
      })
      .catch((err) => {
        if (groupSeqRef.current !== seq) return;
        setGroupProjects([]);
        setGroupError(toUserMessage(err, "加载该群组的电路工程失败。"));
      })
      .finally(() => {
        if (groupSeqRef.current === seq) setLoadingGroupItems(false);
      });
  }, [scope, activeGroupId, reloadTick]);

  return (
    <section className="home-circuits-section">
      <div className="collection-title home-circuits-title-row">
        <div>
          <h1>我的电路库</h1>
          <p>快速浏览你的云端电路工程、已加入群组的共享电路以及全员可见的公共电路库。</p>
        </div>
        <div className="circuit-lib-head-actions">
          <button
            type="button"
            className="sort-button"
            disabled={loadingBase || loadingGroupItems}
            onClick={() => setReloadTick((value) => value + 1)}
          >
            <RefreshCw size={14} aria-hidden="true" />
            <span>{loadingBase || loadingGroupItems ? "刷新中…" : "刷新"}</span>
          </button>
        </div>
      </div>

      <div className="circuit-lib-bar">
        <div className="circuit-lib-kind-tabs" role="tablist" aria-label="电路库范围">
          <button
            type="button"
            role="tab"
            aria-selected={scope === "personal"}
            className={`circuit-lib-pill ${scope === "personal" ? "active" : ""}`}
            onClick={() => setScope("personal")}
          >
            <CircuitBoard size={14} aria-hidden="true" />
            <span>我的云端电路（{personalProjects.length}）</span>
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={scope === "groups"}
            className={`circuit-lib-pill ${scope === "groups" ? "active" : ""}`}
            onClick={() => setScope("groups")}
          >
            <Users size={14} aria-hidden="true" />
            <span>群组电路库（{privateGroups.length}）</span>
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={scope === "public"}
            className={`circuit-lib-pill ${scope === "public" ? "active" : ""}`}
            onClick={() => setScope("public")}
          >
            <Globe size={14} aria-hidden="true" />
            <span>公共电路库（{publicGroups.length}）</span>
          </button>
        </div>
      </div>

      {error && <div className="circuit-lib-error" role="alert">{error}</div>}

      {scope === "personal" && (
        loadingBase ? (
          <div className="circuit-lib-empty">
            <p>正在加载我的云端电路…</p>
          </div>
        ) : personalProjects.length === 0 ? (
          <div className="circuit-lib-empty">
            <CircuitBoard size={26} aria-hidden="true" />
            <strong>还没有个人云端电路</strong>
            <p>打开电路画布绘制并保存到云端后，这里会展示你的所有个人电路工程。</p>
            <a className="new-pill home-canvas-cta" href="/canvas">
              <CircuitBoard size={15} aria-hidden="true" />
              <span>打开电路画布</span>
            </a>
          </div>
        ) : (
          <div className="circuit-lib-grid">
            {personalProjects.map((project) => (
              <article key={project.id} className="circuit-lib-card">
                <div className="circuit-lib-thumb">
                  <CircuitThumb
                    previewPath={`projects/${encodeURIComponent(project.id)}/preview.svg?v=${project.revision}`}
                    alt={project.name || "未命名电路"}
                  />
                </div>
                <div className="circuit-lib-card-head">
                  <h4>{project.name || "未命名电路"}</h4>
                </div>
                <p className="circuit-lib-meta">
                  更新于 {formatCircuitTime(project.updatedAt) || "刚刚"}
                </p>
                <div className="circuit-lib-card-actions">
                  <a
                    className="sort-button home-canvas-link"
                    href={`/canvas/?project=${encodeURIComponent(project.id)}`}
                  >
                    <ExternalLink size={13} aria-hidden="true" />
                    <span>在画布中打开</span>
                  </a>
                </div>
              </article>
            ))}
          </div>
        )
      )}

      {(scope === "groups" || scope === "public") && (
        loadingBase ? (
          <div className="circuit-lib-empty">
            <p>正在加载{scope === "public" ? "公共电路库" : "群组电路库"}…</p>
          </div>
        ) : (scope === "public" ? publicGroups : privateGroups).length === 0 ? (
          <div className="circuit-lib-empty">
            {scope === "public" ? (
              <>
                <Globe size={26} aria-hidden="true" />
                <strong>暂未配置公共电路库</strong>
                <p>
                  {isSystemAdmin
                    ? "你可以前往群组页面新建一个全员可见公共群组，再在电路画布中发布电路图。"
                    : "系统管理员开启公共群组并发布电路后，所有用户都可以在这里浏览和打开。"}
                </p>
                <button type="button" className="sort-button" onClick={onOpenGroups}>
                  前往群组页面
                </button>
              </>
            ) : (
              <>
                <Users size={26} aria-hidden="true" />
                <strong>还没有加入任何群组</strong>
                <p>创建或加入群组后，团队成员共享的电路工程和器件库会在这里汇总展示。</p>
                <button type="button" className="sort-button" onClick={onOpenGroups}>
                  前往群组页面
                </button>
              </>
            )}
          </div>
        ) : (
          <div className="home-circuits-group-panel">
            {(scope === "public" ? publicGroups : privateGroups).length > 1 && (
              <div className="home-circuits-group-pills" role="tablist" aria-label="选择群组">
                {(scope === "public" ? publicGroups : privateGroups).map((group) => (
                  <button
                    key={group.id}
                    type="button"
                    role="tab"
                    aria-selected={group.id === activeGroupId}
                    className={`circuit-lib-pill ${group.id === activeGroupId ? "active" : ""}`}
                    onClick={() =>
                      scope === "public"
                        ? setSelectedPublicGroupId(group.id)
                        : setSelectedPrivateGroupId(group.id)
                    }
                  >
                    <span>{group.name}</span>
                    <span className="home-circuits-count-muted">
                      （{group.approvedProjectCount}）
                    </span>
                  </button>
                ))}
              </div>
            )}

            {activeGroup && (
              <div className="home-circuits-group-banner">
                <div>
                  <div className="home-circuits-group-title-row">
                    <h3>{activeGroup.name}</h3>
                    {activeGroup.isPublic && (
                      <span className="group-chip">全员公共</span>
                    )}
                    <span className="circuit-lib-meta">
                      已入库 {activeGroup.approvedProjectCount} 个电路 · {activeGroup.approvedComponentCount} 个器件
                    </span>
                  </div>
                  {activeGroup.description && (
                    <p className="circuit-lib-meta">{activeGroup.description}</p>
                  )}
                </div>
                <div className="circuit-lib-head-actions">
                  <a
                    className="sort-button home-canvas-link"
                    href={`/canvas/?group=${encodeURIComponent(activeGroup.id)}&view=projects`}
                  >
                    <CircuitBoard size={14} aria-hidden="true" />
                    <span>在画布中管理工程库</span>
                  </a>
                  <a
                    className="sort-button home-canvas-link"
                    href={`/canvas/?group=${encodeURIComponent(activeGroup.id)}&view=components`}
                  >
                    <ExternalLink size={14} aria-hidden="true" />
                    <span>器件库</span>
                  </a>
                </div>
              </div>
            )}

            {groupError && (
              <div className="circuit-lib-error" role="alert">{groupError}</div>
            )}

            {loadingGroupItems ? (
              <div className="circuit-lib-empty">
                <p>正在加载电路工程…</p>
              </div>
            ) : groupProjects.length === 0 ? (
              <div className="circuit-lib-empty">
                <CircuitBoard size={26} aria-hidden="true" />
                <strong>该库还没有已入库的电路工程</strong>
                <p>
                  {activeGroup?.isAdmin
                    ? "你可以在电路画布中打开或绘制电路，直接发布到当前电路库。"
                    : "你可以在电路画布中提交电路工程，待管理员审核后对成员可见。"}
                </p>
                {activeGroup && (
                  <a
                    className="new-pill home-canvas-cta"
                    href={`/canvas/?group=${encodeURIComponent(activeGroup.id)}&view=projects`}
                  >
                    <CircuitBoard size={15} aria-hidden="true" />
                    <span>前往画布发布电路</span>
                  </a>
                )}
              </div>
            ) : (
              <div className="circuit-lib-grid">
                {groupProjects.map((project) => (
                  <article key={project.id} className="circuit-lib-card">
                    <div className="circuit-lib-thumb">
                      <CircuitThumb
                        previewPath={`notebook/groups/${encodeURIComponent(activeGroupId)}/projects/${encodeURIComponent(project.id)}/preview.svg`}
                        alt={project.name}
                      />
                    </div>
                    <div className="circuit-lib-card-head">
                      <h4>{project.name}</h4>
                      {activeGroup?.isPublic && (
                        <span className="circuit-lib-status status-approved">公共</span>
                      )}
                    </div>
                    <p className="circuit-lib-meta">
                      作者：{project.authorName || "未知"} · {formatCircuitTime(project.updatedAt)}
                    </p>
                    {project.description && (
                      <p className="circuit-lib-desc">{project.description}</p>
                    )}
                    <div className="circuit-lib-card-actions">
                      <a
                        className="sort-button home-canvas-link"
                        href={`/canvas/?group=${encodeURIComponent(activeGroupId)}&groupProject=${encodeURIComponent(project.id)}`}
                      >
                        <ExternalLink size={13} aria-hidden="true" />
                        <span>在画布中打开</span>
                      </a>
                    </div>
                  </article>
                ))}
              </div>
            )}
          </div>
        )
      )}
    </section>
  );
}
