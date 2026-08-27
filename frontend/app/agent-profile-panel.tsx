// 「AI 对这个库的理解」面板(P1-T7)——后端四个端点的全栈对等界面。
//
// 形态选择与理由:
//
// · 入口由 build-time workspace UI registry 的首个真实 contribution 提供。插件只
//   委托 exact-owner `openUnderstanding` action；本文件仍是理解数据、busy 与轮询的
//   唯一 owner，入口本身在点击前不发领域请求。只读成员同样可见，因为后端四个端点
//   都走 notebook read，而“本人那一份”本来就允许读者维护。
//
// · **两档共用一个 `UnderstandingChain` 子组件**。两档的差别只有三处(标题、能不能
//   编辑、写哪个 scope),复制两份的唯一后果是下次改保存逻辑时改漏一边。
//
// · **清空是两步确认,不是 `window.confirm`。** 与群组面板同一条口径:一次误点不该
//   决定一段已经攒了很久的内容;而原生确认框在浮动窗里还会把焦点整个抢走。
//
// 长任务契约（见 `docs/development.md`）在这里的落点:「重新整理」点完立刻
// 不可点并换成「整理中…」;忙碌位存的是**哪个库在忙**(共享的 `notebook-busy-set`,
// 不是裸布尔);解除**按证据**——只有轮询读到服务端说这条链不在跑了才解除,另设
// 轮询尝试上限,超限走中性文案而不猜结局。
"use client";

import { useCallback, useEffect, useRef, useState, type SyntheticEvent } from "react";
import { ChevronDown } from "lucide-react";

import { toUserMessage } from "./errors.ts";
import {
  clearAgentObservations,
  clearUnderstandingBlock,
  fetchAgentObservations,
  fetchUnderstanding,
  rebuildUnderstanding,
  saveUnderstandingBlock,
} from "../features/agent-profile/profile-api.ts";
import {
  AGENT_OBSERVATION_SAMPLE_MAX,
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
  draftIsStale,
  evidenceSourceIds,
  groupObservationsByAgent,
  isUnderstandingChainBusy,
  observationRelativeTime,
  orderedUnderstandingBlocks,
  releaseNotebookClaim,
  understandingValueLength,
  understandingValueTooLong,
  zeroHitCount,
  type AgentObservation,
  type UnderstandingBlock,
  type UnderstandingDraft,
  type UnderstandingJobStatus,
  type UnderstandingResponse,
  type UnderstandingScope,
} from "../features/agent-profile/profile-model.ts";

/**
 * 「这段话凭什么这么说」——一块的依据行。
 *
 * codex #520 R2 P2-2:服务端一直在写这份记账(底座记来源 id、`usage_gaps` 记零命中
 * 次数),前端却把它当 opaque 透传、一个字都不渲染,而设计契约要的正是「结论可点开
 * 来源」。只对**AI 整理出来的**块显示:人自己写的那段话,依据是他自己,再挂一排
 * 来源只会让人以为那是系统给他的论据。
 *
 * `onOpenSource` 缺省时 chip 仍然渲染、只是不可点——面板要能脱离 page.tsx 单独
 * 测试,而「有几份资料支撑」这件事本身就有信息量,不该因为没接线就整行消失。
 */
