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
