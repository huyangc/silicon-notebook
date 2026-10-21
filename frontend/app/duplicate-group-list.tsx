import { Fragment, useEffect, useState, type ReactNode } from "react";

import { LatexText } from "./answer-panel";
import { Pagination } from "./Pagination";
import { useClientPagination, type PaginationResetKey } from "./use-client-pagination.ts";
import { label, KNOWLEDGE_STATUS } from "./vocabulary";
import type { DuplicateGroup, DuplicatePartition, KnowledgeRef, MergeOutcome } from "./workspace-model";

/** 查重结果整份返回,界面每页显示的重复组数。 */
const DUPLICATE_GROUP_PAGE_SIZE = 20;

/** 合并结果在旁边停留多久:够读完一句话,之后自己收起,下一次合并的结果不会与它混在一起。 */
export const MERGE_RESULT_HOLD_MS = 4000;

/** 本人记忆分区的标题与说明——说的是此刻这一段里有什么、谁看得到。 */
export const OWN_DUPLICATES_TITLE = "我的记忆里的重复";
export const OWN_DUPLICATES_HINT = "这些条目来自你的个人记忆，只有你能看到";
/** 合并时弃用、还能恢复的本人条目。 */
export const MERGED_AWAY_TITLE = "合并时弃用的条目";
export const MERGED_AWAY_HINT = "恢复后，这一条会重新出现在你的检索结果里";

/**
 * 一次写入的结果,连同它发生在哪里:`key` 是那一组的第一条(合并)或那一行(恢复)的 id,
 * `index` 是点击时它在当前页的序号——它因这次写入而从列表里消失时,结果就放回这个位置。
 */
export type WriteFeedback = { key: string; index: number; outcome: MergeOutcome };

function WriteResult({ outcome }: { outcome: MergeOutcome }) {
  if (outcome.tone === "done") {
    return <p className="dup-merge-result dup-merge-result-done" role="status">{outcome.text}</p>;
  }
  if (outcome.tone === "stale") {
    return <p className="dup-merge-result dup-merge-result-stale" role="status">{outcome.text}</p>;
  }
  return <p className="dup-merge-result dup-merge-result-failed" role="status">{outcome.text}</p>;
}

/**
 * 把一组条目画成列表,并把 `feedback` 放在发生写入的地方:那一项还在当前页上,结果就画在
 * 那一项里(`inside`);它已经不在了(合并后只剩一条、恢复后离开这一段,或重排到别的页),
 * 结果就画在它原来的序号位置,而不是这一段的顶部或页面顶部。
 */
function withFeedback<T>(
  items: T[],
  keyOf: (item: T) => string,
  feedback: WriteFeedback | null,
  render: (item: T, index: number, inside: ReactNode) => ReactNode,
): ReactNode {
  const anchoredAt = feedback === null ? -1 : items.findIndex((item) => keyOf(item) === feedback.key);
  const placeholderAt = feedback !== null && anchoredAt < 0
    ? Math.min(Math.max(feedback.index, 0), items.length)
    : -1;
  const result = feedback ? <WriteResult outcome={feedback.outcome} /> : null;
  return (
    <>
      {items.map((item, index) => (
        <Fragment key={keyOf(item)}>
          {index === placeholderAt && result}
          {render(item, index, index === anchoredAt ? result : null)}
        </Fragment>
      ))}
      {placeholderAt === items.length && result}
    </>
  );
}

/**
 * 知识浏览器里的「查重」结果:一批重复组,每组内可以把非第一条合并进第一条。
 * `resetKey` 传当前 kind——切换知识类型 tab 时,前一个 tab 翻到的页码不该带过来。
 * `onMerge` 的第三个参数是这一组在当前页的序号(结果要放回的位置)。
 */
