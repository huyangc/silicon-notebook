// 「AI 对这个库的理解」面板(P1-T7)——后端四个端点的全栈对等界面。
//
// 形态选择与理由:
//
// · **入口是知识图谱视图头部的第三颗按钮,不是笔记本设置里的一项。** 设置页整体是
//   一个 `<form onSubmit>`,往里塞五个独立保存的文本框会让回车键落到别人的提交上;
//   而图谱视图的头部本来就是无条件的顶层导航(打开它不受「有没有图谱」门控),只读
//   成员同样进得去——这一档里「本人那一份」恰恰是只读成员唯一能写的东西。
//
// · **入口按钮单独导出成组件**(`UnderstandingEntryButton`),而不是在 page.tsx 里
//   内联一个 `{flag && <button …>}`。总闸关掉时「入口不渲染」是这条特性的可见契约
//   之一,而 page.tsx 是九千行的客户端组件、在测试里渲染不动;把这颗按钮抽出来,
//   那条契约就能被真正渲染一遍来证明,而不是只靠读源码的守卫。
//
// · **两档共用一个 `UnderstandingChain` 子组件**。两档的差别只有三处(标题、能不能
//   编辑、写哪个 scope),复制两份的唯一后果是下次改保存逻辑时改漏一边。
//
// · **清空是两步确认,不是 `window.confirm`。** 与群组面板同一条口径:一次误点不该
//   决定一段已经攒了很久的内容;而原生确认框在浮动窗里还会把焦点整个抢走。
//
// 长任务红线(CLAUDE.md「长任务按钮的忙碌态」)在这里的落点:「重新整理」点完立刻
// 不可点并换成「整理中…」;忙碌位存的是**哪个库在忙**(共享的 `notebook-busy-set`,
// 不是裸布尔);解除**按证据**——只有轮询读到服务端说这条链不在跑了才解除,另设
// 轮询尝试上限,超限走中性文案而不猜结局。
"use client";

import { useCallback, useEffect, useState } from "react";
import { Sparkles } from "lucide-react";

import { toUserMessage } from "./errors.ts";
import {
  clearUnderstandingBlock,
  fetchUnderstanding,
  rebuildUnderstanding,
  saveUnderstandingBlock,
} from "../features/agent-profile/profile-api.ts";
import {
  AGENT_PROFILE_VALUE_MAX_CHARS,
  BASE_LABELS,
  OVERLAY_LABELS,
  PROFILE_BLOCK_HINTS,
  PROFILE_BLOCK_TITLES,
  UNDERSTANDING_POLL_GAVE_UP_MESSAGE,
  UNDERSTANDING_POLL_MAX_ATTEMPTS,
  UNDERSTANDING_POLL_MS,
  busyForNotebook,
  claimNotebookSlot,
  isUnderstandingChainBusy,
  orderedUnderstandingBlocks,
  releaseNotebookClaim,
  understandingValueLength,
  understandingValueTooLong,
  type UnderstandingBlock,
  type UnderstandingJobStatus,
  type UnderstandingResponse,
  type UnderstandingScope,
} from "../features/agent-profile/profile-model.ts";

/**
 * 知识图谱视图头部的入口按钮。
 *
 * `enabled` 关掉时**一个节点都不渲染**:后端此时四个端点写路径全部 409、GET 回
 * `enabled=false`,给一颗必然打不开的按钮只会让人以为坏了。判据来自系统配置的
 * `agent_profile_enabled`,`system-api.ts` 对缺失字段按 false 解析(旧后端没有这
 * 项能力)。
 */
export function UnderstandingEntryButton({
  enabled,
  onOpen,
}: {
  enabled: boolean;
  onOpen: () => void;
}) {
  if (!enabled) return null;
  return (
    <button
      type="button"
      className="sort-button kg-schema-button"
      onClick={onOpen}
      title="查看并修改 AI 对这个库形成的理解与你的检索心得"
    >
      <Sparkles size={16} /> AI 对这个库的理解
    </button>
  );
}

