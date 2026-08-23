"use client";

// 「已加载的扩展」——管理员只读页,`GET /admin/extensions` 的前端对等面。
//
// 壳层逐条镜像 admin/usage/page.tsx:同一个 `fetchMe` 登录门(先判角色、再取数据)、
// 同一套四态(加载中 / 无权限 / 加载失败 / 就绪)、同一个 PageHeader、失败一律经
// `toUserMessage` 出文案。差别只有两处,都是刻意的:
//   · 这里没有 usage 那个 `FORBIDDEN_SENTINEL`。403 的识别改用 `httpErrorStatus`
//     ——它读的是 errors.ts 挂在人话错误上的状态码 Symbol,不必读 `.message`,因此
//     不用往 errors-guard 的精确计数清单里加一笔。同理失败态那个字段叫 `notice`
//     而不是 `message`:它装的是**已经过人话层**的文案,不是某个 Error 的
//     `.message`,借用那个名字只会在普查里挂一条名不副实的豁免。
//   · 页面是纯只读的,没有任何写动作,所以没有忙碌态/二次确认那一套。
import { Fragment, useEffect, useState } from "react";

import { fetchMe } from "../../auth.ts";
import { PageHeader } from "../../components/PageHeader.tsx";
import { httpErrorStatus, toUserMessage } from "../../errors.ts";
import { label } from "../../vocabulary.ts";
import {
  fetchLoadedExtensions,
  KNOWN_API_VERSION,
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

  return (
    <main className="ext-page">
      <PageHeader title="已加载的扩展" />
      <p className="ext-description">
        扩展在服务启动时一次性装入并固定，之后不再变化；未启用的扩展不会出现在这里。改动部署配置后需重启服务才会生效。
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
                <th>服务端接入</th>
                <th>界面入口</th>
              </tr>
            </thead>
            <tbody>
              {topology.extensions.map((extension) => {
                const isOpen = expanded.has(extension.id);
                const name = displayNameOf(extension);
                return (
                  <Fragment key={extension.id}>
                    <tr className={isOpen ? "ext-row-expanded" : undefined}>
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
                      <td className="ext-count">{extension.contributions.length}</td>
                      <td className="ext-count">{extension.uiContributions.length}</td>
                    </tr>
                    {isOpen && (
                      <tr className="ext-subrow">
                        <td colSpan={7}>
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
