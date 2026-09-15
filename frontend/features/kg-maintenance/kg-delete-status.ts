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

/** 等到轮询上限仍未见终态:任务可能还在跑,只说「可能仍在进行」,不说失败也不说不知道。 */
export const KG_DELETE_TIMEOUT_MESSAGE = "删除可能仍在进行，稍后重新打开知识图谱查看";

/**
 * 轮询的尝试上限(3 秒一次 ⇒ 约 30 分钟)。
 *
 * 后端只在**进程内**记这件事,所以「进程还活着但任务卡死」这一种是 idle 兜不住的:
 * status 会一直如实回报 running,轮询就一直转。上限让按钮一定能解锁。它不取消后台任务
 * (删除没有取消入口),只是不再等了,所以结果是中性的,不说它失败了。
 */
export const KG_DELETE_POLL_MAX_ATTEMPTS = 600;

const UNKNOWN_OUTCOME: KgDeletePollOutcome = {
  done: true,
  refresh: true,
  result: { tone: "neutral", text: KG_DELETE_UNKNOWN_MESSAGE },
};

/** 等到上限仍未见终态时的收工回执(只用于本标签页确实提交过或亲眼见过在跑的删除)。 */
export const KG_DELETE_POLL_TIMED_OUT: KgDeletePollOutcome = {
  done: true,
  refresh: true,
  result: { tone: "neutral", text: KG_DELETE_TIMEOUT_MESSAGE },
};

/**
 * 轮询看到的终态不属于我们期望的那个任务(job_id 连续对不上;进程重启后的 idle 也是空
 * job_id)时的收工回执:那份统计是别人的,不能拿来说「已删除 N 个」,只能如实说不知道,
 * 并刷新让界面对齐服务端——我们确实开始过一次删除,图谱可能已经变了。
 */
export const KG_DELETE_JOB_MISMATCH: KgDeletePollOutcome = UNKNOWN_OUTCOME;

/**
 * 本标签页既没有提交成功、也没有亲眼见过在跑的删除(例如 409 之后领养探测失败,或探测
 * 在切换笔记本期间落空)、且回执也证明不了有删除跑过(idle、或到了轮询上限)时的收工回执:
 * **静默**。只放掉忙碌位——不刷新(删除之后的刷新会清掉搜索与选中,那是替一次并未发生的
 * 删除收拾现场),不弹提示,也不覆盖按钮旁已有的那一行(例如服务端点名占用者的 409 文案)。
 * 同进程里更早一次删除留下的 `succeeded` 回执也不报数字:它与这次点击无关(但它证明图谱
 * 变过,所以走下面的 `KG_DELETE_UNOBSERVED_RAN` 刷新)。
 */
export const KG_DELETE_UNOBSERVED: KgDeletePollOutcome = { done: true, refresh: false, result: null };

/**
 * 没期望过、但回执本身证明**确有一次删除跑完了**(非空 job_id 且 succeeded / failed,例如
 * 另一个标签页发起的):图谱确实变了,所以照删除后的样子刷新(清搜索与选中、重拉图谱与
 * 依赖),其余仍守静默规则——不报数字、不弹成功提示、不覆盖按钮旁已有的那一行。
 */
export const KG_DELETE_UNOBSERVED_RAN: KgDeletePollOutcome = { done: true, refresh: true, result: null };

/** 没期望过的终态回执 → 纯释放,还是释放并刷新(见上两条)。idle 的空 job_id 永远是纯释放。 */
export function kgDeleteUnobservedOutcome(
  status: Pick<KgDeleteStatus, "job_id" | "status">,
): KgDeletePollOutcome {
  return status.job_id && (status.status === "succeeded" || status.status === "failed")
    ? KG_DELETE_UNOBSERVED_RAN
    : KG_DELETE_UNOBSERVED;
}

/**
 * 一条已结束的回执能不能以它自己的名义结算:
 * - `unobserved`:没有期望的 job_id(本标签页没提交成功、也没见过它在跑)——静默收工;
 * - `report`:job_id 正是期望的那个——按回执报数字 / 失败;
 * - `mismatch`:期望过某个任务,回执却是另一个(含重启后的空 job_id)——连续几次后说不知道。
 */
export function kgDeleteTerminalSettlement(
  status: Pick<KgDeleteStatus, "job_id">,
  expectedJobId: string | undefined,
): "report" | "mismatch" | "unobserved" {
  if (!expectedJobId) return "unobserved";
  return status.job_id === expectedJobId ? "report" : "mismatch";
}

/**
 * 一次轮询回执 → 该做什么(只看回执本身;它属不属于我们由 `kgDeleteTerminalSettlement` 判)。
 *
 * `idle` 是**终态**而不是「还没开始」:服务端只在进程内记这件事,重启后就回 idle;槽被
 * 「补上关联」「重新合并」占着时也回 idle。删除分页提交,重启可能留下删了一半的图,所以
 * 这里收工并刷新,结果说「不知道」而不是编一个数字。idle 的 job_id 恒为空,所以在轮询里
 * 它总是走 `mismatch`/`unobserved` 那两条,这一支只是让映射保持完整。
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