function BlockEvidence({
  block,
  onOpenSource,
  resolveSourceTitle,
}: {
  block: UnderstandingBlock;
  onOpenSource?: (sourceId: string) => void;
  resolveSourceTitle?: (sourceId: string) => string;
}) {
  if (block.updated_origin !== "job") return null;
  const zeroHits = zeroHitCount(block);
  if (zeroHits !== null) {
    return (
      <p className="tool-hint" style={{ margin: "6px 0 0" }}>
        依据：{zeroHits} 次没找到结果的检索
      </p>
    );
  }
  const sourceIds = evidenceSourceIds(block);
  if (sourceIds.length === 0) return null;
  return (
    <div style={{ marginTop: 6 }}>
      <p className="tool-hint" style={{ margin: "0 0 4px" }}>依据来源</p>
      <div className="tag-row">
        {sourceIds.map((sourceId) => {
          // 标题查不到时退回 id:那不是内部黑话,是这份资料在这个库里的名字,
          // 而「隐藏一条查不到标题的依据」会让依据数与实际不符。
          const title = resolveSourceTitle?.(sourceId) || sourceId;
          if (!onOpenSource) {
            return <span className="tag" key={sourceId}>{title}</span>;
          }
          return (
            <button
              type="button"
              className="tag"
              key={sourceId}
              title="打开这份资料"
              onClick={() => onOpenSource(sourceId)}
            >
              {title}
            </button>
          );
        })}
      </div>
    </div>
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
  /** 按 label 存的草稿。fork 点(`baseRevision`)由面板层管理,这里只读。 */
  drafts: Readonly<Record<string, UnderstandingDraft>>;
  onDraft: (label: string, value: string, blockRevision: number) => void;
  /** 丢弃这个块的草稿,回到服务端当前值——陈旧草稿警示行的「放弃我的修改」入口。 */
  onDiscardDraft: (label: string) => void;
  savingLabels: ReadonlySet<string>;
  confirmingLabel: string;
  onSave: (scope: UnderstandingScope, block: UnderstandingBlock) => void;
  onAskClear: (label: string) => void;
  onCancelClear: () => void;
  onClear: (scope: UnderstandingScope, block: UnderstandingBlock) => void;
  onRebuild: (scope: UnderstandingScope) => void;
  onOpenSource?: (sourceId: string) => void;
  resolveSourceTitle?: (sourceId: string) => string;
};

function UnderstandingChain({
  title,
  description,
  blocks,
  scope,
  canEdit,
  job,
  busy,
  drafts,
  onDraft,
  onDiscardDraft,
  savingLabels,
  confirmingLabel,
  onSave,
  onAskClear,
  onCancelClear,
  onClear,
  onRebuild,
  onOpenSource,
  resolveSourceTitle,
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
            title={busy ? "整理进行中" : "让 AI 重新读一遍，整理出最新的一份"}
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
        const draft = drafts[block.label];
        const value = draft?.value ?? block.value;
        const dirty = value !== block.value;
        // 草稿 fork 出去之后服务端那一行又前进了一版(被别处整理,或用户自己另一次
        // 保存推进)——保存仍然可以做,但必须显式说清「这是一次知情覆盖」。
        const stale = draftIsStale(block, draft);
        const tooLong = understandingValueTooLong(value);
        const saving = savingLabels.has(block.label);
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
                  onChange={(event) => onDraft(block.label, event.target.value, block.revision)}
                />
                {stale ? (
                  <div
                    className="tool-hint"
                    role="alert"
                    style={{ margin: "4px 0 0", color: "var(--warning)" }}
                  >
                    <p style={{ margin: "0 0 4px" }}>
                      内容刚被重新整理过，下面是你未保存的修改
                    </p>
                    <button
                      type="button"
                      className="sort-button"
                      onClick={() => onDiscardDraft(block.label)}
                    >
                      放弃我的修改
                    </button>
                  </div>
                ) : null}
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
            {block.value ? (
              <BlockEvidence
                block={block}
                onOpenSource={onOpenSource}
                resolveSourceTitle={resolveSourceTitle}
              />
            ) : null}
          </div>
        );
      })}
    </section>
  );
}

/**
 * 「Agent 记录」——P3-T5 的第二套小节,与上面五块「理解」完全独立:只读/清空,
 * 没有编辑,内容也不是 AI 整理出的结论,而是外部 Agent 自己写下的原始短句。
 *
 * 折叠面板默认收起、且**首次展开才发第一次请求**:这份记录多数用户可能永远
 * 不点开,总闸开着就无条件跟着面板一起加载是白付一次查询。展开状态与已加载
 * 的数据都是本组件自己的 state——它不参与上面「理解」两条链的轮询/忙碌位,
 * 清空不是后台任务(同步请求、发出即完成),不需要 `notebook-busy-set` 那一套
 * 按笔记本单飞的语义。
 */
