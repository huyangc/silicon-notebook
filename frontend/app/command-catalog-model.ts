// 命令目录(方案 C·C1c)的纯装配逻辑:内部枚举 → 界面词、任务状态 → 进度/摘要文案、
// 忙碌态解除判据、确认结果归纳。单测于 command-catalog-model.test.mjs。
//
// 值导入须带 .ts 后缀:本模块被 node --test 直接加载(见 checkup-view.ts 同款)。
import { label } from "./vocabulary.ts";
import type {
  CommandCatalogApplyResult,
  CommandCatalogCandidate,
  CommandCatalogDismissResult,
  CommandCatalogJob,
  CommandCatalogPreview,
  CommandCatalogProgress,
} from "./command-catalog-api.ts";

// ---------------------------------------------------------------- 任务状态

/**
 * 任务已落终态的三个值(后端 `CATALOG_TERMINAL_STATUSES` 逐字对齐)。
 *
 * 判据只列**终态**、不列 queued/running,方向是刻意的:后端将来若新增一个中间状态
 * (比如 "paused"),未知值会落进「还没结束」,按钮继续禁用 —— 那是安全的一边。
 * 反过来枚举「活跃状态」则会把未知值当成已结束,把「点完还能接着点」放回来。
 */
export const CATALOG_TERMINAL_STATUSES: ReadonlySet<string> = new Set([
  "succeeded",
  "failed",
  "cancelled",
]);

export function isCatalogSettled(status: string): boolean {
  return CATALOG_TERMINAL_STATUSES.has(status);
}

/**
 * 「识别中…」现在是否还该显示 —— 长任务按钮忙碌态的**唯一**判据。
 *
 * 两段合起来才完整:
 *   · `starting` 是 POST 在飞的那一小段。此时 job 还没回来(甚至可能撞 409),没有它
 *     按钮会在「点下去」到「第一个 job 快照到手」之间短暂恢复可点,而这中间用户
 *     完全来得及再点一次。
 *   · job 到手之后改按**任务行**判定 —— 那是跨请求、跨刷新都成立的证据,不是本地
 *     乐观标志。刷新页面后仍在跑的任务照样把按钮锁住。
 *
 * ⚠ 解除**不能**改成「POST 返回即解除」:POST 返回只代表任务排上了,识别本身在后台
 * 跑数分钟。有界轮询窗口关闭与切换来源是兜底(见 command-catalog-panel.tsx),不是主
 * 判据 —— 主判据永远是这里的终态证据。
 */
export function isCatalogBusy(starting: boolean, job: CommandCatalogJob | null): boolean {
  if (starting) return true;
  return job !== null && !isCatalogSettled(job.status);
}

// ---------------------------------------------------------------- 进度/摘要

/** 因过长被截断、只识别了开头部分的节数提示。为 0 时不渲染任何提示。 */
function catalogTruncationNote(progress: CommandCatalogProgress): string {
  const truncated = progress.truncated_sections;
  return truncated > 0 ? `${truncated} 节因过长仅识别了开头部分` : "";
}

/**
 * 识别进行中的一行进度。sections_total 为 0 表示任务已建、还没算出总节数
 * (queued 或刚 running),这时报 0/0 会让人以为卡住了,改说「准备中…」。
 */
export function catalogProgressText(job: CommandCatalogJob): string {
  const { sections_total: total, sections_done: done, entries } = job.progress;
  if (total <= 0) return "准备中…";
  const parts = [`已处理 ${done}/${total} 节 · 已识别 ${entries} 条`];
  const truncationNote = catalogTruncationNote(job.progress);
  if (truncationNote) parts.push(truncationNote);
  return parts.join(" · ");
}

export type CatalogStatusLine = {
  /** 正文。 */
  text: string;
  /** 展示语气:进行中 / 正常结果 / 需要注意。异常态走 --danger,由调用方上色。 */
  tone: "progress" | "info" | "danger";
};

/**
 * 入口卡片上那一行状态文案。
 *
 * · failed 直接展示后端 `failure_reason` —— 它是按 `user_error()` 口径写的中文用户
 *   文案(内部诊断在后端另一列,根本不进传输层)。为空时才退到通用兜底。
 * · cancelled **不**展示 failure_reason:那一列此刻恒为一句「已取消」的同义句,
 *   没有多余信息,而它出自后端的句子带句号、拼进卡片里读着像半截报错。这里自己
 *   写一句更干净的,信息量零损失。
 */
