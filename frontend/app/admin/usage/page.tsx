"use client";

import { Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { fetchMe } from "../../auth.ts";
import { clampPopoverLeft } from "../../effort-picker-logic";
import { PageHeader } from "../../components/PageHeader.tsx";
import { toUserMessage } from "../../errors.ts";
import {
  fetchAdminUsers,
  fetchOnlineIds,
  fetchUploadLimitDefault,
  FORBIDDEN_SENTINEL,
  resetAdminUserPassword,
  updateAdminUserRole,
  updateAdminUserUploadLimit,
  updateUploadLimitDefault,
  type AdminUserRole,
  type AdminUserUsage,
} from "./api.ts";
import {
  formatLastActive,
  logsDrillHref,
  parseUploadLimit,
  sortAdminUsers,
  type AdminUserSortKey,
  type SortDirection,
} from "./format.ts";
import { fetchUserNotebooks, notebookStatusLabel, type AdminUserNotebook } from "./notebooks.ts";
import "./usage.css";

type State =
  | { kind: "loading" }
  | { kind: "forbidden" }
  | { kind: "error"; message: string }
  | { kind: "ready"; rows: AdminUserUsage[] };

type NotebookCacheEntry = AdminUserNotebook[] | "loading" | "error";

const PAGE_SIZE_OPTIONS = [20, 50, 100] as const;

type SortableHeaderProps = {
  label: string;
  sortKey: AdminUserSortKey;
  activeKey: AdminUserSortKey;
  direction: SortDirection;
  onSort: (key: AdminUserSortKey) => void;
};

function SortableHeader({ label, sortKey, activeKey, direction, onSort }: SortableHeaderProps) {
  const active = activeKey === sortKey;
  const ariaSort = active ? (direction === "asc" ? "ascending" : "descending") : "none";
  return (
    <th aria-sort={ariaSort}>
      <button
        type="button"
        className={`usage-sort-button${active ? " usage-sort-button-active" : ""}`}
        onClick={() => onSort(sortKey)}
      >
        {label}
        <span className="usage-sort-indicator" aria-hidden="true">
          {active ? (direction === "asc" ? "▲" : "▼") : "↕"}
        </span>
      </button>
    </th>
  );
}

/** 弹出层与视口内缘、与锚点之间的留白。 */
const LIMIT_POPOVER_MARGIN = 8;
const LIMIT_POPOVER_GAP = 6;

type UploadLimitCellProps = {
  user: AdminUserUsage;
  /** 本行处于编辑态(弹出层打开)。 */
  editing: boolean;
  /** 其他行正在编辑,本行入口暂不可点。 */
  lockedByOther: boolean;
  /** 本行的保存/重置请求进行中。 */
  pending: boolean;
  /** 任一行请求进行中(入口统一禁点,镜像旧行为)。 */
  anyPending: boolean;
  input: string;
  onInput: (value: string) => void;
  onOpen: () => void;
  onCancel: () => void;
  onSave: () => void;
  onReset: () => void;
};

/**
 * 文档上限单元格:查看态常驻「数值 + 标签 + 编辑」,编辑控件放进锚定在按钮下方的
 * 浮动弹出层。此前编辑态在单元格里内联展开(输入框 + 三个按钮),会把整张表顶宽、
 * 出现横向滚动条,末尾按钮被截在可视区外——弹出层不占表格布局,行宽在两态间不变。
 */
function UploadLimitCell({
  user, editing, lockedByOther, pending, anyPending,
  input, onInput, onOpen, onCancel, onSave, onReset,
}: UploadLimitCellProps) {
  const anchorRef = useRef<HTMLDivElement | null>(null);
  const popoverRef = useRef<HTMLDivElement | null>(null);
  // null = 尚未量出位置(首帧绘制前由 layout effect 填上;jsdom 量不出宽度时保持 CSS 默认位)。
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);

  // 打开时量一次锚点与弹出层,定位到「编辑」按钮下方并夹回视口;表格横向滚动、页面
  // 滚动或窗口变化都会挪动锚点,所以 scroll(捕获)/resize 时重算。
  useLayoutEffect(() => {
    if (!editing) {
      setPos(null);
      return;
    }
    const sync = () => {
      const anchor = anchorRef.current;
      const popover = popoverRef.current;
      if (!anchor || !popover) return;
      const anchorBox = anchor.getBoundingClientRect();
      const popoverBox = popover.getBoundingClientRect();
      if (popoverBox.width <= 0) return; // 尚未布局(jsdom 全零),保持默认位
      const viewportWidth = window.innerWidth;
      const viewportHeight = window.innerHeight;
      const left = anchorBox.left + clampPopoverLeft({
        anchorLeft: anchorBox.left,
        anchorRight: anchorBox.right,
        popoverWidth: popoverBox.width,
        containerLeft: 0,
        containerRight: viewportWidth > 0 ? viewportWidth : Number.POSITIVE_INFINITY,
        margin: LIMIT_POPOVER_MARGIN,
      });
      let top = anchorBox.bottom + LIMIT_POPOVER_GAP;
      const flipped = anchorBox.top - LIMIT_POPOVER_GAP - popoverBox.height;
      if (
        viewportHeight > 0 &&
        top + popoverBox.height > viewportHeight - LIMIT_POPOVER_MARGIN &&
        flipped >= LIMIT_POPOVER_MARGIN
      ) {
        top = flipped; // 底部放不下且上方放得下时翻到锚点上方
      }
      setPos({ left, top });
    };
    sync();
    window.addEventListener("resize", sync);
    window.addEventListener("scroll", sync, true);
    // 无滚动/缩放的布局位移也要跟:校验/请求横幅插到表格上方、上方行展开笔记本清单
    // 都会把锚点向下推(codex R1 P2)。这些都会改变 body 高度,观察 body 尺寸即可覆盖;
    // 弹出层自身也观察——「保存」变「保存中…」会改宽度,右对齐的夹取要重算。
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(sync);
    if (observer) {
      observer.observe(document.body);
      if (popoverRef.current) observer.observe(popoverRef.current);
    }
    return () => {
      window.removeEventListener("resize", sync);
      window.removeEventListener("scroll", sync, true);
      observer?.disconnect();
    };
  }, [editing]);

  // 点外部 / Esc 关闭(镜像 EffortPicker);请求进行中不关,避免丢掉 pending 反馈。
  useEffect(() => {
    if (!editing) return;
    function handlePointerDown(event: PointerEvent) {
      if (pending) return;
      const target = event.target;
      if (target instanceof Node &&
        (popoverRef.current?.contains(target) || anchorRef.current?.contains(target))) return;
      onCancel();
    }
    function handleKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape" && !pending) onCancel();
    }
    window.addEventListener("pointerdown", handlePointerDown);
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("pointerdown", handlePointerDown);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [editing, pending, onCancel]);

  return (
    <div className="usage-limit-view" ref={anchorRef}>
      <span className="usage-limit-value">{user.upload_limit}</span>
      <span className={`usage-limit-tag${user.upload_limit_overridden ? " usage-limit-tag-custom" : ""}`}>
        {user.upload_limit_overridden ? "自定义" : "默认"}
      </span>
      <button
        type="button"
        className={`usage-role-button${editing ? " usage-role-button-open" : ""}`}
        disabled={anyPending || lockedByOther}
        aria-haspopup="dialog"
        aria-expanded={editing}
        onClick={() => (editing ? onCancel() : onOpen())}
      >编辑</button>
      {editing && (
        <div
          className="usage-limit-popover"
          role="dialog"
          aria-label={`设置 ${user.username} 的文档上限`}
          ref={popoverRef}
          style={pos === null ? undefined : { left: pos.left, top: pos.top }}
        >
          <div className="usage-limit-popover-title">文档上限 · {user.username}</div>
          <div className="usage-limit-popover-row">
            <input
              className="usage-limit-input usage-limit-popover-input"
              type="number"
              min={1}
              max={100000}
              value={input}
              disabled={pending}
              autoFocus
              aria-label={`${user.username} 的文档上限`}
              onChange={(event) => onInput(event.target.value)}
              onKeyDown={(event) => { if (event.key === "Enter") onSave(); }}
            />
            <button
              type="button"
              className="usage-role-button usage-role-button-confirm"
              disabled={pending}
              onClick={onSave}
            >
              {pending ? "保存中…" : "保存"}
            </button>
          </div>
          <div className="usage-limit-popover-row">
            <button
              type="button"
              className="usage-role-button"
              disabled={pending}
              title="恢复为默认文档上限"
              onClick={onReset}
            >重置默认</button>
            <button
              type="button"
              className="usage-role-button"
              disabled={pending}
              onClick={onCancel}
            >取消</button>
          </div>
        </div>
      )}
    </div>
  );
}

