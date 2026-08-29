"use client";

// 「已加载的扩展」——管理员页,`GET /admin/extensions` 的前端对等面,外加一处写
// 动作:部署插件的运行时开关(`PATCH /admin/extensions/{plugin_id}`)。
//
// 壳层逐条镜像 admin/usage/page.tsx:同一个 `fetchMe` 登录门(先判角色、再取数据)、
// 同一套四态(加载中 / 无权限 / 加载失败 / 就绪)、同一个 PageHeader、失败一律经
// `toUserMessage` 出文案。与 usage 的差别只有一处,是刻意的:这里没有 usage 那个
// `FORBIDDEN_SENTINEL`。403 的识别改用 `httpErrorStatus`——它读的是 errors.ts
// 挂在人话错误上的状态码 Symbol,不必读 `.message`,因此不用往 errors-guard 的
// 精确计数清单里加一笔。同理失败态那个字段叫 `notice` 而不是 `message`:它装的是
// **已经过人话层**的文案,不是某个 Error 的 `.message`,借用那个名字只会在普查里
// 挂一条名不副实的豁免。
//
// 页面**不再是纯只读**:运行时开关是行级写动作,忙碌态、成功态与失败态都必须落在
// 触发它的那一行(按钮自身 + 紧邻的行内文案),绝不发页面顶部横幅——顶部只留给
// `versionRecognized` 这类「与哪一行都无关」的整页级提示。每个插件的 pending/error
// 状态各自独立存在 `runtimeRowState`(keyed by plugin id),互不影响;成功后的新
// 状态完全来自服务端响应,不做乐观更新。
import { Fragment, useEffect, useState } from "react";

import { fetchMe } from "../../auth.ts";
import { PageHeader } from "../../components/PageHeader.tsx";
import { httpErrorStatus, toUserMessage } from "../../errors.ts";
import { label } from "../../vocabulary.ts";
import {
  fetchLoadedExtensions,
  KNOWN_API_VERSION,
  setExtensionRuntimeEnabled,
  type LoadedExtension,
  type LoadedExtensionTopology,
} from "./api.ts";
import "./extensions.css";

type State =
  | { kind: "loading" }
  | { kind: "forbidden" }
  | { kind: "error"; notice: string }
  | { kind: "ready"; topology: LoadedExtensionTopology };

// 后端 `trust` 的封闭取值(api.ts 已把认不出的值归成 "unknown")。
const TRUST_LABELS: Record<string, string> = {
  builtin: "内置",
  deployment: "部署装入",
  unknown: "未知",
};

// 后端 ContributionKind 的封闭取值。经 vocabulary 的 `label()` 出词,避免把英文
// 枚举 id 直接上屏(raw-enum-fallback 守卫盯的正是这类兜底)。
const CONTRIBUTION_KIND_LABELS: Record<string, string> = {
  provider: "唯一实现",
  provider_chain: "实现链",
  contributor: "补充",
  observer: "完成后通知",
};

/** 空值不进 `label()`——那会为一个已知的「后端没给这个字段」记一条误导性的控制台报错。 */
function kindLabel(kind: string): string {
  return kind ? label(CONTRIBUTION_KIND_LABELS, kind, "其他") : "—";
}

function displayNameOf(extension: LoadedExtension): string {
  return extension.displayName || extension.id;
}

/** 单个插件行上,「运行时开关」这个写动作眼下所处的状态。默认(不在 map 里)按
 *  `idle` 处理——多数行从没被点过,不必为每一行预先塞一条记录。 */
type RuntimeRowState =
  | { kind: "idle" }
  | { kind: "pending" }
  | { kind: "error"; notice: string };

const IDLE_ROW_STATE: RuntimeRowState = { kind: "idle" };

/** SQLite 裸本地 ISO(无时区)与 PG 带 `+00:00` 偏移两种形状都交给 `new Date()`
 *  解析,绝不做字符串切片。解析不出来时返回空串,调用方据此隐藏这段文案,而不是
 *  把 "Invalid Date" 露给用户。 */
