"use client";

// 公开分享页（`/c/{token}` 问答、`/r/{token}` 深度报告）的引用浮层。
//
// 点正文引用标记的行为与站内问答**同一个组件**：`CitationPopover`（同一套
// `.cite-popover` / `.cite-detail-*` 样式、同一套定位与点外/滚动/Esc 关闭）。分享页
// 只做一件事——把白名单投影里的引用整形成站内的 `AnswerReference`。
//
// 整形刻意只填投影里**有**的东西：没有 source_id / element_id / notebook_id / url，
// 所以卡片上「查看原文」「知识图谱」「打开笔记本」「打开链接」一个都出不来（宿主也
// 不传任何回调）；`notebookId` 传 null，卡片不取任何附图——分享页的附图走正文里的
// token 别名图片区，不经这张卡片。

import { CitationPopover } from "./citation-card";
import type { AnswerReference } from "./answer-formatting";
import type { CitationVerification } from "./citation-verification";
import { publicReferenceLocation } from "./public-report";

/** 两个公开页的引用投影的公共形状（会话那份是报告那份的超集）。 */
export type PublicPopoverReferenceT = {
  key: string;
  title: string;
  file_name: string;
  location: string;
  snippet: string;
  title_truncated?: boolean;
  snippet_truncated?: boolean;
  file_name_truncated?: boolean;
  is_memory?: boolean;
  is_image_reference?: boolean;
  is_external?: boolean;
  verification?: CitationVerification;
};

/** 点开的那条引用 + 被点的标记位置（浮层按它定位）。 */
export type PublicCitePopoverState = Readonly<{ key: string; rect: DOMRect }>;

const NO_NOTEBOOK_NAMES: Record<string, string> = {};

/** 白名单投影 → 站内引用卡吃的形状。 */
export function publicReferenceAsAnswerReference(
  reference: PublicPopoverReferenceT,
  displayLabel: string,
): AnswerReference {
  return {
    id: `public:${reference.key}`,
    displayLabel,
    citation: {
      label: reference.title || reference.file_name || "(未命名资料)",
      source_id: "",
      element_id: "",
      location_label: publicReferenceLocation(reference),
      // 引用本身是图片元素时，摘录是解析器生成的图注/描述，与站内一样不显示成正文
      // （没通过核对的除外：那时附图不插入，摘录是这条引用唯一剩下的内容）。
      quoted_span: reference.is_image_reference && !reference.verification ? "" : reference.snippet,
      source_file_name: reference.file_name,
      tier: reference.is_external ? "external" : undefined,
      verification: reference.verification,
    },
  };
}

/** 截断绝不静默：卡片上逐条说明哪一项被切成了前缀。 */
export function publicReferenceNotes(reference: PublicPopoverReferenceT): string[] {
  const notes: string[] = [];
  if (reference.title_truncated) notes.push("（标题过长，已截断）");
  if (reference.file_name_truncated) notes.push("（原始文件名过长，已截断）");
  const snippetShown = !reference.is_image_reference || Boolean(reference.verification);
  if (snippetShown && reference.snippet_truncated) notes.push("（摘录过长，已截断）");
  return notes;
}

export function PublicCitationPopover({
  reference,
  displayLabel,
  anchorRect,
  onClose,
}: {
  reference: PublicPopoverReferenceT;
  displayLabel: string;
  anchorRect: DOMRect;
  onClose: () => void;
}) {
  return (
    <CitationPopover
      reference={publicReferenceAsAnswerReference(reference, displayLabel)}
      notebookId={null}
      notebookNames={NO_NOTEBOOK_NAMES}
      anchorRect={anchorRect}
      onClose={onClose}
      notes={publicReferenceNotes(reference)}
    />
  );
}
