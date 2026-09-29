/**
 * citation-verification.ts
 *
 * 全局问答终态引用核对的**呈现口径**——唯一出处。
 *
 * 产品裁决（2026-09-29，Q3「部分失败而非整份作废」）：核对不通过**从不**作废回答。
 * 回答原样交付，答案下方一句如实的说明，每条没通过核对的引用/锚点带一个标记；任何
 * 界面路径都不得因为核对结果隐藏或清空回答正文。
 *
 * 线上形状（后端全部 additive、空则不序列化，缺席一律按「通过」读）：
 *   · `Citation.verification` / `AnswerAnchor.verification` ∈ changed / source_gone / unverifiable；
 *   · `AskResponse.citation_check`（`outcome`、`checked`、`failed` 与三类计数），只在 `failed > 0` 时出现；
 *   · 公开页 `PublicReference.verification`、`PublicTurn.citation_check`（回答时刻的快照）。
 *
 * 带标记的引用：卡片**留在列表里并保留摘录**（删掉它会在正文里留下一个裸 `[k]`），显示
 * 原因行，**不渲染**打开原文 / 图谱 / Knowhow / 图片 / 笔记本 / 外链这些入口（后端对
 * 带标记引用的下钻端点返回 404，留一颗按钮就是留一颗必然失败的按钮）；正文里它的行内
 * 标记换成弱化样式，原因进可访问名称；它名下的内联图片跳过。
 *
 * 文案、标签与判定集中在这里，笔记本/全局窗口（AnswerView + 引用卡）、正文标记
 * （answer-markdown）与公开页 `/c/{token}` 共用，不各写一份。
 */

import { label } from "./vocabulary.ts";

export type CitationVerification = "changed" | "source_gone" | "unverifiable";

/** `AskResponse.citation_check` / `PublicTurn.citation_check` 的镜像。 */
export type CitationCheckSummary = {
  outcome: string;
  checked: number;
  failed: number;
  changed: number;
  source_gone: number;
  unverifiable: number;
};

/** 卡片原因行与行内标记可访问名称用的标签。 */
export const CITATION_VERIFICATION_LABELS: Record<CitationVerification, string> = {
  changed: "原文已改动",
  source_gone: "资料已删除",
  unverifiable: "无法核对",
};

/** 卡片原因行下面那句解释：摘录是回答时的内容，为什么打不开原文。 */
const CITATION_VERIFICATION_EXPLANATIONS: Record<CitationVerification, string> = {
  changed: "这段原文在回答生成过程中被修改过，下面是回答时引用的摘录。",
  source_gone: "这份资料已被删除，下面是回答时引用的摘录。",
  unverifiable: "没能确认这条引用与原文一致，下面是回答时引用的摘录。",
};

const REASON_ORDER: readonly CitationVerification[] = ["changed", "source_gone", "unverifiable"];

type VerifiableLike = { verification?: string | null };

/**
 * 一个取值是否表示「没通过核对」。缺席 / 空串 = 通过（契约：absent 即 passed）。
 * 非空但不认识的取值按**没通过**读（显示「无法核对」）：宁可少一个入口，也不给一条
 * 后端明说有问题的引用留下打开原文的按钮。
 */
export function verificationOf(value: VerifiableLike | null | undefined): string {
  const raw = value?.verification;
  return typeof raw === "string" ? raw : "";
}

/** 一条引用（anchor 优先、citation 兜底，与卡片其余字段同序）的核对结果；通过则空串。 */
export function referenceVerification(reference: {
  anchor?: VerifiableLike;
  citation?: VerifiableLike;
}): string {
  return verificationOf(reference.anchor) || verificationOf(reference.citation);
}

export function verificationLabel(verification: string): string {
  return label(CITATION_VERIFICATION_LABELS, verification, "无法核对");
}

export function verificationExplanation(verification: string): string {
  return label(
    CITATION_VERIFICATION_EXPLANATIONS,
    verification,
    CITATION_VERIFICATION_EXPLANATIONS.unverifiable,
  );
}

/** 行内标记的可访问名称：先念可见编号（label-in-name），再念原因。 */
export function verificationMarkerName(displayLabel: string, verification: string): string {
  return `${displayLabel} 未通过核对：${verificationLabel(verification)}`;
}

function count(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? Math.floor(value) : 0;
}

/** 说明要不要出现：只认 `failed > 0`（后端也只在那时下发，这里再守一次形状）。 */
export function hasFailedCitationCheck(
  check: CitationCheckSummary | null | undefined,
): check is CitationCheckSummary {
  return Boolean(check) && count(check!.failed) > 0;
}

/**
 * 原因与条数，例如「2 条原文已改动、1 条资料已删除」。只列条数 > 0 的原因；三类计数
 * 全缺（旧形状或未知原因）时退回总数，绝不留一个空冒号。
 */
export function citationCheckReasonText(check: CitationCheckSummary): string {
  const parts = REASON_ORDER
    .filter((reason) => count(check[reason]) > 0)
    .map((reason) => `${count(check[reason])} 条${CITATION_VERIFICATION_LABELS[reason]}`);
  if (parts.length > 0) return parts.join("、");
  return `共 ${count(check.failed)} 条`;
}

/**
 * 答案下方那一句说明。`tense: "snapshot"` 用于公开页——那是回答时刻的快照，说的是
 * 当时的核对结果，不是此刻。
 */
export function citationCheckNotice(
  check: CitationCheckSummary,
  tense: "live" | "snapshot" = "live",
): string {
  const lead = tense === "snapshot" ? "回答生成时，有部分引用未通过核对" : "本次回答有部分引用未通过核对";
  return `${lead}：${citationCheckReasonText(check)}。回答内容照常保留，带标记的引用可点开查看原因。`;
}