export default function AdminUsagePage() {
  const [state, setState] = useState<State>({ kind: "loading" });
  const [expanded, setExpanded] = useState<string | null>(null);
  const [nbCache, setNbCache] = useState<Record<string, NotebookCacheEntry>>({});
  const [onlineIds, setOnlineIds] = useState<Set<string>>(new Set());
  const [currentUserId, setCurrentUserId] = useState("");
  const [confirmingRole, setConfirmingRole] = useState<{ userId: string; role: AdminUserRole } | null>(null);
  const [rolePendingId, setRolePendingId] = useState("");
  const [roleNotice, setRoleNotice] = useState<{ kind: "ok" | "error"; message: string } | null>(null);
  const [uploadLimitDefault, setUploadLimitDefault] = useState<number | null>(null);
  const [defaultInput, setDefaultInput] = useState("");
  const [defaultSaving, setDefaultSaving] = useState(false);
  const [editingLimitId, setEditingLimitId] = useState("");
  const [limitInput, setLimitInput] = useState("");
  const [limitPendingId, setLimitPendingId] = useState("");
  const [limitNotice, setLimitNotice] = useState<{ kind: "ok" | "error"; message: string } | null>(null);
  const [resettingId, setResettingId] = useState("");
  const [resetInput, setResetInput] = useState("");
  const [resetPendingId, setResetPendingId] = useState("");
  const [resetNotice, setResetNotice] = useState<{ kind: "ok" | "error"; message: string } | null>(null);
  const [sortKey, setSortKey] = useState<AdminUserSortKey>("created_at");
  const [sortDirection, setSortDirection] = useState<SortDirection>("asc");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState<number>(PAGE_SIZE_OPTIONS[0]);

  useEffect(() => {
    (async () => {
      try {
        const me = await fetchMe();
        if (me.role !== "admin") {
          setState({ kind: "forbidden" });
          return;
        }
        setCurrentUserId(me.id);
        const [rows, limitDefault] = await Promise.all([fetchAdminUsers(), fetchUploadLimitDefault()]);
        setState({ kind: "ready", rows });
        setUploadLimitDefault(limitDefault);
        setDefaultInput(String(limitDefault));
        setOnlineIds(new Set(rows.filter((r) => r.is_online).map((r) => r.id)));
      } catch (e) {
        // 哨兵先判(分流到专用无权限视图),其余一律过人话层——此前这里直出
        // e.message,断网时页面上会写「加载失败:Failed to fetch」。
        if (e instanceof Error && e.message === FORBIDDEN_SENTINEL) {
          setState({ kind: "forbidden" });
          return;
        }
        setState({ kind: "error", message: toUserMessage(e, "请稍后重试") });
      }
    })();
  }, []);

  useEffect(() => {
    if (state.kind !== "ready") return;
    const timer = setInterval(async () => {
      try {
        setOnlineIds(new Set(await fetchOnlineIds()));
      } catch {
        // 忽略瞬时失败,下个周期重试
      }
    }, 15000);
    return () => clearInterval(timer);
  }, [state.kind]);

  async function toggleExpand(userId: string) {
    if (expanded === userId) {
      setExpanded(null);
      return;
    }
    setExpanded(userId);
    if (nbCache[userId] !== undefined) return; // 已缓存,不重复请求
    setNbCache((prev) => ({ ...prev, [userId]: "loading" }));
    try {
      const notebooks = await fetchUserNotebooks(userId);
      setNbCache((prev) => ({ ...prev, [userId]: notebooks }));
    } catch {
      setNbCache((prev) => ({ ...prev, [userId]: "error" }));
    }
  }

  async function submitRoleChange(target: AdminUserUsage, role: AdminUserRole) {
    setRolePendingId(target.id);
    setRoleNotice(null);
    try {
      const updated = await updateAdminUserRole(target.id, role);
      setState((previous) => previous.kind === "ready"
        ? {
            kind: "ready",
            rows: previous.rows.map((row) => row.id === updated.id
              ? { ...row, role: updated.role }
              : row),
          }
        : previous);
      setConfirmingRole(null);
      setRoleNotice({
        kind: "ok",
        message: role === "admin"
          ? `已授予 ${updated.username} 管理员权限`
          : `已撤销 ${updated.username} 的管理员权限`,
      });
    } catch (error) {
      if (error instanceof Error && error.message === FORBIDDEN_SENTINEL) {
        setState({ kind: "forbidden" });
        return;
      }
      setRoleNotice({ kind: "error", message: toUserMessage(error, "权限更新失败，请稍后重试") });
    } finally {
      setRolePendingId("");
    }
  }

  async function saveDefault() {
    const parsed = parseUploadLimit(defaultInput);
    if (parsed === null) {
      setLimitNotice({ kind: "error", message: "请输入 1 到 100000 之间的整数" });
      return;
    }
    setDefaultSaving(true);
    setLimitNotice(null);
    try {
      const saved = await updateUploadLimitDefault(parsed);
      setUploadLimitDefault(saved);
      setDefaultInput(String(saved));
      // 没有单独设置的用户,有效上限跟随全局默认——就地刷新表格,免整表重取。
      setState((previous) => previous.kind === "ready"
        ? {
            kind: "ready",
            rows: previous.rows.map((row) => row.upload_limit_overridden ? row : { ...row, upload_limit: saved }),
          }
        : previous);
      setLimitNotice({ kind: "ok", message: `已将默认文档上限设为 ${saved}` });
    } catch (error) {
      if (error instanceof Error && error.message === FORBIDDEN_SENTINEL) {
        setState({ kind: "forbidden" });
        return;
      }
      setLimitNotice({ kind: "error", message: toUserMessage(error, "默认文档上限更新失败，请稍后重试") });
    } finally {
      setDefaultSaving(false);
    }
  }

  // limit=null 清除覆盖(回落全局默认);其余是显式覆盖值。镜像 submitRoleChange 的
  // 哨兵分流 + 就地更新对应行 + 人话层错误。
  async function submitLimitChange(target: AdminUserUsage, limit: number | null) {
    setLimitPendingId(target.id);
    setLimitNotice(null);
    try {
      const updated = await updateAdminUserUploadLimit(target.id, limit);
      setState((previous) => previous.kind === "ready"
        ? {
            kind: "ready",
            rows: previous.rows.map((row) => row.id === updated.id
              ? { ...row, upload_limit: updated.upload_limit, upload_limit_overridden: updated.upload_limit_overridden }
              : row),
          }
        : previous);
      setEditingLimitId("");
      setLimitNotice({
        kind: "ok",
        message: updated.upload_limit_overridden
          ? `已将 ${updated.username} 的文档上限设为 ${updated.upload_limit}`
          : `已恢复 ${updated.username} 的文档上限为默认值（${updated.upload_limit}）`,
      });
    } catch (error) {
      if (error instanceof Error && error.message === FORBIDDEN_SENTINEL) {
        setState({ kind: "forbidden" });
        return;
      }
      setLimitNotice({ kind: "error", message: toUserMessage(error, "文档上限更新失败，请稍后重试") });
    } finally {
      setLimitPendingId("");
    }
  }

  function saveLimit(target: AdminUserUsage) {
    const parsed = parseUploadLimit(limitInput);
    if (parsed === null) {
      setLimitNotice({ kind: "error", message: "请输入 1 到 100000 之间的整数" });
      return;
    }
    void submitLimitChange(target, parsed);
  }

  // 镜像 submitRoleChange 的哨兵分流 + 人话层错误;成功后目标用户全部会话被
  // 吊销(纯后端行为,这里只负责收起编辑态并给出提示)。
  async function submitResetPassword(target: AdminUserUsage) {
    if (!resetInput.trim()) {
      setResetNotice({ kind: "error", message: "请输入新密码" });
      return;
    }
    setResetPendingId(target.id);
    setResetNotice(null);
    try {
      const updated = await resetAdminUserPassword(target.id, resetInput);
      setResettingId("");
      setResetInput("");
      setResetNotice({
        kind: "ok",
        message: `已重置 ${updated.username} 的密码，该用户需用新密码重新登录`,
      });
    } catch (error) {
      if (error instanceof Error && error.message === FORBIDDEN_SENTINEL) {
        setState({ kind: "forbidden" });
        return;
      }
      setResetNotice({ kind: "error", message: toUserMessage(error, "密码重置失败，请稍后重试") });
    } finally {
      setResetPendingId("");
    }
  }

  function resetRowInteractions() {
    setExpanded(null);
    setConfirmingRole(null);
    setEditingLimitId("");
    setResettingId("");
    // 收起编辑态时同步清掉已输入的明文新密码,别让凭据在 state 里过夜。
    setResetInput("");
  }

  function changeSort(nextKey: AdminUserSortKey) {
    if (sortKey === nextKey) {
      setSortDirection((previous) => previous === "asc" ? "desc" : "asc");
    } else {
      setSortKey(nextKey);
      setSortDirection("asc");
    }
    setPage(1);
    resetRowInteractions();
  }

  const readyRows = state.kind === "ready" ? state.rows : [];
  const sortedRows = useMemo(
    () => sortAdminUsers(readyRows, sortKey, sortDirection),
    [readyRows, sortKey, sortDirection],
  );
  const pageCount = Math.max(1, Math.ceil(sortedRows.length / pageSize));
  const currentPage = Math.min(page, pageCount);
  const visibleRows = sortedRows.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  if (state.kind === "loading") return <main className="usage-page">加载中…</main>;
  if (state.kind === "forbidden")
    return <main className="usage-page usage-empty">无权限:仅管理员可查看用户使用总览。</main>;
  if (state.kind === "error")
    return <main className="usage-page usage-empty">加载失败:{state.message}</main>;

  function changePage(nextPage: number) {
    setPage(Math.max(1, Math.min(nextPage, pageCount)));
    resetRowInteractions();
  }

  return (
    <main className="usage-page">
      <PageHeader title="用户使用总览" />
      <p className="usage-description">查看用户用量，配置文档数量上限，并授予或撤销管理员权限。</p>
      <div className="usage-settings-bar">
        <label className="usage-settings-label" htmlFor="upload-limit-default">普通用户默认文档上限</label>
        <input
          id="upload-limit-default"
          className="usage-limit-input"
          type="number"
          min={1}
          max={100000}
          value={defaultInput}
          disabled={uploadLimitDefault === null || defaultSaving}
          onChange={(event) => setDefaultInput(event.target.value)}
        />
        <button
          type="button"
          className="usage-role-button usage-role-button-confirm"
          disabled={uploadLimitDefault === null || defaultSaving}
          onClick={() => void saveDefault()}
        >
          {defaultSaving ? "保存中…" : "保存"}
        </button>
        <span className="usage-settings-hint">管理员的笔记本不受限；为某位用户单独设置后，以其设置为准。</span>
      </div>
      {roleNotice && (
        <div className={`usage-role-notice usage-role-notice-${roleNotice.kind}`} role="status">
          {roleNotice.message}
        </div>
      )}
      {limitNotice && (
        <div className={`usage-role-notice usage-role-notice-${limitNotice.kind}`} role="status">
          {limitNotice.message}
        </div>
      )}
      {resetNotice && (
        <div className={`usage-role-notice usage-role-notice-${resetNotice.kind}`} role="status">
          {resetNotice.message}
        </div>
      )}
      <div className="usage-table-wrap">
        <table className="usage-table">
          <thead>
            <tr>
              <th className="usage-expand-col"></th>
              <SortableHeader label="用户名" sortKey="username" activeKey={sortKey} direction={sortDirection} onSort={changeSort} />
              <SortableHeader label="角色" sortKey="role" activeKey={sortKey} direction={sortDirection} onSort={changeSort} />
              <SortableHeader label="注册时间" sortKey="created_at" activeKey={sortKey} direction={sortDirection} onSort={changeSort} />
              <SortableHeader label="笔记本" sortKey="notebooks" activeKey={sortKey} direction={sortDirection} onSort={changeSort} />
              <SortableHeader label="来源" sortKey="sources" activeKey={sortKey} direction={sortDirection} onSort={changeSort} />
              <SortableHeader label="提问" sortKey="questions" activeKey={sortKey} direction={sortDirection} onSort={changeSort} />
              <SortableHeader label="报告" sortKey="reports" activeKey={sortKey} direction={sortDirection} onSort={changeSort} />
              <SortableHeader label="最近活跃" sortKey="last_active" activeKey={sortKey} direction={sortDirection} onSort={changeSort} />
              <th>用户分析</th>
              <SortableHeader label="文档上限" sortKey="upload_limit" activeKey={sortKey} direction={sortDirection} onSort={changeSort} />
              <th>密码</th>
              <th>权限管理</th>
            </tr>
          </thead>
          <tbody>
          {visibleRows.map((u) => {
            const isOpen = expanded === u.id;
            const entry = nbCache[u.id];
            const isOnline = onlineIds.has(u.id);
            return (
              <Fragment key={u.id}>
                <tr className={isOpen ? "usage-row-expanded" : undefined}>
                  <td className="usage-expand-col">
                    <button
                      type="button"
                      className="usage-expand-btn"
                      aria-expanded={isOpen}
                      aria-label={isOpen ? "收起笔记本列表" : "展开笔记本列表"}
                      onClick={() => toggleExpand(u.id)}
                    >
                      {isOpen ? "▾" : "▸"}
                    </button>
                  </td>
                  <td>
                    <span
                      className={`usage-dot ${isOnline ? "usage-dot-online" : "usage-dot-offline"}`}
                      role="img"
                      aria-label={isOnline ? "在线" : "离线"}
                      title={isOnline ? "在线" : "离线"}
                    />
                    {u.username}
                  </td>
                  <td>{u.role === "admin" ? "管理员" : "用户"}</td>
                  <td>{formatLastActive(u.created_at)}</td>
                  <td>{u.notebooks}</td>
                  <td>{u.sources}</td>
                  <td>{u.questions}</td>
                  <td>{u.reports}</td>
                  <td>{formatLastActive(u.last_active)}</td>
                  <td><a href={logsDrillHref(u.id)}>查看提问</a></td>
                  <td className="usage-limit-cell">
                    {u.role === "admin" ? (
                      // 管理员的笔记本豁免(写路径 owner-only ⇒ owner 即当前 admin),显示「不限」不可编辑。
                      <span className="usage-role-locked">不限</span>
                    ) : (
                      <UploadLimitCell
                        user={u}
                        editing={editingLimitId === u.id}
                        lockedByOther={Boolean(editingLimitId) && editingLimitId !== u.id}
                        pending={limitPendingId === u.id}
                        anyPending={Boolean(limitPendingId)}
                        input={limitInput}
                        onInput={setLimitInput}
                        onOpen={() => { setEditingLimitId(u.id); setLimitInput(String(u.upload_limit)); setLimitNotice(null); }}
                        onCancel={() => setEditingLimitId("")}
                        onSave={() => saveLimit(u)}
                        onReset={() => void submitLimitChange(u, null)}
                      />
                    )}
                  </td>
                  <td className="usage-limit-cell">
                    {u.id === "user-local" ? (
                      <span className="usage-role-locked" title="内置管理员密码由部署配置决定">受保护</span>
                    ) : u.id === currentUserId ? (
                      <span className="usage-role-locked" title="请在主界面头像菜单中修改自己的密码">本人</span>
                    ) : resettingId === u.id ? (
                      <span className="usage-limit-edit">
                        <input
                          className="usage-limit-input"
                          type="password"
                          autoComplete="new-password"
                          value={resetInput}
                          disabled={resetPendingId === u.id}
                          aria-label={`${u.username} 的新密码`}
                          onChange={(event) => setResetInput(event.target.value)}
                        />
                        <button
                          type="button"
                          className="usage-role-button usage-role-button-confirm"
                          disabled={resetPendingId === u.id}
                          onClick={() => void submitResetPassword(u)}
                        >
                          {resetPendingId === u.id ? "重置中…" : "确认重置"}
                        </button>
                        <button
                          type="button"
                          className="usage-role-button"
                          disabled={resetPendingId === u.id}
                          onClick={() => { setResettingId(""); setResetInput(""); }}
                        >取消</button>
                      </span>
                    ) : (
                      <button
                        type="button"
                        className="usage-role-button"
                        disabled={Boolean(resetPendingId) || Boolean(resettingId)}
                        onClick={() => { setResettingId(u.id); setResetInput(""); setResetNotice(null); }}
                      >重置密码</button>
                    )}
                  </td>
                  <td>
                    {!u.role_mutable ? (
                      <span className="usage-role-locked">
                        {u.id === currentUserId ? "当前账户" : "受保护"}
                      </span>
                    ) : confirmingRole?.userId === u.id ? (
                      <span className="usage-role-confirm">
                        <button
                          type="button"
                          className="usage-role-button usage-role-button-confirm"
                          disabled={rolePendingId === u.id}
                          onClick={() => void submitRoleChange(u, confirmingRole.role)}
                        >
                          {rolePendingId === u.id ? "更新中…" : "确认"}
                        </button>
                        <button
                          type="button"
                          className="usage-role-button"
                          disabled={rolePendingId === u.id}
                          onClick={() => setConfirmingRole(null)}
                        >取消</button>
                      </span>
                    ) : (
                      <button
                        type="button"
                        className="usage-role-button"
                        disabled={Boolean(rolePendingId)}
                        onClick={() => setConfirmingRole({
                          userId: u.id,
                          role: u.role === "admin" ? "user" : "admin",
                        })}
                      >
                        {u.role === "admin" ? "撤销管理员" : "设为管理员"}
                      </button>
                    )}
                  </td>
                </tr>
                {isOpen && (
                  <tr className="usage-subrow">
                    <td colSpan={13}>
                      {entry === "loading" && (
                        <div className="usage-subtable-status">加载中…</div>
                      )}
                      {entry === "error" && (
                        <div className="usage-subtable-status usage-subtable-error">加载失败,请重试。</div>
                      )}
                      {Array.isArray(entry) && entry.length === 0 && (
                        <div className="usage-subtable-status">该用户暂无笔记本。</div>
                      )}
                      {Array.isArray(entry) && entry.length > 0 && (
                        <table className="usage-subtable">
                          <thead>
                            <tr>
                              <th>笔记本</th><th>状态</th>
                              <th>来源</th><th>提问</th><th>报告</th>
                              <th>创建</th><th>最近更新</th>
                            </tr>
                          </thead>
                          <tbody>
                            {entry.map((nb) => (
                              <tr key={nb.id}>
                                <td>{nb.name}</td>
                                <td>{notebookStatusLabel(nb.status)}</td>
                                <td>{nb.sources}</td>
                                <td>{nb.questions}</td>
                                <td>{nb.reports}</td>
                                <td>{formatLastActive(nb.created_at)}</td>
                                <td>{formatLastActive(nb.updated_at)}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      )}
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
          </tbody>
        </table>
      </div>
      <nav className="usage-pagination" aria-label="用户列表分页">
        <span className="usage-pagination-summary">
          第 {currentPage} / {pageCount} 页，共 {sortedRows.length} 位用户
        </span>
        <label className="usage-page-size-label">
          每页
          <select
            className="usage-page-size-select"
            aria-label="每页用户数"
            value={pageSize}
            onChange={(event) => {
              setPageSize(Number(event.target.value));
              setPage(1);
              resetRowInteractions();
            }}
          >
            {PAGE_SIZE_OPTIONS.map((option) => <option key={option} value={option}>{option}</option>)}
          </select>
          条
        </label>
        <div className="usage-pagination-actions">
          <button type="button" disabled={currentPage === 1} onClick={() => changePage(1)}>首页</button>
          <button type="button" disabled={currentPage === 1} onClick={() => changePage(currentPage - 1)}>上一页</button>
          <button type="button" disabled={currentPage === pageCount} onClick={() => changePage(currentPage + 1)}>下一页</button>
          <button type="button" disabled={currentPage === pageCount} onClick={() => changePage(pageCount)}>末页</button>
        </div>
      </nav>
    </main>
  );
}