export function catalogStatusLine(job: CommandCatalogJob | null): CatalogStatusLine | null {
  if (!job) return null;
  if (!isCatalogSettled(job.status)) {
    return { text: catalogProgressText(job), tone: "progress" };
  }
  if (job.status === "failed") {
    return { text: job.failure_reason || "识别没能完成，请稍后重试", tone: "danger" };
  }
  if (job.status === "cancelled") {
    return { text: "已取消，已识别的结果仍可查看", tone: "info" };
  }
  const { entries, rejected, uncovered } = job.progress;
  if (entries <= 0) {
    // 零产出必须说清「为什么」的线索在哪:未通过条目是用户唯一能自己判断「模型错了
    // 还是这份文档本来就不是手册」的依据,所以这里主动把它报出来。
    return {
      text: rejected > 0
        ? `未识别出命令，有 ${rejected} 条未通过校验，可在结果里查看原因`
        : "未识别出命令，这份来源可能不是命令手册",
      tone: "danger",
    };
  }
  const parts = [`已识别 ${entries} 条命令`];
  if (rejected > 0) parts.push(`${rejected} 条未通过`);
  if (uncovered > 0) parts.push(`${uncovered} 节未覆盖`);
  const truncationNote = catalogTruncationNote(job.progress);
  if (truncationNote) parts.push(truncationNote);
  return { text: parts.join(" · "), tone: "info" };
}

/** 入口主按钮的文案。忙碌态由 isCatalogBusy 决定,不看这里。 */
export function catalogEntryLabel(job: CommandCatalogJob | null): string {
  if (job && job.status === "succeeded") return "查看识别结果";
  return "识别命令目录";
}

// -------------------------------------------------------------- 重新识别拦截

/**
 * 重新发起识别(不论走的是「重新识别」还是失败/取消后的主按钮「识别命令
 * 目录」)是否该被上一轮还没审完的候选拦住。
 *
 * R6 P1:从「只拦 succeeded」改成**任意终态**(succeeded/failed/cancelled)。
 * R5 版本的依据是「失败/取消的任务走主按钮`识别命令目录`入口,那句
 * `INTERRUPTED_MESSAGE`/`CANCELLED_MESSAGE` 本身已经告诉用户候选保留、邀请
 * 重新发起」——codex 评审指出这站不住:*保留*不等于*够得着*。`.../job` 只
 * 返回一个来源最近一次任务,不管上一轮是成功、失败还是取消,新任务一发起,
 * 旧任务的候选都会永久孤儿化(界面从此再也拿不到那个 job id)。所以这条判据
 * 现在覆盖 `isCatalogSettled` 的全部三个终态,活跃状态(queued/running)仍然
 * 不归它管——那是单飞守卫的地盘。第二道防线在后端 `catalog_job.py` 的
 * `_reject_if_pending_candidates`(同样已改成覆盖任意终态)。
 */
export function catalogBlocksRestart(job: CommandCatalogJob | null): boolean {
  if (!job) return false;
  return isCatalogSettled(job.status) && job.progress.pending_candidates > 0;
}

/** 「重新识别」被拦住时,按钮旁那句解释文案。 */
export function catalogPendingReviewNote(pending: number): string {
  return `仍有 ${pending} 条待审阅候选，请先确认或跳过后再重新识别。`;
}

// ---------------------------------------------------------------- 成本预告

/**
 * ⚠ 正文字段刻意**不叫** `message`:`errors-guard.test.mjs` 把 app 目录下每一处
 * `.message` 读取都记进「需人工复核的原始错误文本」台账(「这是不是错误对象」静态
 * 判不了,所以规则故意粗)。这里是确认弹窗的用户文案,不是错误 —— 为它开一条豁免
 * 只会稀释那份台账的意义,换个名字成本为零。
 */
export type CatalogPreviewCopy = {
  title: string;
  body: string;
  sections: Array<[string, string[]]>;
  confirmLabel: string;
};

/**
 * 成本预告确认弹窗的文案。
 *
 * 三条硬约束:
 *  1. 数字一律写成**约数**。preview 只读了一段有界前缀,真实成本只会更高不会更低。
 *  2. `sampled` 时必须明说只看了前多少条,否则那句「预计 M 次」是个守不住的承诺。
 *  3. 非手册形状**不禁用入口**,只换提示语 —— 形状检测是启发式,凭它替用户否决
 *     一次识别是越权;把计数摆出来让人自己判断才是诚实用法。
 */
