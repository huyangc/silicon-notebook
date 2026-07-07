// answer-citations.ts
//
// remark 插件：把答案文本里的 [k\d+] 和数字引用标记替换为 `cite:<key>` 链接节点，
// 供 AnswerMarkdown 的自定义 <a> 组件渲染成可点击的引用徽章。
//
// ⚠️ 曾踩坑（PR #51 → 本次修复）：unified 的 attacher 必须是「接收 options →
// 返回 transformer」两层结构。早期实现写成 `(refsByKey) => () => (tree) => {…}`
// 多了一层 `() =>`；以 `[remarkCitations, refsByKey]` 形式被 react-markdown 调用时，
// unified 把内层 `(tree) => {…}` 当成「transformer 的返回值（替换后的树）」，
// 真实 mdast 被丢弃 → mdast→hast 产出空 → **整段答案空白**。
// 正确形态是 `(refsByKey) => (tree) => {…}`。
import { visit, SKIP } from "unist-util-visit";
import type { Root, Text, Link, PhrasingContent } from "mdast";
import type { AnswerReference } from "./answer-formatting";

/**
 * 创建 remark 插件（attacher）：将 text 节点中的 [k\d+] / [1, 2] 标记替换为 `link` 节点，
 * `url` 设为 `cite:<key>`，供后续 <a> 组件识别为引用徽章。
 *
 * @param refsByKey key→AnswerReference 的映射，用于取显示编号；key 可以是 anchor key
 *                  (`k1`) 或显示编号 (`1`)。未命中的 key 原样保留为文本节点。
 */
export function remarkCitations(refsByKey: Record<string, AnswerReference>) {
  return (tree: Root) => {
    visit(tree, "text", (node: Text, index, parent) => {
      if (!parent || index === undefined) return;
      // 复合引用统一:一到多个 k?\d+ 以逗号分隔。[k1] / [1] / [k6, k10] / [1, 2] 皆匹配。
      const pattern = /\[((?:k?\d+\s*,\s*)*k?\d+)\]/g;
      const text = node.value;
      if (!pattern.test(text)) return;

      // 重置 lastIndex（test() 会改变它）
      pattern.lastIndex = 0;

      const newChildren: PhrasingContent[] = [];
      let last = 0;
      let match: RegExpExecArray | null;

      while ((match = pattern.exec(text)) !== null) {
        const token = match[1];
        // 逗号分隔的复合引用(k 前缀或纯数字皆可)统一按逗号拆;单 key 拆出单元素。
        const keys = token.split(",").map((part) => part.trim()).filter(Boolean);
        const refs = keys.map((key) => refsByKey[key]);

        // 前面的纯文本
        if (match.index > last) {
          newChildren.push({ type: "text", value: text.slice(last, match.index) });
        }

        if (refs.length > 0 && refs.every(Boolean)) {
          // 替换为 link 节点，url = "cite:<key>"
          keys.forEach((key, keyIndex) => {
            const ref = refs[keyIndex]!;
            if (keyIndex > 0) {
              newChildren.push({ type: "text", value: ", " });
            }
            const linkNode: Link = {
              type: "link",
              url: `cite:${key}`,
              children: [{ type: "text", value: ref.displayLabel }],
            };
            newChildren.push(linkNode);
          });
        } else {
          // key 不在引用映射里 → 原样保留文本
          newChildren.push({ type: "text", value: match[0] });
        }

        last = match.index + match[0].length;
      }

      // 尾部剩余文本
      if (last < text.length) {
        newChildren.push({ type: "text", value: text.slice(last) });
      }

      if (newChildren.length > 0) {
        parent.children.splice(index, 1, ...newChildren);
        // 跳过新插入的节点，从其后继续遍历（避免重复访问）。
        return [SKIP, index + newChildren.length];
      }
    });
  };
}
