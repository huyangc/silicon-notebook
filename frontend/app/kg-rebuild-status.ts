/**
 * 「重新合并」后台任务的完成信号 —— 纯函数,页面只负责发请求和改 state。
 *
 * 端点后台化之后 POST 只认领任务槽,真正的结果要等 status 报终态。所以按钮的忙碌位不能
 * 靠 POST 的 await 解除,而要靠对 status 的**有界轮询**;这个模块就是那一步的判据,单独抽
 * 出来是为了能不挂整棵组件树就把每种终态钉住(镜像 kg-relink-status.ts 的做法)。
 *
 * 忙碌位本身(集合语义、认领、结算)与「补上关联」共用 notebook-busy-set.ts 的三个纯函数
 * ——两件事的形态逐字相同,复制一份只会多出一份没有单测的边角。
 */

import {
  busyForNotebook,
  claimNotebookSlot,
  releaseNotebookClaim,
} from "./notebook-busy-set.ts";

export { busyForNotebook, claimNotebookSlot, releaseNotebookClaim };

export type RebuildRunStatus = "running" | "succeeded" | "failed" | "idle";

export type UnifiedKgRebuildStatus = {
  job_id: string;
  notebook_id: string;
  status: RebuildRunStatus;
  running: boolean;
  clusters: number;
};

export type RebuildPollOutcome = {
  /** 轮询是否结束(结束就解除忙碌位)。 */
  done: boolean;
  /** 结束时是否按当前范围重拉图谱与待确认合并。 */
  refresh: boolean;
  /** 结束时给用户的提示;没有可说的就是 null。 */
  toast: string | null;
};

const KEEP_POLLING: RebuildPollOutcome = { done: false, refresh: false, toast: null };

/**
 * 轮询的尝试上限(3 秒一次 ⇒ 约 30 分钟)。
 *
 * 后端只在**进程内**记这件事,所以「进程还活着但任务卡死」这一种是 idle 兜不住的:
 * status 会一直如实回报 running,轮询就一直转。上限让按钮一定能解锁——用户只是要重新
 * 点一次,而不是刷新整页。它不取消后台任务(重新合并是幂等的确定性动作,没有取消入口),
 * 只是不再等了。
 */
export const REBUILD_POLL_MAX_ATTEMPTS = 600;

/** 等到上限仍未见终态时的收工回执(中性措辞:任务可能还在跑,别说它失败了)。 */
export const REBUILD_POLL_TIMED_OUT: RebuildPollOutcome = {
  done: true,
  refresh: true,
  toast: "重新合并还在进行，稍后打开知识图谱即可看到结果",
};

/**
 * 一次轮询回执 → 该做什么。
 *
 * `idle` 是**终态**而不是「还没开始」:服务端只在进程内记这件事,重启后就回 idle;它同时
 * 覆盖「这一格现在被『补上关联』占着」——那不是我们发起的任务,不该在这里空等。如实收工
 * 并重拉一次图谱(那次运行可能已经写进了新的聚类),但不编一个数字出来。
 */
export function rebuildPollOutcome(
  status: UnifiedKgRebuildStatus | null | undefined,
): RebuildPollOutcome {
  if (!status) return KEEP_POLLING;
  if (status.running || status.status === "running") return KEEP_POLLING;
  if (status.status === "succeeded") {
    return {
      done: true,
      refresh: true,
      // 数字来自服务端终态,不是 POST 的返回值——后台化之后 POST 根本没有它。
      toast: status.clusters > 0
        ? `已重新合并，现有 ${status.clusters} 组概念`
        : "已重新合并，暂时没有可合并的概念",
    };
  }
  if (status.status === "failed") {
    return { done: true, refresh: true, toast: "重新合并没有完成，请稍后重试" };
  }
  // idle:这个进程不知道有我们的任务在跑。收工并刷新,但不假装有统计。
  return { done: true, refresh: true, toast: null };
}