function AgentObservationSection({ notebookId }: { notebookId: string }) {
  const [items, setItems] = useState<AgentObservation[] | null>(null);
  const [remoteDisabled, setRemoteDisabled] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [clearingAgentId, setClearingAgentId] = useState("");
  const [clearingAll, setClearingAll] = useState(false);
  const [confirmingAll, setConfirmingAll] = useState(false);
  // codex #535 R5 P2:任一清空在飞时**全部**清空按钮禁用(见 anyClearing),
  // 且 load 带代次守卫——快速连清两个 Agent 时,先发请求的 load 若最后返回,
  // 会拿旧快照盖掉后一次的结果,让已删除的记录复活到下次刷新。
  const loadEpochRef = useRef(0);

  const load = useCallback(async () => {
    const epoch = ++loadEpochRef.current;
    setLoading(true);
    setError("");
    try {
      const next = await fetchAgentObservations(notebookId);
      if (epoch === loadEpochRef.current) {
        // codex #535 R9 P2:后端可能在浏览器仍持旧配置时已关掉该能力——响应
        // 的 enabled=false 必须保真,不能把「关闭」渲染成「暂无记录」。
        setRemoteDisabled(next.enabled === false);
        setItems(next.items);
      }
    } catch (err) {
      if (epoch === loadEpochRef.current) {
        setError(toUserMessage(err, "没能读到 Agent 记录，请稍后重试"));
      }
    } finally {
      if (epoch === loadEpochRef.current) setLoading(false);
    }
  }, [notebookId]);

  const anyClearing = clearingAgentId !== "" || clearingAll;

  function onToggle(event: SyntheticEvent<HTMLDetailsElement>) {
    if (event.currentTarget.open && items === null && !loading) {
      void load();
    }
  }

  async function clearAgent(agentProfileId: string) {
    setClearingAgentId(agentProfileId);
    setError("");
    try {
      await clearAgentObservations(notebookId, agentProfileId);
      await load();
    } catch (err) {
      setError(toUserMessage(err, "没能清空，请稍后重试"));
    } finally {
      setClearingAgentId("");
    }
  }

  async function clearAll() {
    setConfirmingAll(false);
    setClearingAll(true);
    setError("");
    try {
      await clearAgentObservations(notebookId);
      await load();
    } catch (err) {
      setError(toUserMessage(err, "没能清空，请稍后重试"));
    } finally {
      setClearingAll(false);
    }
  }

  const groups = items ? groupObservationsByAgent(items) : [];

  return (
    <details className="agent-observation-panel" onToggle={onToggle}>
      <summary><ChevronDown size={14} /> Agent 记录</summary>
      <div className="stack">
        <p className="tool-hint" style={{ margin: 0 }}>
          外部 Agent 通过接口写下的使用线索。它们只会用来更新你自己的「我的检索心得」，不会进入回答，也不会被引用。
        </p>
        {error ? (
          <p className="tool-hint" role="alert" style={{ color: "var(--danger)" }}>{error}</p>
        ) : null}
        {loading && items === null ? <p className="tool-hint">加载中…</p> : null}
        {items !== null && items.length === 0 ? (
          <p className="tool-hint">{remoteDisabled ? "该功能已在此部署关闭" : "暂无 Agent 记录"}</p>
        ) : null}
        {/* 服务端按最近 `AGENT_OBSERVATION_SAMPLE_MAX` 条取数(见该常量注释里的
            镜像关系)、不分页、也不回传取了多少——`items.length` 恰好等于这个
            上限时,唯一能说清「还有更早的记录没显示」的办法就是这一行提示,
            否则用户会把「取满了」误读成「一共就这么多」。 */}
        {items !== null && items.length === AGENT_OBSERVATION_SAMPLE_MAX ? (
          <p className="tool-hint">仅显示最近 {AGENT_OBSERVATION_SAMPLE_MAX} 条</p>
        ) : null}
        {items !== null && items.length > 0 ? (
          <>
            <div>
              {confirmingAll ? (
                <div className="tag-row">
                  <button
                    type="button"
                    className="sort-button"
                    disabled={anyClearing}
                    onClick={() => { void clearAll(); }}
                  >
                    {clearingAll ? "清空中…" : "确认清空全部记录"}
                  </button>
                  <button
                    type="button"
                    className="sort-button"
                    disabled={anyClearing}
                    onClick={() => setConfirmingAll(false)}
                  >
                    取消
                  </button>
                </div>
              ) : (
                <button
                  type="button"
                  className="sort-button"
                  disabled={anyClearing}
                  onClick={() => setConfirmingAll(true)}
                >
                  清空全部记录
                </button>
              )}
            </div>
            {groups.map((group) => (
              <div className="item" key={group.agentProfileId}>
                <div className="tag-row" style={{ justifyContent: "space-between" }}>
                  <strong>{group.agentName}</strong>
                  <button
                    type="button"
                    className="sort-button"
                    disabled={anyClearing}
                    onClick={() => { void clearAgent(group.agentProfileId); }}
                  >
                    {clearingAgentId === group.agentProfileId ? "清空中…" : "清空这个 Agent 的记录"}
                  </button>
                </div>
                <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
                  {group.items.map((item) => (
                    <li key={item.id}>
                      <span className="tool-hint">{observationRelativeTime(item.created_at)}　</span>
                      {item.text}
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </>
        ) : null}
      </div>
    </details>
  );
}

/**
 * 面板主体。外层(page.tsx)负责浮动窗与标题栏,这里只管内容——同 `SchemaManager`
 * 与 `KgAnalysisView` 的分工。
 */
export function AgentProfilePanel({
  notebookId,
  onOpenSource,
  resolveSourceTitle,
}: {
  notebookId: string;
  /**
   * 打开一份资料的详情。由 page.tsx 接到**引用卡走的同一条**打开路径上
   * (`onOpenSourceElement`),不另造弹窗;缺省时依据 chip 仍显示、只是不可点。
   */
  onOpenSource?: (sourceId: string) => void;
  /** source id → 用户看得懂的资料名。查不到就回空串,由调用处退回 id。 */
  resolveSourceTitle?: (sourceId: string) => string;
}) {
  const [data, setData] = useState<UnderstandingResponse | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [drafts, setDrafts] = useState<Record<string, UnderstandingDraft>>({});
  // 忙碌位是**集合**而不是单值:一次点两块的保存互不相干,单值会让先点的那格的
  // 「保存中…」被后点的那格挤掉(状态还在飞、界面却已经看不出来)。
  const [savingLabels, setSavingLabels] = useState<ReadonlySet<string>>(new Set());
  const [confirmingLabel, setConfirmingLabel] = useState("");
  // 忙碌位是**按笔记本的集合**而不是裸布尔:面板虽然一次只看一个库,但这套语义与
  // 「补上关联」「重新合并」逐字相同,共用同一份有单测的实现(见 profile-model.ts
  // 里那段再导出的注释),不另写一版只记单个库的形态。
  const [baseBusyIds, setBaseBusyIds] = useState<ReadonlySet<string>>(new Set());
  const [mineBusyIds, setMineBusyIds] = useState<ReadonlySet<string>>(new Set());
  // 轮询超限之后**停轮询**,而不是只发一条提示:服务端只在进程内记状态,任务真卡死
  // 时它会一直如实回报「在跑」,不设这道闸就会一直发下去。
  //
  // ⚠ 已登记的取舍:超限之后按钮**维持禁用**,不会自己再恢复——`baseBusy`/`mineBusy`
  // 仍然读 `data.job` 里那个「running」,而轮询已经停了,`data` 不会再更新。恢复
  // 只能靠重开这个面板(`pollExhausted` 是本地 state,组件重新挂载就复位;见 page.tsx
  // 对 `AgentProfilePanel` 的 `key={currentNotebookId}`,同一个库里关了再开同样有效
  // ——因为整个面板随浮动窗一起卸载重挂)。不做成「超限也自动解锁」是刻意的:那等于
  // 在任务可能真卡死时假装它跑完了,会放行一次重复的模型调用。
  const [pollExhausted, setPollExhausted] = useState(false);
  // codex R11 P2:每次新的重建认领都要一份**新的**轮询预算。attempts 活在
  // effect 闭包里,deps 不变就不重启——第二条链在第一条快耗尽预算时启动,会
  // 只分到剩下的一两拍。epoch 进 deps,新认领即重启 effect、计数归零。
  const [pollEpoch, setPollEpoch] = useState(0);

  const load = useCallback(async () => {
    const next = await fetchUnderstanding(notebookId);
    setData(next);
    return next;
  }, [notebookId]);

  const retryLoad = useCallback(() => {
    setError("");
    load().catch((err) => {
      setError(toUserMessage(err, "没能读到这个库的理解，请稍后重试"));
    });
  }, [load]);

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
  }, [polling, notebookId, pollEpoch]);

  const draftOf = useCallback(
    (label: string, fallback: string) => drafts[label]?.value ?? fallback,
    [drafts],
  );

  // 一份草稿只在**首次**创建时捕获它 fork 自哪一版(`baseRevision`);同一份草稿上
  // 继续打字不能重新捕获,否则「服务端已经往前走了」这件事会被每次按键悄悄抹掉。
  const onDraft = useCallback((label: string, value: string, blockRevision: number) => {
    setDrafts((prev) => ({
      ...prev,
      [label]: { value, baseRevision: prev[label]?.baseRevision ?? blockRevision },
    }));
  }, []);

  // 无条件丢弃——用于「放弃我的修改」与清空成功之后:这两处都是用户主动表达
  // 「不要这份草稿了」,不需要比对提交值。
  const dropDraft = useCallback((label: string) => {
    setDrafts((prev) => {
      if (!(label in prev)) return prev;
      const next = { ...prev };
      delete next[label];
      return next;
    });
  }, []);

  // 保存成功之后**只在草稿仍逐字等于本次提交值时**才丢:保存请求在飞的那段窗口
  // 用户可能续打了几个字,这时草稿已经不是「刚提交的那份」,删掉就是丢字。保留下来
  // 的草稿的 `baseRevision` 还是提交前那一版,`load()` 换回新 revision 之后会自然
  // 落进 `draftIsStale` 的陈旧提示——不是新分支,是同一条判据的自然结果。
  const dropDraftIfUnchanged = useCallback((label: string, submittedValue: string) => {
    setDrafts((prev) => {
      const draft = prev[label];
      if (!draft || draft.value !== submittedValue) return prev;
      const next = { ...prev };
      delete next[label];
      return next;
    });
  }, []);

  async function saveBlock(scope: UnderstandingScope, block: UnderstandingBlock) {
    const value = draftOf(block.label, block.value);
    if (understandingValueTooLong(value)) return;
    setSavingLabels((prev) => (prev.has(block.label) ? prev : new Set(prev).add(block.label)));
    setError("");
    setNotice("");
    try {
      await saveUnderstandingBlock(notebookId, block.label, {
        scope,
        value,
        expected_revision: block.revision,
      });
      dropDraftIfUnchanged(block.label, value);
      await load();
    } catch (err) {
      setError(toUserMessage(err, "没能保存，请稍后重试"));
      // 409(这段刚被别处更新过)之后必须重取一次:不重取的话用户手里还是旧
      // revision,再点保存只会撞同一堵墙。草稿**刻意保留**——重取只换来最新的
      // revision 与对照值,用户写了一半的字不该被一次冲突吃掉;revision 前进之后
      // `draftIsStale` 会自然触发警示行,不需要在这里另外处理。
      await load().catch(() => undefined);
    } finally {
      setSavingLabels((prev) => {
        if (!prev.has(block.label)) return prev;
        const next = new Set(prev);
        next.delete(block.label);
        return next;
      });
    }
  }

  async function clearBlock(scope: UnderstandingScope, block: UnderstandingBlock) {
    setSavingLabels((prev) => (prev.has(block.label) ? prev : new Set(prev).add(block.label)));
    setConfirmingLabel("");
    setError("");
    setNotice("");
    try {
      // 带上界面看到过的版本号:加载后内容又被整理/他人改过时走 409 冲突路径,
      // 而不是把没看过的内容清掉(codex R1 P2)。
      await clearUnderstandingBlock(notebookId, block.label, scope, block.revision);
      dropDraft(block.label);
      await load();
    } catch (err) {
      setError(toUserMessage(err, "没能清空，请稍后重试"));
      await load().catch(() => undefined);
    } finally {
      setSavingLabels((prev) => {
        if (!prev.has(block.label)) return prev;
        const next = new Set(prev);
        next.delete(block.label);
        return next;
      });
    }
  }

  async function startRebuild(scope: UnderstandingScope) {
    const claim = scope === "shared" ? setBaseBusyIds : setMineBusyIds;
    // 忙碌位在 await **之前**置上:请求在飞的那段窗口按钮也不能连点。
    claim((prev) => claimNotebookSlot(prev, notebookId));
    setPollExhausted(false);
    setPollEpoch((epoch) => epoch + 1);   // 新认领 = 新轮询预算(codex R11 P2)
    setError("");
    setNotice("");
    try {
      await rebuildUnderstanding(notebookId, scope);
    } catch (err) {
      const release = scope === "shared" ? setBaseBusyIds : setMineBusyIds;
      release((prev) => releaseNotebookClaim(prev, notebookId));
      setError(toUserMessage(err, "现在没能开始整理，请稍后重试"));
      // codex R10 P2:409 的常见来历是「别的端/自动触发抢先认领」——本地手上
      // 的 data.job 还是终态旧照,不重取就不会进入轮询,按钮立刻恢复可点、
      // 再点还是 409。重取一次让服务端的 running 接管忙碌位与轮询。
      load().catch(() => undefined);
      return;
    }
    // 排上了 ≠ 做完了。终态由上面那条轮询按服务端状态判,这里不猜。
    load().catch(() => undefined);
  }

  if (data === null) {
    if (error) {
      return (
        <div className="stack">
          <p className="tool-hint" role="alert" style={{ color: "var(--danger)" }}>{error}</p>
          <div>
            <button type="button" className="sort-button" onClick={retryLoad}>重试</button>
          </div>
        </div>
      );
    }
    return <p className="tool-hint">加载中…</p>;
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
        drafts={drafts}
        onDraft={onDraft}
        onDiscardDraft={dropDraft}
        savingLabels={savingLabels}
        confirmingLabel={confirmingLabel}
        onSave={(scope, block) => { void saveBlock(scope, block); }}
        onAskClear={setConfirmingLabel}
        onCancelClear={() => setConfirmingLabel("")}
        onClear={(scope, block) => { void clearBlock(scope, block); }}
        onRebuild={(scope) => { void startRebuild(scope); }}
        onOpenSource={onOpenSource}
        resolveSourceTitle={resolveSourceTitle}
      />
      <UnderstandingChain
        title="我的检索心得"
        description="只有你能看到，也只在你自己提问时生效；别的成员既看不到，也不会被它影响。"
        blocks={orderedUnderstandingBlocks(data.mine, OVERLAY_LABELS)}
        scope="mine"
        canEdit
        job={data.job?.mine ?? null}
        busy={mineBusy}
        drafts={drafts}
        onDraft={onDraft}
        onDiscardDraft={dropDraft}
        savingLabels={savingLabels}
        confirmingLabel={confirmingLabel}
        onSave={(scope, block) => { void saveBlock(scope, block); }}
        onAskClear={setConfirmingLabel}
        onCancelClear={() => setConfirmingLabel("")}
        onClear={(scope, block) => { void clearBlock(scope, block); }}
        onRebuild={(scope) => { void startRebuild(scope); }}
        onOpenSource={onOpenSource}
        resolveSourceTitle={resolveSourceTitle}
      />
      <AgentObservationSection notebookId={notebookId} />
    </div>
  );
}