type ChainProps = {
  title: string;
  description: string;
  blocks: UnderstandingBlock[];
  scope: UnderstandingScope;
  /** 共享那一档对只读成员是 `false`:内容照常显示,写入控件不渲染。 */
  canEdit: boolean;
  job: UnderstandingJobStatus | null;
  busy: boolean;
  draftOf: (label: string, fallback: string) => string;
  onDraft: (label: string, value: string) => void;
  savingLabel: string;
  confirmingLabel: string;
  onSave: (scope: UnderstandingScope, block: UnderstandingBlock) => void;
  onAskClear: (label: string) => void;
  onCancelClear: () => void;
  onClear: (scope: UnderstandingScope, block: UnderstandingBlock) => void;
  onRebuild: (scope: UnderstandingScope) => void;
};

function UnderstandingChain({
  title,
  description,
  blocks,
  scope,
  canEdit,
  job,
  busy,
  draftOf,
  onDraft,
  savingLabel,
  confirmingLabel,
  onSave,
  onAskClear,
  onCancelClear,
  onClear,
  onRebuild,
}: ChainProps) {
  return (
    <section className="stack">
      <div>
        <h3 style={{ margin: 0 }}>{title}</h3>
        <p className="tool-hint" style={{ margin: "4px 0 0" }}>{description}</p>
      </div>
      {canEdit && (
        <div>
          <button
            type="button"
            className="sort-button"
            disabled={busy}
            title="让 AI 重新读一遍，整理出最新的一份"
            onClick={() => onRebuild(scope)}
          >
            {busy ? "整理中…" : "重新整理"}
          </button>
        </div>
      )}
      {/* 失败原因是后端刻意写给用户看的那一份(内部诊断从不下发),不显示等于让
          用户对着一份永远不更新的内容猜。 */}
      {job?.failure_reason ? (
        <p className="tool-hint" role="status">上次整理没成功：{job.failure_reason}</p>
      ) : null}
      {blocks.map((block) => {
        const blockTitle = PROFILE_BLOCK_TITLES[block.label];
        // 查不到中文标题就整块不渲染:直出内部枚举名不是「降级」,是把黑话上屏。
        if (!blockTitle) return null;
        const value = draftOf(block.label, block.value);
        const dirty = value !== block.value;
        const tooLong = understandingValueTooLong(value);
        const saving = savingLabel === block.label;
        const confirming = confirmingLabel === block.label;
        return (
          <div className="item" key={block.label}>
            <strong>{blockTitle}</strong>
            <p className="tool-hint" style={{ margin: "4px 0 8px" }}>
              {PROFILE_BLOCK_HINTS[block.label] ?? ""}
            </p>
            {canEdit ? (
              <>
                <textarea
                  className="textarea"
                  aria-label={blockTitle}
                  value={value}
                  onChange={(event) => onDraft(block.label, event.target.value)}
                />
                <p className="tool-hint" style={{ margin: "4px 0 0" }}>
                  {understandingValueLength(value)} / {AGENT_PROFILE_VALUE_MAX_CHARS} 字
                  {tooLong ? "　内容过长，请先删减后再保存" : ""}
                </p>
                <div className="tag-row" style={{ marginTop: 8 }}>
                  <button
                    type="button"
                    className="sort-button"
                    disabled={!dirty || tooLong || saving}
                    onClick={() => onSave(scope, block)}
                  >
                    {saving ? "保存中…" : "保存"}
                  </button>
                  {confirming ? (
                    <>
                      <button
                        type="button"
                        className="sort-button"
                        disabled={saving}
                        onClick={() => onClear(scope, block)}
                      >
                        确认清空
                      </button>
                      <button type="button" className="sort-button" onClick={onCancelClear}>
                        取消
                      </button>
                    </>
                  ) : (
                    <button
                      type="button"
                      className="sort-button"
                      disabled={saving || (block.value === "" && value === "")}
                      onClick={() => onAskClear(block.label)}
                    >
                      清空
                    </button>
                  )}
                </div>
              </>
            ) : (
              <p style={{ margin: 0, whiteSpace: "pre-wrap" }}>
                {block.value || "还没有整理出内容。"}
              </p>
            )}
          </div>
        );
      })}
    </section>
  );
}

/**
 * 面板主体。外层(page.tsx)负责浮动窗与标题栏,这里只管内容——同 `SchemaManager`
 * 与 `KgAnalysisView` 的分工。
 */
