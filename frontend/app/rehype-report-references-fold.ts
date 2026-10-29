import type { Element, ElementContent, Root, Text } from "hast";

/**
 * 把深度报告正文里的「## 参考文献」一节折叠成默认收起的 `<details>`。
 *
 * 引用已经改成在正文标记旁弹出引用卡（与问答同一张卡），参考文献整节平铺在正文里
 * 只会把读者往下推；但它仍是报告的一部分（导出、打印、核对都要），所以是**收起**而
 * 不是删掉。范围：从这个 h2 起，到下一个 h1/h2 之前的全部兄弟节点。
 *
 * 认的是**最后一个**标题文字恰为「参考文献」的 h2——与生成器写下的 `## 参考文献`
 * 逐字相同（后端 report_followup.py 的 `_REFERENCES_HEADING` 同一口径）。正文里
 * 「参考文献中的方法比较」这类章节不是它，前面若有同名章节也不抢先折叠。
 *
 * h2 本身放进 `<summary>`：收起时标题仍在、仍是目录（ReportDocument 读渲染后的
 * 标题）能定位到的可见元素，id（`report-references`）也照旧挂在它上面。
 *
 * 只是渲染期变换：存下来的 `content_md`、下载与复制都逐字不变。站内报告与报告公开
 * 分享页共用这一份，两处折叠口径不可能分叉。
 */
export const REPORT_REFERENCES_FOLD_CLASS = "report-references-fold";

function textOf(node: ElementContent): string {
  if (node.type === "text") return (node as Text).value;
  if (node.type === "element") return node.children.map(textOf).join("");
  return "";
}

function isSectionHeading(node: ElementContent): node is Element {
  return node.type === "element" && (node.tagName === "h1" || node.tagName === "h2");
}

const REFERENCES_HEADING = "参考文献";

function isReferencesHeading(node: ElementContent): node is Element {
  return node.type === "element" && node.tagName === "h2" && textOf(node).trim() === REFERENCES_HEADING;
}

export function rehypeReportReferencesFold() {
  return (tree: Root) => {
    const children = tree.children as ElementContent[];
    const start = children.findLastIndex(isReferencesHeading);
    if (start < 0) return;
    let end = start + 1;
    while (end < children.length && !isSectionHeading(children[end])) end += 1;
    const heading = children[start] as Element;
    const fold: Element = {
      type: "element",
      tagName: "details",
      properties: { className: [REPORT_REFERENCES_FOLD_CLASS] },
      children: [
        { type: "element", tagName: "summary", properties: {}, children: [heading] },
        ...children.slice(start + 1, end),
      ],
    };
    children.splice(start, end - start, fold);
  };
}