export function DuplicateGroupList({
  duplicates,
  readOnly,
  mergingId,
  onMerge,
  resetKey,
  feedback = null,
  pagerLabel = "重复组分页",
}: {
  duplicates: DuplicateGroup[];
  readOnly?: boolean;
  mergingId: string | null;
  onMerge: (sourceId: string, intoId: string, index: number) => void;
  resetKey?: PaginationResetKey;
  feedback?: WriteFeedback | null;
  pagerLabel?: string;
}) {
  const page = useClientPagination(duplicates, DUPLICATE_GROUP_PAGE_SIZE, resetKey);
  return (
    <>
      {withFeedback(page.pageItems, (group) => group.members[0]?.id ?? "", feedback, (group, index, inside) => (
        <article className="item">
          <div className="tag-row"><span className="tag">similarity {group.similarity}</span></div>
          {group.members.map((member, memberIndex) => (
            <div className="dup-member" key={member.id}>
              <span><LatexText text={member.headline} isFormula={(member.object_type || group.object_type) === "formula"} /> <span className="tag">{label(KNOWLEDGE_STATUS, member.status, "其他")}</span></span>
              {!readOnly && memberIndex > 0 && (
                <button
                  className="sort-button"
                  disabled={mergingId !== null}
                  onClick={() => onMerge(member.id, group.members[0].id, index)}
                >
                  {mergingId === member.id ? "合并中…" : "合并到第 1 条"}
                </button>
              )}
            </div>
          ))}
          {inside}
        </article>
      ))}
      <Pagination page={page.page} pageSize={DUPLICATE_GROUP_PAGE_SIZE} total={page.total} onPage={page.setPage} label={pagerLabel} />
    </>
  );
}

/** 合并时弃用、还带着合并记录的本人条目,每行一个「恢复」。 */
function MergedAwayList({
  items,
  busyId,
  onRestore,
  feedback,
}: {
  items: KnowledgeRef[];
  busyId: string | null;
  onRestore: (id: string, index: number) => void;
  feedback: WriteFeedback | null;
}) {
  return (
    <div className="dup-merged-away">
      <p className="section-title">{MERGED_AWAY_TITLE}</p>
      <p className="tool-hint">{MERGED_AWAY_HINT}</p>
      {withFeedback(items, (item) => item.id, feedback, (item, index, inside) => (
        <div className="dup-merged-away-row">
          <div className="dup-member">
            <span><LatexText text={item.headline} isFormula={item.object_type === "formula"} /> <span className="tag">{label(KNOWLEDGE_STATUS, item.status, "其他")}</span></span>
            <button
              className="sort-button"
              disabled={busyId !== null}
              onClick={() => onRestore(item.id, index)}
            >
              {busyId === item.id ? "恢复中…" : "恢复"}
            </button>
          </div>
          {inside}
        </div>
      ))}
    </div>
  );
}

type Slot = DuplicatePartition | "restore";

/**
 * 「查重」的两段结果:共享重复组(有知识治理权的人才拉、才显示),以及它下方单独成段的
 * 「我的记忆里的重复」(只由本人记忆支撑、只有本人看得到;没有就不显示这一段),这一段
 * 末尾列出合并时弃用、可以恢复的本人条目。每一段各自分页、各自显示自己的写入结果;
 * 同一时刻只有一次写入在途,两段的按钮一起禁用。只读成员只有本人那一段:她看不到共享组,
 * 也不请求共享查重。某一段没能读取时,在它的位置如实说明。
 */
