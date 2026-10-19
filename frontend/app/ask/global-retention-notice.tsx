import { formatQuestionTime } from "../chat-question-time";

/** 一轮全局问答用到的某个笔记本已被删除：这一轮保留到什么时候。
 *
 *  后端只在这一轮真被删除留存盖过戳时才给 `retained_until`（见
 *  `global-deleted-sources.ts` 顶部的裁决）；没有就一个节点都不多。文案只说现象与
 *  期限，与管理员活动详情里「原笔记本已删除…留存至」同一个时间写法。 */
export function GlobalRetentionNotice({ until, now }: { until?: string; now?: Date }) {
  if (!until) return null;
  const when = formatQuestionTime(until, now);
  return <p className="global-retention-notice" role="note">
    {when
      ? `本轮用到的笔记本已被删除，这一轮问答保留到 ${when}，之后自动清除。`
      : "本轮用到的笔记本已被删除，这一轮问答会在留存期结束后自动清除。"}
  </p>;
}