export function AgentProfilePanel({ notebookId }: { notebookId: string }) {
  const [data, setData] = useState<UnderstandingResponse | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [savingLabel, setSavingLabel] = useState("");
  const [confirmingLabel, setConfirmingLabel] = useState("");
  // 忙碌位是**按笔记本的集合**而不是裸布尔:面板虽然一次只看一个库,但这套语义与
  // 「补上关联」「重新合并」逐字相同,共用同一份有单测的实现(见 profile-model.ts
  // 里那段再导出的注释),不另写一版只记单个库的形态。
  const [baseBusyIds, setBaseBusyIds] = useState<ReadonlySet<string>>(new Set());
  const [mineBusyIds, setMineBusyIds] = useState<ReadonlySet<string>>(new Set());
  // 轮询超限之后**停轮询**,而不是只发一条提示:服务端只在进程内记状态,任务真卡死
  // 时它会一直如实回报「在跑」,不设这道闸就会一直发下去。下一次点「重新整理」复位。
  const [pollExhausted, setPollExhausted] = useState(false);

  const load = useCallback(async () => {
    const next = await fetchUnderstanding(notebookId);
    setData(next);
    return next;
  }, [notebookId]);

  useEffect(() => {
    let cancelled = false;
    load().catch((err) => {
      if (!cancelled) setError(toUserMessage(err, "没能读到这个库的理解，请稍后重试"));
    });
    return () => { cancelled = true; };
  }, [load]);

  // 忙碌判据把两条证据取或:本地刚认领的那一格(点完立刻生效,不等第一次轮询),
  // 与服务端如实报出的 `running`(别人触发的整理、以及重开面板时仍在跑的那次)。
  const baseBusy = busyForNotebook(baseBusyIds, notebookId)
    || isUnderstandingChainBusy(data?.job?.base ?? null);
  const mineBusy = busyForNotebook(mineBusyIds, notebookId)
    || isUnderstandingChainBusy(data?.job?.mine ?? null);
  const polling = (baseBusy || mineBusy) && !pollExhausted;

  useEffect(() => {
    if (!polling) return;
    let cancelled = false;
    let attempts = 0;
    const timer = window.setInterval(() => {
      attempts += 1;
      if (attempts > UNDERSTANDING_POLL_MAX_ATTEMPTS) {
        if (cancelled) return;
        setPollExhausted(true);
        setNotice(UNDERSTANDING_POLL_GAVE_UP_MESSAGE);
        setBaseBusyIds((prev) => releaseNotebookClaim(prev, notebookId));
        setMineBusyIds((prev) => releaseNotebookClaim(prev, notebookId));
        return;
      }
      fetchUnderstanding(notebookId)
        .then((next) => {
          if (cancelled) return;
          setData(next);
          // 解除只认服务端证据:这条链不再是 running 才放开按钮。
          if (!isUnderstandingChainBusy(next.job?.base ?? null)) {
            setBaseBusyIds((prev) => releaseNotebookClaim(prev, notebookId));
          }
          if (!isUnderstandingChainBusy(next.job?.mine ?? null)) {
            setMineBusyIds((prev) => releaseNotebookClaim(prev, notebookId));
          }
        })
        // 一次瞬时失败不解除忙碌位——那不是「跑完了」的证据。尝试上限兜底。
        .catch(() => undefined);
    }, UNDERSTANDING_POLL_MS);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [polling, notebookId]);

  const draftOf = useCallback(
    (label: string, fallback: string) => drafts[label] ?? fallback,
    [drafts],
  );

  const onDraft = useCallback((label: string, value: string) => {
    setDrafts((prev) => ({ ...prev, [label]: value }));
  }, []);

  const dropDraft = useCallback((label: string) => {
    setDrafts((prev) => {
      if (!(label in prev)) return prev;
      const next = { ...prev };
      delete next[label];
      return next;
    });
  }, []);

  async function saveBlock(scope: UnderstandingScope, block: UnderstandingBlock) {
    const value = draftOf(block.label, block.value);
    if (understandingValueTooLong(value)) return;
    setSavingLabel(block.label);
    setError("");
    setNotice("");
    try {
      await saveUnderstandingBlock(notebookId, block.label, {
        scope,
        value,
        expected_revision: block.revision,
      });
      dropDraft(block.label);
      await load();
    } catch (err) {
      setError(toUserMessage(err, "没能保存，请稍后重试"));
      // 409(这段刚被别处更新过)之后必须重取一次:不重取的话用户手里还是旧
      // revision,再点保存只会撞同一堵墙。草稿**刻意保留**——重取只换来最新的
      // revision 与对照值,用户写了一半的字不该被一次冲突吃掉。
      await load().catch(() => undefined);
    } finally {
      setSavingLabel("");
    }
  }

  async function clearBlock(scope: UnderstandingScope, block: UnderstandingBlock) {
    setSavingLabel(block.label);
    setConfirmingLabel("");
    setError("");
    setNotice("");
    try {
      await clearUnderstandingBlock(notebookId, block.label, scope);
      dropDraft(block.label);
      await load();
    } catch (err) {
      setError(toUserMessage(err, "没能清空，请稍后重试"));
      await load().catch(() => undefined);
    } finally {
      setSavingLabel("");
    }
  }

  async function startRebuild(scope: UnderstandingScope) {
    const claim = scope === "shared" ? setBaseBusyIds : setMineBusyIds;
    // 忙碌位在 await **之前**置上:请求在飞的那段窗口按钮也不能连点。
    claim((prev) => claimNotebookSlot(prev, notebookId));
    setPollExhausted(false);
    setError("");
    setNotice("");
    try {
      await rebuildUnderstanding(notebookId, scope);
    } catch (err) {
      const release = scope === "shared" ? setBaseBusyIds : setMineBusyIds;
      release((prev) => releaseNotebookClaim(prev, notebookId));
      setError(toUserMessage(err, "现在没能开始整理，请稍后重试"));
      return;
    }
    // 排上了 ≠ 做完了。终态由上面那条轮询按服务端状态判,这里不猜。
    load().catch(() => undefined);
  }

  if (data === null) {
    return <p className="tool-hint">{error || "加载中…"}</p>;
  }
  if (!data.enabled) {
    return <p className="tool-hint">这项功能当前未开启。</p>;
  }

  return (
    <div className="stack">
      {error ? (
        <p className="tool-hint" role="alert" style={{ color: "var(--danger)" }}>{error}</p>
      ) : null}
      {notice ? <p className="tool-hint" role="status">{notice}</p> : null}
      <UnderstandingChain
        title="AI 对这个库的理解"
        description="AI 读过这个库里的资料之后形成的印象，笔记本里的每个人看到的都是同一份，提问时会带上它。"
        blocks={orderedUnderstandingBlocks(data.base, BASE_LABELS)}
        scope="shared"
        canEdit={data.can_edit_base}
        job={data.job?.base ?? null}
        busy={baseBusy}
        draftOf={draftOf}
        onDraft={onDraft}
        savingLabel={savingLabel}
        confirmingLabel={confirmingLabel}
        onSave={(scope, block) => { void saveBlock(scope, block); }}
        onAskClear={setConfirmingLabel}
        onCancelClear={() => setConfirmingLabel("")}
        onClear={(scope, block) => { void clearBlock(scope, block); }}
        onRebuild={(scope) => { void startRebuild(scope); }}
      />
      <UnderstandingChain
        title="我的检索心得"
        description="只有你能看到，也只在你自己提问时生效；别的成员既看不到，也不会被它影响。"
        blocks={orderedUnderstandingBlocks(data.mine, OVERLAY_LABELS)}
        scope="mine"
        canEdit
        job={data.job?.mine ?? null}
        busy={mineBusy}
        draftOf={draftOf}
        onDraft={onDraft}
        savingLabel={savingLabel}
        confirmingLabel={confirmingLabel}
        onSave={(scope, block) => { void saveBlock(scope, block); }}
        onAskClear={setConfirmingLabel}
        onCancelClear={() => setConfirmingLabel("")}
        onClear={(scope, block) => { void clearBlock(scope, block); }}
        onRebuild={(scope) => { void startRebuild(scope); }}
      />
    </div>
  );
}
