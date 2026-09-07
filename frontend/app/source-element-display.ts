// 来源详情「阅读视图」的呈现规则:决定每个 source_element 在详情窗里给用户看什么、
// 不看什么。纯函数,不碰 DOM。
//
// 背景:解析器给每个元素的 location_label 是「PDF p.1 block 3」「DOCX paragraph 12」
// 这种机器定位串——它是引用/证据的稳定坐标(answer-panel、kg-evidence-list 仍照用),
// 但对正在读文档的用户没有信息量:格式名与顶部的来源类型标签重复,block 序号只是
// 解析顺序。详情窗因此不再逐元素显示它,改成:
//
//   1. 分节分隔线——从 metadata 取页码/幻灯片号/工作表名,只在相邻元素跨节时插一条;
//   2. 标题元素按真正的标题渲染(h3/h4/h5),不再是「一张卡片 + 标题 tag」;
//   3. 类型 tag 只留给正文形状看不出来的类型(演讲者备注等);段落/标题/表格/公式/
//      图片这些一眼可辨的不再贴标签;
//   4. 表格行保留行号——对 CSV/XLSX 来源,行号正是用户要的定位。
//
// metadata 键名的真源是 backend/app/services/parsers.py 各 _element 调用:
//   page_number(pdf / pymupdf4llm / mineru)、slide_number(python-pptx / pptx)、
//   sheet(xlsx / xls)、row_index(csv / xlsx / xls / docx 表格)、table_index(docx)、
//   heading_level(markdown / mammoth)、text_level(mineru)。
import { ELEMENT_TYPE, label } from "./vocabulary.ts";

type ElementLike = {
  element_type: string;
  metadata?: Record<string, unknown> | null;
};

export type ElementSection = { key: string; label: string };

function positiveInt(value: unknown): number | null {
  if (typeof value === "number" && Number.isInteger(value) && value > 0) return value;
  if (typeof value === "string" && /^\d+$/.test(value)) {
    const parsed = Number(value);
    return parsed > 0 ? parsed : null;
  }
  return null;
}

function nonEmptyString(value: unknown): string | null {
  return typeof value === "string" && value.trim() !== "" ? value.trim() : null;
}

/** 元素所属的「节」:页 / 幻灯片 / 工作表。没有任何定位元数据时返回 null。 */
export function elementSection(element: ElementLike): ElementSection | null {
  const metadata = element.metadata ?? {};
  const page = positiveInt(metadata.page_number);
  if (page !== null) return { key: `page:${page}`, label: `第 ${page} 页` };
  const slide = positiveInt(metadata.slide_number);
  if (slide !== null) return { key: `slide:${slide}`, label: `幻灯片 ${slide}` };
  const sheet = nonEmptyString(metadata.sheet);
  if (sheet !== null) return { key: `sheet:${sheet}`, label: `工作表 ${sheet}` };
  return null;
}

export type SectionedElement<T extends ElementLike> = { element: T; divider: string | null };

/**
 * 给元素序列标出分隔线:某元素所属的节与前一个元素不同时,divider 是这一节的标签。
 * 已加载窗口的第一个元素若有节,也带 divider——翻页后用户需要知道自己在第几页,
 * 而窗口之前的元素不在内存里,无从比较。
 */
export function withSectionDividers<T extends ElementLike>(elements: readonly T[]): SectionedElement<T>[] {
  let previousKey: string | null = null;
  return elements.map((element) => {
    const section = elementSection(element);
    const key = section?.key ?? null;
    const divider = section !== null && key !== previousKey ? section.label : null;
    previousKey = key;
    return { element, divider };
  });
}

// 从正文形状就能看出类型的元素:不贴类型标签。slide_text 靠幻灯片分隔线定位,
// table_row 靠行号定位,同样不需要标签。
const SELF_EVIDENT_TYPES = new Set([
  "heading",
  "paragraph",
  "page_text",
  "table",
  "table_row",
  "formula",
  "code_block",
  "list_item",
  "image",
  "slide_text",
]);

/** 需要显示的类型标签文案;一眼可辨或词汇表未收录的类型返回 null。 */
export function elementTypeTag(elementType: string): string | null {
  if (SELF_EVIDENT_TYPES.has(elementType)) return null;
  if (!Object.hasOwn(ELEMENT_TYPE, elementType)) return null;
  return label(ELEMENT_TYPE, elementType, "");
}

/** 标题元素渲染成哪一级 h 标签。详情窗标题是 h1、「来源指南」是 h3,所以文档一级标题从 h3 起。 */
export function elementHeadingLevel(element: ElementLike): 3 | 4 | 5 {
  const metadata = element.metadata ?? {};
  const level = positiveInt(metadata.heading_level) ?? positiveInt(metadata.text_level) ?? 1;
  if (level <= 1) return 3;
  if (level === 2) return 4;
  return 5;
}

/** 元素旁的定位小字。目前只有表格行需要:行号是 CSV/XLSX 来源里用户真正要的坐标。 */
export function elementLocationNote(element: ElementLike): string | null {
  if (element.element_type !== "table_row") return null;
  const metadata = element.metadata ?? {};
  const row = positiveInt(metadata.row_index);
  if (row === null) return null;
  const table = positiveInt(metadata.table_index);
  return table !== null ? `表 ${table} 第 ${row} 行` : `第 ${row} 行`;
}
