import { useEffect, useRef, useState } from "react";
import {
  BarChart3,
  Bookmark,
  ChevronDown,
  KeyRound,
  LogOut,
  Puzzle,
  HeartHandshake,
  MessagesSquare,
  SlidersHorizontal,
  Users,
  Wand2,
} from "lucide-react";


type AccountMenuProps = {
  username: string;
  role: string;
  initials: string;
  memoryActive: boolean;
  showAdminUsage: boolean;
  /** 内置管理员(密码由部署配置派生)不显示「修改密码」入口——后端对它一律 409。 */
  canChangePassword: boolean;
  /** 当前是否为高级模式；开关渲染其开/关态，点击调用 onToggleAdvancedMode。 */
  advancedMode: boolean;
  /** 「我的回答偏好」入口的部署总闸（/system/config 的
   *  user_search_profile_enabled，缺失按 true）；关闭时整个菜单项不渲染。 */
  searchProfileEnabled: boolean;
  /** 管理员「提问分析」入口的部署能力位。 */
  activityViewEnabled: boolean;
  onOpenMemory: () => void;
  /** 打开独立群组管理页。任何登录用户都可用（项目组人人可建）。 */
  onOpenGroups: () => void;
  onToggleAdvancedMode: () => void;
  onOpenSearchProfile: () => void;
  onChangePassword: () => void;
  onLogout: () => void | Promise<void>;
};


export function AccountMenu({
  username,
  role,
  initials,
  memoryActive,
  showAdminUsage,
  canChangePassword,
  advancedMode,
  searchProfileEnabled,
  activityViewEnabled,
  onOpenMemory,
  onOpenGroups,
  onToggleAdvancedMode,
  onOpenSearchProfile,
  onChangePassword,
  onLogout,
}: AccountMenuProps) {
  const [open, setOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;

    function close() {
      setOpen(false);
    }

    function handlePointerDown(event: PointerEvent) {
      const target = event.target;
      if (target instanceof Node && menuRef.current?.contains(target)) {
        return;
      }
      close();
    }

    function handleKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") close();
    }

    window.addEventListener("pointerdown", handlePointerDown);
    window.addEventListener("keydown", handleKeyDown);
    window.addEventListener("resize", close);
    return () => {
      window.removeEventListener("pointerdown", handlePointerDown);
      window.removeEventListener("keydown", handleKeyDown);
      window.removeEventListener("resize", close);
    };
  }, [open]);

  const accountRole = role === "admin" ? "管理员" : "用户";

  return (
    <div className="user-menu" ref={menuRef}>
      <button
        aria-label="账户菜单"
        className="user-menu-trigger"
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        title="账户菜单"
        onClick={() => setOpen((value) => !value)}
      >
        <span className="user-avatar">{initials}</span>
        <span className="user-name">
          {username}{role === "admin" ? "（管理员）" : ""}
        </span>
        <ChevronDown size={14} className="user-menu-chevron" />
      </button>
      {open && (
        <div className="user-menu-popover" role="menu" aria-label="账户菜单">
          <div className="user-menu-profile">
            <span className="user-avatar large">{initials}</span>
            <div>
              <strong>{username}</strong>
              <small>{accountRole}</small>
            </div>
          </div>
          <button
            className={`user-logout ${memoryActive ? "active" : ""}`}
            type="button"
            role="menuitem"
            onClick={() => {
              setOpen(false);
              onOpenMemory();
            }}
          >
            <Bookmark size={16} />
            <span>私有记忆</span>
          </button>
          <a
            className="user-logout"
            role="menuitem"
            href="/wishes"
            title="提交问题、功能需求或查看更新计划"
          >
            <HeartHandshake size={16} />
            <span>许愿墙</span>
          </a>
          <button
            className="user-logout"
            type="button"
            role="menuitem"
            title="管理群组成员，并查看共享给群组的知识库"
            onClick={() => {
              setOpen(false);
              onOpenGroups();
            }}
          >
            <Users size={16} />
            <span>群组</span>
          </button>
          <button
            className={`user-logout user-menu-toggle ${advancedMode ? "active" : ""}`}
            type="button"
            role="menuitemcheckbox"
            aria-checked={advancedMode}
            title="高级模式会显示引擎切换、检索档位、研究深度与来源范围勾选等完整配置项"
            onClick={() => {
              setOpen(false);
              onToggleAdvancedMode();
            }}
          >
            <SlidersHorizontal size={16} />
            <span>高级模式</span>
            <span className="user-menu-state">{advancedMode ? "已开启" : "已关闭"}</span>
          </button>
          {searchProfileEnabled && (
            <button
              className="user-logout"
              type="button"
              role="menuitem"
              title="设置回答的语言、组织方式、详略与常用术语"
              onClick={() => {
                setOpen(false);
                onOpenSearchProfile();
              }}
            >
              <Wand2 size={16} />
              <span>我的回答偏好</span>
            </button>
          )}
          {canChangePassword && (
            <button
              className="user-logout"
              type="button"
              role="menuitem"
              onClick={() => {
                setOpen(false);
                onChangePassword();
              }}
            >
              <KeyRound size={16} />
              <span>修改密码</span>
            </button>
          )}
          {showAdminUsage && (
            <a
              className="user-logout"
              role="menuitem"
              href="/admin/usage"
              title="用户使用总览"
            >
              <BarChart3 size={16} />
              <span>用户总览</span>
            </a>
          )}
          {showAdminUsage && activityViewEnabled && (
            <a
              className="user-logout"
              role="menuitem"
              href="/admin/questions"
              title="跨用户查看问答与深度报告中的提问"
            >
              <MessagesSquare size={16} />
              <span>提问分析</span>
            </a>
          )}
          {/* 管理员页面共用 showAdminUsage / canSeeAdminUsage 角色判据；提问分析
              还需匹配后端 USER_ACTIVITY_VIEW_ENABLED 能力位。 */}
          {showAdminUsage && (
            <a
              className="user-logout"
              role="menuitem"
              href="/admin/extensions"
              title="查看这个服务已经装入的扩展"
            >
              <Puzzle size={16} />
              <span>已加载的扩展</span>
            </a>
          )}
          <button
            className="user-logout"
            type="button"
            role="menuitem"
            onClick={() => {
              setOpen(false);
              void onLogout();
            }}
          >
            <LogOut size={16} />
            <span>退出登录</span>
          </button>
        </div>
      )}
    </div>
  );
}