export function catalogPreviewCopy(preview: CommandCatalogPreview): CatalogPreviewCopy {
  const { estimated_sections: sections, estimated_calls: calls, sampled, signal } = preview;
  const cost = `检测到约 ${sections} 个命令节，预计 ${calls} 次模型调用。`;
  const sampledNote = sampled
    ? `成本只按前 ${preview.element_limit} 个元素估算，实际可能更多。`
    : "";
  const shapeNote = signal.is_manual
    ? ""
    : "未检测到明显的命令手册结构，识别结果可能很少或为空。";
  return {
    title: "识别命令目录",
    body: [shapeNote, cost, sampledNote].filter(Boolean).join(""),
    sections: [
      // 这里的分子分母是**块口径**(每个语法/参数子段各算一块),与主句「检测到约
      // N 个命令节」的**分组口径**(同一命令的子段合并计数)不是同一个数,写法必须
      // 显式区分,否则同一张卡上两句话像是在互相矛盾。
      ["形状线索", [
        `命令样式的段落 ${signal.command_shaped_sections}/${signal.total_sections}（含语法/参数子段）`,
        `含语法说明 ${signal.syntax_sections}`,
        `含参数说明 ${signal.flag_sections}`,
      ]],
    ],
    confirmLabel: "开始识别",
  };
}

// ---------------------------------------------------------------- 未通过原因

// 接地校验拦下一个值时记录的字段位置。真源:backend/app/services/command_catalog.py
// 的 `_reject(...)` 调用点 + catalog_job.py 里那条整片失败的 "slice"。
const REJECT_FIELD: Record<string, string> = {
  command_name: "命令名",
  arg: "参数",
  default: "默认值",
  syntax: "语法",
  slice: "整段",
};

// 拦截原因。真源同上;文案写「原文是什么样」而不是「模型错在哪」——用户要判断的是
// 这条该不该留,而不是模型的内部状态。
const REJECT_REASON: Record<string, string> = {
  empty: "模型没有给出这个值",
  not_in_candidates: "这一节没有记录这条命令",
  not_in_text: "原文里找不到这个写法",
  dash_stripped: "少了前面的短横（原文写作 -xxx）",
  not_contiguous: "与原文不连续",
  model_response_unusable: "模型返回无法解析",
  // 一条命令的参数多时会分几批提取,每批只问其中一部分。这两条说的是「这一批
  // 该给的没给、不该给的给了」——都不是原文的问题,所以文案落在「这一批」而不是
  // 「原文里没有」,否则用户会去原文里找一个明明存在的参数。
  arg_outside_slice: "不在这一批要提取的参数里",
  arg_not_returned: "原文里有，但模型没有提取",
};

/** 一条未通过原因的中文说明。 */
export function rejectionText(field: string, reason: string, value: string): string {
  const where = label(REJECT_FIELD, field, "内容");
  const why = label(REJECT_REASON, reason, "未通过校验");
  return value ? `${where}「${value}」：${why}` : `${where}：${why}`;
}

// ---------------------------------------------------------------- 已跳过原因

// 顶层跳过原因码，三个写者各写各的。真源:backend/app/services/catalog_job.py ——
// `_apply_locked` 对冲突候选写 `reject_info={"reason": "conflict_existing_row"}`
// (确认时发现目标表已有同名行，自动跳过)；`dismiss()` 对审阅者显式跳过的候选写
// `reject_info={"reason": "user_dismissed"}`(R7:「重新识别」被待审阅候选拦住时
// 唯一的放弃出口)；`_require_current_generation` 对来源被重新解析之后整批作废的
// 候选写 `reject_info={"reason": "source_reparsed"}`(R8:它们描述的是文档里已经
// 不存在的内容，既不能确认、也不该继续占着重跑守卫)。与 REJECT_REASON 是两套
// 互不相关的词表——那套按字段接地失败，这套是「候选本身没问题，只是没有被写进表」。
const DISMISS_REASON: Record<string, string> = {
  conflict_existing_row: "已存在同名行",
  user_dismissed: "人工跳过",
  source_reparsed: "来源已重新解析，结果已过期",
};

/** 一条「已跳过」原因的中文说明。 */
export function dismissReasonText(reason: string): string {
  return label(DISMISS_REASON, reason, "未写入目标表");
}

// ---------------------------------------------------------------- 拦截账目溢出

/**
 * 展开面板拦截账目区块尾部的溢出小字。
 *
 * 两个数各自独立触发、独立成句,不折成一句——它们数的是两种不相关的丢失
 * (`rejectionsOverflow`:超过 `MAX_SECTION_REJECTIONS` 未能进 `rejections`
 * 列表的记录数;`descOverflow`:因 `MODEL_ARG_DESC_TOTAL_CHARS` 聚合预算被截短
 * 的参数说明段数),折在一起会得到一个两头都答不上的数(真源:后端
 * `_reject_info()` 的 docstring)。都为 0 时返回空串,调用方据此不渲染。
 */
export function catalogOverflowNote(rejectionsOverflow: number, descOverflow: number): string {
  const parts: string[] = [];
  if (rejectionsOverflow > 0) parts.push(`另有 ${rejectionsOverflow} 条拦截记录未显示`);
  if (descOverflow > 0) parts.push(`有 ${descOverflow} 段参数描述因超长被截`);
  return parts.join("；");
}

// ---------------------------------------------------------------- 确认结果