export function DuplicateResults({
  shared,
  own,
  ownMergedAway = null,
  failures = {},
  canMergeShared,
  canMergeOwn,
  onMerge,
  onRestore,
  resetKey,
}: {
  /** `null`:没拉共享组(没有治理权,或还没点「查重」)。 */
  shared: DuplicateGroup[] | null;
  own: DuplicateGroup[] | null;
  ownMergedAway?: KnowledgeRef[] | null;
  failures?: Partial<Record<DuplicatePartition, string>>;
  canMergeShared: boolean;
  canMergeOwn: boolean;
  onMerge: (
    sourceId: string, intoId: string, partition: DuplicatePartition,
  ) => Promise<MergeOutcome | null> | void;
  onRestore?: (id: string) => Promise<MergeOutcome | null> | void;
  resetKey?: PaginationResetKey;
}) {
  // 正在写入的条目 id。写入会连着重拉知识列表、类型统计并重跑这一段的查重,不锁住的话
  // 在几处连点会并发排出若干次重扫。
  const [mergingId, setMergingId] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<{ slot: Slot; value: WriteFeedback } | null>(null);

  useEffect(() => {
    if (feedback === null) return;
    const timer = window.setTimeout(() => setFeedback(null), MERGE_RESULT_HOLD_MS);
    return () => window.clearTimeout(timer);
  }, [feedback]);

  // 换了知识类型:上一类的结果不该挂到这一类的组上。
  useEffect(() => {
    setFeedback(null);
  }, [resetKey]);

  const runWrite = async (
    slot: Slot, busy: string, key: string, index: number,
    write: () => Promise<MergeOutcome | null> | void,
  ) => {
    if (mergingId) return;
    setMergingId(busy);
    setFeedback(null);
    try {
      const outcome = await write();
      if (outcome) setFeedback({ slot, value: { key, index, outcome } });
    } finally {
      setMergingId(null);
    }
  };

  const feedbackFor = (slot: Slot) => (feedback?.slot === slot ? feedback.value : null);
  const ownGroups = own ?? [];
  const mergedAway = ownMergedAway ?? [];
  const showOwn = ownGroups.length > 0 || mergedAway.length > 0
    || feedbackFor("own") !== null || feedbackFor("restore") !== null
    || failures.own !== undefined;
  const sharedShown = shared !== null || failures.shared !== undefined;

  return (
    <div className="knowledge-panel">
      {sharedShown && (
        <>
          <p className="section-title">重复组（规范名归一化后相同）</p>
          {failures.shared !== undefined ? (
            <p className="dup-merge-result dup-merge-result-failed" role="alert">
              共享重复组没能读取：{failures.shared}
            </p>
          ) : (shared ?? []).length === 0 && feedbackFor("shared") === null ? (
            <p className="tool-hint">未发现重复。</p>
          ) : (
            <DuplicateGroupList
              duplicates={shared ?? []}
              readOnly={!canMergeShared}
              mergingId={mergingId}
              onMerge={(sourceId, intoId, index) => {
                void runWrite("shared", sourceId, intoId, index, () => onMerge(sourceId, intoId, "shared"));
              }}
              resetKey={resetKey}
              feedback={feedbackFor("shared")}
            />
          )}
        </>
      )}
      {!sharedShown && !showOwn && <p className="tool-hint">未发现重复。</p>}
      {showOwn && (
        <section className="dup-own-section" aria-label={OWN_DUPLICATES_TITLE}>
          <p className="section-title">{OWN_DUPLICATES_TITLE}</p>
          <p className="tool-hint">{OWN_DUPLICATES_HINT}</p>
          {failures.own !== undefined && (
            <p className="dup-merge-result dup-merge-result-failed" role="alert">
              我的记忆里的重复没能读取：{failures.own}
            </p>
          )}
          <DuplicateGroupList
            duplicates={ownGroups}
            readOnly={!canMergeOwn}
            mergingId={mergingId}
            onMerge={(sourceId, intoId, index) => {
              void runWrite("own", sourceId, intoId, index, () => onMerge(sourceId, intoId, "own"));
            }}
            resetKey={resetKey}
            feedback={feedbackFor("own")}
            pagerLabel="我的记忆重复组分页"
          />
          {canMergeOwn && onRestore && (mergedAway.length > 0 || feedbackFor("restore") !== null) && (
            <MergedAwayList
              items={mergedAway}
              busyId={mergingId}
              onRestore={(id, index) => { void runWrite("restore", id, id, index, () => onRestore(id)); }}
              feedback={feedbackFor("restore")}
            />
          )}
        </section>
      )}
    </div>
  );
}
