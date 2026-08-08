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
 * 忙碌位是否作用于**当前打开的**笔记本。
 *
 * 「补上关联」是按笔记本单飞的后台任务，忙碌位因此存的是「哪些库在补」的一个集合，不是
 * 一个裸布尔，也不是只能记一个库的裸字符串——用户可以点完 A 就切到 B 再点一次，两次
 * 认领必须都活着（单值形态会让后一次覆盖前一次，A 的完成信号就此丢失，见
 * releaseRelinkClaim 的同一条注释）。按钮的 disabled 与轮询 effect 共用这一条判据：只看
 * 「当前这个库在不在集合里」，不在集合里的库照常可点、不轮询。
 */
export function relinkBusyFor(
  relinkingNotebookIds: ReadonlySet<string>,
  currentNotebookId: string | null,
): boolean {
  return currentNotebookId !== null && relinkingNotebookIds.has(currentNotebookId);
}

/**
 * 认领某个笔记本的忙碌位：只加自己那一格，其余库的认领原样保留。
 */
export function claimRelinkSlot(
  previous: ReadonlySet<string>,
  notebookId: string,
): Set<string> {
  if (previous.has(notebookId)) return previous as Set<string>;
  const next = new Set(previous);
  next.add(notebookId);
  return next;
}

/**
 * 结算某个笔记本的忙碌位：只清自己那一格。
 *
 * 用户完全可以点完 A 就切到 B 再点一次——A 那条迟到的终态（或失败回执）绝不能把 B 刚
 * 置上的忙碌位一并抹掉，否则 B 的按钮立刻可点、轮询却已经被这一下清掉了。Set 语义天生
 * 满足这一条：删除自己的键从不触碰其余键，不需要像单值形态那样先比较「是不是我」。
 */
export function releaseRelinkClaim(
  previous: ReadonlySet<string>,
  settled: string,
): Set<string> {
  if (!previous.has(settled)) return previous as Set<string>;
  const next = new Set(previous);
  next.delete(settled);
  return next;
}

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