/**
 * 一次确认要提交哪些 id。
 *
 * 后端对超限显式 422(`apply_command_catalog` 的 `_APPLY_TOO_MANY_MESSAGE`),不会
 * 静默截断;这里提前切到 100 条不是为了防错,而是为了让「已选 150」这类多选仍能
 * 走一次部分确认(提交前 100 条)而不是整批被 422 拒绝、什么都没落库。按 position
 * 升序切,和列表看到的顺序一致。
 *
 * 超出上限的那部分**不会**留在选中集里等用户接着点:调用方(command-catalog-
 * panel.tsx 的 runApply)确认落地后会把整个选中集清空,再从头重拉列表——已提交
 * 的那批状态已经变了(candidate → applied),继续让没提交的那部分跨这次重拉存活
 * 在选中集里,承担的是「指向可能已经漂移的行」的风险,不比让用户重新勾一次更划算。
 * 界面在按钮旁提示「其余需重新勾选」,信息量不打折扣。
 */
export function catalogApplyBatch(
  selected: ReadonlySet<string>,
  loaded: readonly CommandCatalogCandidate[],
  cap: number,
): string[] {
  return loaded
    .filter((item) => selected.has(item.id))
    .sort((a, b) => a.position - b.position)
    .slice(0, Math.max(0, cap))
    .map((item) => item.id);
}

export type CatalogApplyOutcome = {
  /** 主结果句。 */
  headline: string;
  /** 同名冲突提示;无冲突时为空串。 */
  conflictNote: string;
  /** 仍待审阅的提示;为 0 时为空串。 */
  remainingNote: string;
  /** 是否该给出「去看表格」入口 —— 只有真的落了行、且拿得到表 id 时才给。 */
  canOpenTable: boolean;
};

/**
 * 确认结果归纳。
 *
 * 三件事必须同时说清,少一件都会让人误判:
 *  · 新增了几条(rows_added,可能为 0);
 *  · 哪些**没有**写入(conflicts:目标表已有同名行,v1 绝不覆盖手工编辑过的内容);
 *  · 还剩几条待审阅(pending_remaining:一次最多确认一页,300 条点一次「确认全部」
 *    拿到 rows_added:100 会以为做完了)。
 *
 * 表名读 `result.table_title`(后端权威值,R15/codex PR #412 评审第 15 轮),
 * 不再由调用方传一个从 `sourceDetail.title` 预测出来的字符串——论文来源的
 * 上传文件名与后端实际建表所用的规范标题(接地论文标题优先)可以不同,预测
 * 会让「已写入《命令目录：<预测名>》」这句话在那种来源上撒谎。
 */
export function catalogApplyOutcome(result: CommandCatalogApplyResult): CatalogApplyOutcome {
  const where = result.table_title ? `《${result.table_title}》` : "命令目录表";
  const headline = result.rows_added > 0
    ? (result.created
      ? `已新建${where}并写入 ${result.rows_added} 条命令`
      : `已向${where}新增 ${result.rows_added} 条命令`)
    : "没有新增命令";
  const names = result.conflicts.map((item) => item.command_name).filter(Boolean);
  const shown = names.slice(0, 5).join("、");
  const conflictNote = result.conflicts.length === 0
    ? ""
    : `${result.conflicts.length} 条已存在同名行，未改动，已跳过，不再重复确认：${shown}`
      + (names.length > 5 ? ` 等 ${result.conflicts.length} 条` : "");
  const remainingNote = result.pending_remaining > 0
    ? `还有 ${result.pending_remaining} 条待审阅，可以继续确认。`
    : "";
  return {
    headline,
    conflictNote,
    remainingNote,
    canOpenTable: result.rows_added > 0 && Boolean(result.table_id),
  };
}

/**
 * 跳过结果归纳。复用 `CatalogApplyOutcome` 这个形状而不是新起一套——审阅弹窗
 * 用同一块 `catalog-outcome` 展示区渲染两种动作的结果,少一次「两套摘要卡长得
 * 不一样」的界面不一致。跳过从不写表也从不产生冲突,所以 `conflictNote` 恒
 * 为空、`canOpenTable` 恒为 false;`remainingNote` 与 apply 同一语义与同一句
 * 措辞——跳过后「重新识别」的拦截守卫是否已解除,读的是同一个 pending_remaining。
 */
export function catalogDismissOutcome(result: CommandCatalogDismissResult): CatalogApplyOutcome {
  const count = result.dismissed.length;
  const headline = count > 0 ? `已跳过 ${count} 条候选` : "没有可跳过的候选";
  const remainingNote = result.pending_remaining > 0
    ? `还有 ${result.pending_remaining} 条待审阅，可以继续确认或跳过。`
    : "";
  return { headline, conflictNote: "", remainingNote, canOpenTable: false };
}
