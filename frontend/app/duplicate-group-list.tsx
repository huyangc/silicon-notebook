import { useEffect, useState } from "react";

import { LatexText } from "./answer-panel";
import { Pagination } from "./Pagination";
import { useClientPagination, type PaginationResetKey } from "./use-client-pagination.ts";
import { label, KNOWLEDGE_STATUS } from "./vocabulary";
import type { DuplicateGroup, DuplicatePartition, MergeOutcome } from "./workspace-model";

/** 查重结果整份返回,界面每页显示的重复组数。 */
const DUPLICATE_GROUP_PAGE_SIZE = 20;

/** 合并结果在旁边停留多久:够读完一句话,之后自己收起,下一次合并的结果不会与它混在一起。 */
export const MERGE_RESULT_HOLD_MS = 4000;

/** 本人记忆分区的标题与说明——说的是此刻这一段里有什么、谁看得到。 */
export const OWN_DUPLICATES_TITLE = "我的记忆里的重复";
export const OWN_DUPLICATES_HINT = "这些条目来自你的个人记忆，只有你能看到";

/** 一次合并的结果,连同它发生在哪一组(合并进的那一条 = 组的第一条)。 */
export type MergeFeedback = { intoId: string; outcome: MergeOutcome };

function MergeResult({ outcome }: { outcome: MergeOutcome }) {
  return outcome.tone === "done" ? (
    <p className="dup-merge-result dup-merge-result-done" role="status">{outcome.text}</p>
  ) : (
    <p className="dup-merge-result dup-merge-result-failed" role="status">{outcome.text}</p>
  );
}

/**
 * 知识浏览器里的「查重」结果:一批重复组,每组内可以把非第一条合并进第一条。
 * `resetKey` 传当前 kind——切换知识类型 tab 时,前一个 tab 翻到的页码不该带过来。
 *
 * `feedback`:上一次合并的结果。那一组(第一条 = `intoId`)还在当前页上,结果就显示在
 * 那一组里;那一组不在当前页上(因合并只剩一条、不再成组,或重排后到了别的页),就显示在
 * 列表最上面——也就是刚才那颗按钮所在的这一段,而不是页面顶部。
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
  onMerge: (sourceId: string, intoId: string) => void;
  resetKey?: PaginationResetKey;
  feedback?: MergeFeedback | null;
  pagerLabel?: string;
}) {
  const page = useClientPagination(duplicates, DUPLICATE_GROUP_PAGE_SIZE, resetKey);
  const anchored = feedback !== null
    && page.pageItems.some((group) => group.members[0]?.id === feedback.intoId);
  return (
    <>
      {feedback && !anchored && <MergeResult outcome={feedback.outcome} />}
      {page.pageItems.map((group, index) => (
        <article className="item" key={`dup-${index}`}>
          <div className="tag-row"><span className="tag">similarity {group.similarity}</span></div>
          {group.members.map((member, memberIndex) => (
            <div className="dup-member" key={member.id}>
              <span><LatexText text={member.headline} isFormula={(member.object_type || group.object_type) === "formula"} /> <span className="tag">{label(KNOWLEDGE_STATUS, member.status, "其他")}</span></span>
              {!readOnly && memberIndex > 0 && (
                <button
                  className="sort-button"
                  disabled={mergingId !== null}
                  onClick={() => onMerge(member.id, group.members[0].id)}
                >
                  {mergingId === member.id ? "合并中…" : "合并到第 1 条"}
                </button>
              )}
            </div>
          ))}
          {feedback && anchored && group.members[0]?.id === feedback.intoId && (
            <MergeResult outcome={feedback.outcome} />
          )}
        </article>
      ))}
      <Pagination page={page.page} pageSize={DUPLICATE_GROUP_PAGE_SIZE} total={page.total} onPage={page.setPage} label={pagerLabel} />
    </>
  );
}

/**
 * 「查重」的两段结果:共享重复组(有知识治理权的人才拉、才显示),以及它下方单独成段的
 * 「我的记忆里的重复」(只由本人记忆支撑、只有本人看得到;没有就不显示这一段)。两段
 * 各自分页、各自显示自己的合并结果;同一时刻只有一次合并在途,两段的按钮一起禁用。
 * 只读成员只有本人那一段:她看不到共享组,也不请求共享查重。
 */
export function DuplicateResults({
  shared,
  own,
  canMergeShared,
  canMergeOwn,
  onMerge,
  resetKey,
}: {
  /** `null`:没拉共享组(没有治理权,或还没点「查重」)。 */
  shared: DuplicateGroup[] | null;
  own: DuplicateGroup[] | null;
  canMergeShared: boolean;
  canMergeOwn: boolean;
  onMerge: (
    sourceId: string, intoId: string, partition: DuplicatePartition,
  ) => Promise<MergeOutcome | null> | void;
  resetKey?: PaginationResetKey;
}) {
  // 正在合并的重复条目 id。合并会连着重拉知识列表、类型统计并重跑这一段的查重,不锁住的话
  // 在几个重复组上连点会并发排出若干次重扫。
  const [mergingId, setMergingId] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<
    { partition: DuplicatePartition; value: MergeFeedback } | null
  >(null);

  useEffect(() => {
    if (feedback === null) return;
    const timer = window.setTimeout(() => setFeedback(null), MERGE_RESULT_HOLD_MS);
    return () => window.clearTimeout(timer);
  }, [feedback]);

  // 换了知识类型:上一类的结果不该挂到这一类的组上。
  useEffect(() => {
    setFeedback(null);
  }, [resetKey]);

  const runMerge = async (partition: DuplicatePartition, sourceId: string, intoId: string) => {
    if (mergingId) return;
    setMergingId(sourceId);
    setFeedback(null);
    try {
      const outcome = await onMerge(sourceId, intoId, partition);
      if (outcome) setFeedback({ partition, value: { intoId, outcome } });
    } finally {
      setMergingId(null);
    }
  };

  const feedbackFor = (partition: DuplicatePartition) => (
    feedback?.partition === partition ? feedback.value : null
  );
  const ownGroups = own ?? [];
  const ownFeedback = feedbackFor("own");
  const showOwn = ownGroups.length > 0 || ownFeedback !== null;

  return (
    <div className="knowledge-panel">
      {shared !== null && (
        <>
          <p className="section-title">重复组（规范名归一化后相同）</p>
          {shared.length === 0 && feedbackFor("shared") === null ? (
            <p className="tool-hint">未发现重复。</p>
          ) : (
            <DuplicateGroupList
              duplicates={shared}
              readOnly={!canMergeShared}
              mergingId={mergingId}
              onMerge={(sourceId, intoId) => { void runMerge("shared", sourceId, intoId); }}
              resetKey={resetKey}
              feedback={feedbackFor("shared")}
            />
          )}
        </>
      )}
      {shared === null && !showOwn && <p className="tool-hint">未发现重复。</p>}
      {showOwn && (
        <section className="dup-own-section" aria-label={OWN_DUPLICATES_TITLE}>
          <p className="section-title">{OWN_DUPLICATES_TITLE}</p>
          <p className="tool-hint">{OWN_DUPLICATES_HINT}</p>
          <DuplicateGroupList
            duplicates={ownGroups}
            readOnly={!canMergeOwn}
            mergingId={mergingId}
            onMerge={(sourceId, intoId) => { void runMerge("own", sourceId, intoId); }}
            resetKey={resetKey}
            feedback={ownFeedback}
            pagerLabel="我的记忆重复组分页"
          />
        </section>
      )}
    </div>
  );
}
