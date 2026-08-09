/**
 * 「补上关联」后台任务的完成信号 —— 纯函数,页面只负责发请求和改 state。
 *
 * 端点是后台化之后新加的:POST 只认领任务槽,统计要等 status 报终态。所以按钮的忙碌位
 * 不能靠 POST 的 await 解除,而要靠对 status 的**有界轮询**;这个模块就是那一步的判据,
 * 单独抽出来是为了能不挂整棵组件树就把每种终态钉住(镜像 kg-build-status.ts 的做法)。
 */

export type RelinkRunStatus = "running" | "succeeded" | "failed" | "idle";

export type RelinkStatus = {
  job_id: string;
  notebook_id: string;
  status: RelinkRunStatus;
  running: boolean;
  isolated_before: number;
  edges_added: number;
  isolated_after: number;
};

export type RelinkPollOutcome = {
  /** 轮询是否结束(结束就解除忙碌位)。 */
  done: boolean;
  /** 结束时是否按当前范围重拉图谱。 */
  refresh: boolean;
  /** 结束时给用户的提示;没有可说的就是 null。 */
  toast: string | null;
};

const KEEP_POLLING: RelinkPollOutcome = { done: false, refresh: false, toast: null };

/**
 * 轮询的尝试上限(3 秒一次 ⇒ 约 30 分钟)。
 *
 * 后端只在**进程内**记这件事,所以「进程还活着但任务卡死」这一种是 idle 兜不住的:
 * status 会一直如实回报 running,轮询就一直转。上限让按钮一定能解锁——用户只是要重新
 * 点一次,而不是刷新整页。它不取消后台任务(那件事没有取消入口,也不该有:补上关联是
 * 幂等的确定性动作),只是不再等了。
 */
export const RELINK_POLL_MAX_ATTEMPTS = 600;

/** 等到上限仍未见终态时的收工回执(中性措辞:任务可能还在跑,别说它失败了)。 */
export const RELINK_POLL_TIMED_OUT: RelinkPollOutcome = {
  done: true,
  refresh: true,
  toast: "补上关联还在进行，稍后打开知识图谱即可看到结果",
};

/**
 * 忙碌位是「哪些库在补」的一个集合，判据/认领/结算三个纯函数与「重新合并」共用一份
 * 实现（notebook-busy-set.ts，那里有为什么必须是集合的完整论证与单测）。这里保留
 * 「补上关联」语境下的历史命名：接线守卫按这几个名字认 page.tsx 的形态，改名不会让
 * 任何一条断言变红、只会让它们静默失配。
 */
export {
  busyForNotebook as relinkBusyFor,
  claimNotebookSlot as claimRelinkSlot,
  releaseNotebookClaim as releaseRelinkClaim,
} from "./notebook-busy-set.ts";

/**
 * 一次轮询回执 → 该做什么。
 *
 * `idle` 是**终态**而不是「还没开始」:服务端只在进程内记这件事,重启后就回 idle。把它
 * 当运行中会让按钮永远转下去,所以这里如实收工——重拉一次图谱(那次运行可能已经写进了
 * 边),但不编一个「已补上 N 条」的数字出来。
 */
export function relinkPollOutcome(
  status: RelinkStatus | null | undefined,
): RelinkPollOutcome {
  if (!status) return KEEP_POLLING;
  if (status.running || status.status === "running") return KEEP_POLLING;
  if (status.status === "succeeded") {
    return {
      done: true,
      refresh: true,
      toast: status.edges_added > 0
        ? `已补上 ${status.edges_added} 条关联，还有 ${status.isolated_after} 项内容没建立关联`
        : `没有可补的关联，还有 ${status.isolated_after} 项内容没建立关联`,
    };
  }
  if (status.status === "failed") {
    return { done: true, refresh: true, toast: "补上关联没有完成，请稍后重试" };
  }
  // idle:这个进程不知道有任务在跑。收工并刷新,但不假装有统计。
  return { done: true, refresh: true, toast: null };
}