function formatRuntimeTimestamp(iso: string | null): string {
  if (!iso) return "";
  const timestamp = new Date(iso);
  if (!Number.isFinite(timestamp.getTime())) return "";
  return timestamp.toLocaleString("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  });
}

/** 「谁、何时」的一句审计微文案。两个来源都可能单独缺席(比如时间戳解析失败),
 *  缺其一时只说另一半,都没有就返回空串——调用方据此整段不渲染。时间与「由」
 *  之间用仓库既有的 " · " 分隔惯例(见 answer-panel.tsx/memory-panel.tsx 等),
 *  不是两个词粘在一起。 */
function runtimeAuditText(updatedBy: string | null, updatedAt: string | null): string {
  const when = formatRuntimeTimestamp(updatedAt);
  if (updatedBy && when) return `${when} · 由 ${updatedBy} 更新`;
  if (when) return `${when} 更新`;
  if (updatedBy) return `由 ${updatedBy} 更新`;
  return "";
}

/** 部署插件一行的「运行状态」单元格:徽标 + 启停按钮 + 审计/错误微文案。
 *  builtin 行不经过这里(调用方直接渲染静态文字)。
 *
 *  `runtimeEnabled` 认不出/缺失(null)时按启用处理——与后端「无行=启用」同一
 *  条兜底教义,不把一次解析失败误判成「已停用」。 */
function ExtensionRuntimeCell({
  extension,
  rowState,
  onToggle,
}: {
  extension: LoadedExtension;
  rowState: RuntimeRowState;
  onToggle: (pluginId: string, nextEnabled: boolean) => void;
}) {
  const enabled = extension.runtimeEnabled !== false;
  const pending = rowState.kind === "pending";
  const audit = runtimeAuditText(extension.runtimeUpdatedBy, extension.runtimeUpdatedAt);
  // 按钮的可见文案与可访问名同一个值:WCAG 2.5.3(Label in Name)要求可见文案
  // 必须是可访问名的子串,这里干脆让两者相等,再在后面缀插件名消歧——与本文件
  // 展开按钮 `aria-label={`${isOpen ? "收起" : "展开"} ${name} 的接入明细`}`
  // 同一个规矩:可访问名 = 这个按钮此刻会执行的动作 + 对象是谁。
  const actionLabel = pending ? (enabled ? "停用中…" : "启用中…") : (enabled ? "停用" : "启用");
  const name = displayNameOf(extension);
  return (
    <div className="ext-runtime-cell">
      <div className="ext-runtime-controls">
        <span className={`ext-runtime-badge${enabled ? "" : " ext-runtime-badge-disabled"}`}>
          {enabled ? "已启用" : "已停用"}
        </span>
        <button
          type="button"
          className="ext-runtime-btn"
          disabled={pending}
          aria-busy={pending || undefined}
          aria-label={`${actionLabel} ${name}`}
          onClick={() => onToggle(extension.id, !enabled)}
        >
          {actionLabel}
        </button>
      </div>
      {audit && <p className="ext-runtime-audit">{audit}</p>}
      {rowState.kind === "error" && (
        <p className="ext-runtime-error" role="alert">操作没成功：{rowState.notice}</p>
      )}
    </div>
  );
}

function ExtensionDetail({ extension }: { extension: LoadedExtension }) {
  return (
    <div className="ext-detail">
      <section>
        <h3 className="ext-detail-title">服务端接入</h3>
        {extension.contributions.length === 0 ? (
          <p className="ext-none">没有服务端接入。</p>
        ) : (
          <table className="ext-subtable">
            <thead>
              <tr>
                <th>标识</th>
                <th>扩展点</th>
                <th>类型</th>
              </tr>
            </thead>
            <tbody>
              {extension.contributions.map((item) => (
                <tr key={item.id}>
                  <td className="ext-id">{item.id}</td>
                  <td className="ext-id">{item.point}</td>
                  <td title={item.kind}>{kindLabel(item.kind)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
      <section>
        <h3 className="ext-detail-title">界面入口</h3>
        {extension.uiContributions.length === 0 ? (
          <p className="ext-none">没有界面入口。</p>
        ) : (
          <table className="ext-subtable">
            <thead>
              <tr>
                <th>标识</th>
                <th>界面槽位</th>
                <th>能力</th>
              </tr>
            </thead>
            <tbody>
              {extension.uiContributions.map((item) => (
                <tr key={item.id}>
                  <td className="ext-id">{item.id}</td>
                  <td className="ext-id">{item.slot}</td>
                  <td className="ext-id">{item.capability}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}

export default function AdminExtensionsPage() {
  const [state, setState] = useState<State>({ kind: "loading" });
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  // 每个插件各自的运行时开关写动作状态,keyed by plugin id。不在表里的行按
  // `IDLE_ROW_STATE` 处理——大多数行从没被点过,不必预填。
  const [runtimeRowState, setRuntimeRowState] = useState<Record<string, RuntimeRowState>>({});

  useEffect(() => {
    (async () => {
      try {
        const me = await fetchMe();
        if (me.role !== "admin") {
          setState({ kind: "forbidden" });
          return;
        }
        setState({ kind: "ready", topology: await fetchLoadedExtensions() });
      } catch (e) {
        // 403 分流到专用无权限视图(状态码经 errors.ts 的品牌读回,不读 `.message`);
        // 其余一律过人话层,不把 "Failed to fetch" 这类原文摆到界面上。
        if (httpErrorStatus(e) === 403) {
          setState({ kind: "forbidden" });
          return;
        }
        setState({ kind: "error", notice: toUserMessage(e, "请稍后重试") });
      }
    })();
  }, []);

  if (state.kind === "loading") return <main className="ext-page ext-status">加载中…</main>;
  if (state.kind === "forbidden")
    return <main className="ext-page ext-status">无权限：仅管理员可查看已加载的扩展。</main>;
  if (state.kind === "error")
    return <main className="ext-page ext-status">加载失败：{state.notice}</main>;

  const { topology } = state;
  const versionNotice = topology.apiVersion
    ? `服务端声明的清单版本是「${topology.apiVersion}」，本页面按「${KNOWN_API_VERSION}」解读，下面的内容可能不完整。`
    : "没能读到服务端的清单版本，下面的内容可能不完整。";

  function toggle(id: string) {
    setExpanded((current) => {
      const next = new Set(current);
      if (!next.delete(id)) next.add(id);
      return next;
    });
  }

  // 行级写动作:忙碌态先落地,再等服务端响应。成功后的新状态完全来自响应体
  // (`result.*`),不做乐观更新——写失败时行上留的必须是写之前的真实状态,而
  // 不是一个已经翻转过、随后又要翻回去的临时值。失败只更新这一个 plugin id 的
  // 行状态,其余行(包括同时在途的另一次点击)不受影响。
  //
  // 行 key 全程用**请求**的 `pluginId`,不用响应体回声的 `result.pluginId`:
  // 响应只负责填字段值(runtimeEnabled/updatedBy/updatedAt)。这不是同一个值
  // 换个来源那么简单——按响应回声的 id 去 `.map` 查找,一旦回声与请求不一致
  // (畸形响应、契约漂移),要么谁都匹配不上(这次点击的行悄悄没有得到更新,
  // 却也不报错)、要么更糟地匹配上另一个恰好同名的插件行,把这次写入的结果
  // 误写到用户根本没点过的那一行上。按请求 `pluginId` 查找不存在这个问题:
  // 无论响应体里的 id 字段是什么,更新的永远是用户实际点击的那一行。
  async function handleRuntimeToggle(pluginId: string, nextEnabled: boolean) {
    setRuntimeRowState((current) => ({ ...current, [pluginId]: { kind: "pending" } }));
    try {
      const result = await setExtensionRuntimeEnabled(pluginId, nextEnabled);
      setState((current) => {
        if (current.kind !== "ready") return current;
        return {
          kind: "ready",
          topology: {
            ...current.topology,
            extensions: current.topology.extensions.map((extension) =>
              extension.id === pluginId
                ? {
                    ...extension,
                    runtimeEnabled: result.runtimeEnabled,
                    runtimeUpdatedBy: result.runtimeUpdatedBy,
                    runtimeUpdatedAt: result.runtimeUpdatedAt,
                  }
                : extension,
            ),
          },
        };
      });
      setRuntimeRowState((current) => ({ ...current, [pluginId]: IDLE_ROW_STATE }));
    } catch (error) {
      setRuntimeRowState((current) => ({
        ...current,
        [pluginId]: { kind: "error", notice: toUserMessage(error, "请稍后重试") },
      }));
    }
  }

  return (
    <main className="ext-page">
      <PageHeader title="已加载的扩展" />
      <p className="ext-description">
        扩展的装载在服务启动时一次性完成并固定：未在部署配置里点名、或点名但被设为不装载的扩展，都不会出现在这里，需要修改部署配置并重启服务才会生效。
        信任档位为「部署装入」的扩展可以在下面直接启停——点击即时写入，对当前服务进程立即生效，其它服务进程通常数秒内一并收敛；内置扩展始终启用，不支持在此关闭。
      </p>
      {!topology.versionRecognized && (
        <div className="ext-notice" role="status">{versionNotice}</div>
      )}
      {topology.extensions.length === 0 ? (
        <p className="ext-status">没有读到任何扩展。服务默认就带有内置扩展，请检查服务端配置与启动日志。</p>
      ) : (
        <div className="ext-table-wrap">
          <table className="ext-table">
            <thead>
              <tr>
                <th className="ext-expand-col"></th>
                <th>名称</th>
                <th>标识</th>
                <th>版本</th>
                <th>信任档位</th>
                <th>运行状态</th>
                <th>服务端接入</th>
                <th>界面入口</th>
              </tr>
            </thead>
            <tbody>
              {topology.extensions.map((extension) => {
                const isOpen = expanded.has(extension.id);
                const name = displayNameOf(extension);
                const runtimeDisabled = extension.trust === "deployment" && extension.runtimeEnabled === false;
                return (
                  <Fragment key={extension.id}>
                    <tr
                      className={[
                        isOpen ? "ext-row-expanded" : "",
                        runtimeDisabled ? "ext-row-runtime-disabled" : "",
                      ].filter(Boolean).join(" ") || undefined}
                    >
                      <td className="ext-expand-col">
                        <button
                          type="button"
                          className="ext-expand-btn"
                          aria-expanded={isOpen}
                          aria-label={`${isOpen ? "收起" : "展开"} ${name} 的接入明细`}
                          onClick={() => toggle(extension.id)}
                        >
                          {isOpen ? "▾" : "▸"}
                        </button>
                      </td>
                      <td className="ext-name">{name}</td>
                      <td className="ext-id">{extension.id}</td>
                      <td className="ext-id">{extension.version || "—"}</td>
                      <td>
                        <span
                          className={`ext-trust${extension.trust === "deployment" ? " ext-trust-deployment" : ""}`}
                        >
                          {label(TRUST_LABELS, extension.trust, "未知")}
                        </span>
                      </td>
                      <td>
                        {extension.trust === "deployment" ? (
                          <ExtensionRuntimeCell
                            extension={extension}
                            rowState={runtimeRowState[extension.id] ?? IDLE_ROW_STATE}
                            onToggle={handleRuntimeToggle}
                          />
                        ) : extension.trust === "builtin" ? (
                          <span className="ext-runtime-static">始终启用</span>
                        ) : (
                          <span className="ext-runtime-static">—</span>
                        )}
                      </td>
                      <td className="ext-count">{extension.contributions.length}</td>
                      <td className="ext-count">{extension.uiContributions.length}</td>
                    </tr>
                    {isOpen && (
                      <tr className="ext-subrow">
                        <td colSpan={8}>
                          <ExtensionDetail extension={extension} />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </main>
  );
}
