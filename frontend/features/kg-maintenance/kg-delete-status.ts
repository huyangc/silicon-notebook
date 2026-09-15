/**
 * 「删除知识图谱」后台任务的完成信号 —— 纯函数,页面只负责发请求和改 state。
 *
 * 与「补上关联」「重新合并」共用服务端同一个按笔记本单飞的维护槽(holder kind "delete"):
 * POST 只认领任务槽并返回 job_id,删了多少要等 delete/status 报终态。所以按钮的忙碌位
 * 不能靠 POST 的 await 解除,而要靠对 status 的**有界轮询**;这个模块就是那一步的判据,
 * 单独抽出来是为了能不挂整棵组件树就把每种终态钉住(镜像 kg-relink-status.ts 的做法)。
 *
 * 与那两件事的区别只在「结果落在哪」:删除是破坏性动作,结果必须画在按钮紧邻处
 * (AGENTS.md Interactive feedback),所以这里产出的是带语气的 `result`,而不是一句 toast。
 */

export type KgDeleteRunStatus = "running" | "succeeded" | "failed" | "idle";

export type KgDeleteStatus = {
  job_id: string;
  notebook_id: string;
  status: KgDeleteRunStatus;
  running: boolean;
  objects_deleted: number;
  relations_deleted: number;
};

/**
 * 按钮旁那一行结果的语气:成功 / 失败 / 中性。中性覆盖两种「没法说成败」的情形:
 * 状态不知道(任务可能还在跑,或进程重启过),以及这次点击因为槽被占着根本没开始。
 */
export type KgDeleteResultTone = "success" | "failed" | "neutral";

/**
 * 字段名是 `text` 而不是 `message`:这是一条已经写好的界面文案,不是捕获到的异常;
 * errors-guard 把任何 `.message` 读取都当作「可能在直出原始错误」逐个登记审核,
 * 界面反馈类型一律用 `text`(与 admin/usage 的 Notice 同一口径)。
 */
export type KgDeleteResult = {
  tone: KgDeleteResultTone;
  text: string;
};

export type KgDeletePollOutcome = {
  /** 轮询是否结束(结束就解除忙碌位)。 */
  done: boolean;
  /** 结束时是否重拉图谱、待确认合并、笔记本摘要与来源列表。 */
  refresh: boolean;
  /** 结束时画在按钮旁的结果;还没结束就是 null。 */
  result: KgDeleteResult | null;
};

const KEEP_POLLING: KgDeletePollOutcome = { done: false, refresh: false, result: null };

export const KG_DELETE_FAILED_MESSAGE = "删除没有完成，请重试";

export const KG_DELETE_UNKNOWN_MESSAGE = "删除状态未知，请刷新后查看";

/** 409 没带可展示文案时的兜底(正常情况下服务端的 409 会点名占着维护槽的任务)。 */
export const KG_DELETE_BUSY_MESSAGE = "当前有其他整理任务在进行，请等它完成后再删除";

/**
 * 结果在按钮旁保留多久(毫秒)。结果是 JS 状态,不会像 `:active` 那样自己复原,所以必须
 * 按自己的计时器清掉——否则「已删除 N 个知识对象」会一直钉在那里,下一次删除的结果看起来
 * 和上一次没有区别。比复制按钮的 1.6s 长:这是一个后台任务的回执,落地时用户未必正看着。
 */
export const KG_DELETE_RESULT_HOLD_MS = 8000;

/**
 * 轮询的尝试上限(3 秒一次 ⇒ 约 30 分钟)。
 *
 * 后端只在**进程内**记这件事,所以「进程还活着但任务卡死」这一种是 idle 兜不住的:
 * status 会一直如实回报 running,轮询就一直转。上限让按钮一定能解锁。它不取消后台任务
 * (删除没有取消入口),只是不再等了,所以结果是中性的「不知道」,不说它失败了。
 */
export const KG_DELETE_POLL_MAX_ATTEMPTS = 600;

const UNKNOWN_OUTCOME: KgDeletePollOutcome = {
  done: true,
  refresh: true,
  result: { tone: "neutral", text: KG_DELETE_UNKNOWN_MESSAGE },
};

/** 等到上限仍未见终态时的收工回执。 */
export const KG_DELETE_POLL_TIMED_OUT: KgDeletePollOutcome = UNKNOWN_OUTCOME;

/**
 * 轮询看到的终态不属于我们提交的那个任务(job_id 连续对不上)时的收工回执:那份统计是
 * 别人的,不能拿来说「已删除 N 个」,只能如实说不知道,并刷新让界面对齐服务端。
 */
export const KG_DELETE_JOB_MISMATCH: KgDeletePollOutcome = UNKNOWN_OUTCOME;

/**
 * 一次轮询回执 → 该做什么。
 *
 * `idle` 是**终态**而不是「还没开始」:服务端只在进程内记这件事,重启后就回 idle;槽被
 * 「补上关联」「重新合并」占着时也回 idle。删除分页提交,重启可能留下删了一半的图,所以
 * 这里收工并刷新,结果说「不知道」而不是编一个数字。
 */
export function kgDeletePollOutcome(
  status: KgDeleteStatus | null | undefined,
): KgDeletePollOutcome {
  if (!status) return KEEP_POLLING;
  if (status.running || status.status === "running") return KEEP_POLLING;
  if (status.status === "succeeded") {
    return {
      done: true,
      refresh: true,
      result: {
        tone: "success",
        // 数字来自服务端终态,不是 POST 的返回值——后台化之后 POST 根本没有它。
        text: status.objects_deleted > 0
          ? `已删除 ${status.objects_deleted} 个知识对象`
          : "没有可删除的知识对象",
      },
    };
  }
  if (status.status === "failed") {
    return {
      done: true,
      refresh: true,
      result: { tone: "failed", text: KG_DELETE_FAILED_MESSAGE },
    };
  }
  return UNKNOWN_OUTCOME;
}
