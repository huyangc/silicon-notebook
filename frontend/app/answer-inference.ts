// answer-inference.ts
//
// remark 插件：把答案文本里**句首/段首**的推断标记（`（推断）`/`(推断)`/`Likely,`，与
// `prompts.py` L0 规则 2 的三种拼写一致）和通识标记（`【通识】`，报告规则 4）切成独立
// 行内节点，供 AnswerMarkdown / ReportMarkdown 渲染成弱化色小标签（样式见 globals.css
// `.answer-inference` / `.answer-general-knowledge`）。只切节点，不改文本内容——标记本身
// 仍逐字留在 DOM 文本里，`copyAnswer` 走 `renderTextWithReferenceNumbers`，与渲染树无关，
// 复制结果不受影响。
//
// ⚠️ attacher 形态（同 answer-citations.ts 文件头的踩坑注释）：unified 的插件必须是
// 「attacher(options) → transformer(tree)」两层。这里没有 options，但仍要保留两层——
// 写成单层 `(tree) => {...}` 直接塞进 remarkPlugins 数组，会被 unified 当成「attacher
// 本身」调用一次，返回值（undefined）才被当成 transformer，插件整体失效、答案照原样
// 不做任何切分。正确形态是 `() => (tree) => {...}`。
import { visit, SKIP } from "unist-util-visit";
import type { Parent, PhrasingContent, Root, Text } from "mdast";

type MarkerClassName = "answer-inference" | "answer-general-knowledge";

interface MarkerSpec {
  literal: string;
  className: MarkerClassName;
}

// 三种拼写与 L0 规则 2（`prompts.py:332-336`）一致；【通识】对应报告规则 4。
const MARKERS: MarkerSpec[] = [
  { literal: "（推断）", className: "answer-inference" },
  { literal: "(推断)", className: "answer-inference" },
  { literal: "Likely,", className: "answer-inference" },
  { literal: "【通识】", className: "answer-general-knowledge" },
];

const CLASS_NAME_BY_LITERAL: Record<string, MarkerClassName> = Object.fromEntries(
  MARKERS.map((marker) => [marker.literal, marker.className]),
);

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

const MARKER_PATTERN = new RegExp(
  MARKERS.map((marker) => escapeRegExp(marker.literal)).join("|"),
  "g",
);

// 句首/段首判据：标记前面（可隔空格/制表符）是段首、换行，或 。；！？.;!? 之一。
// 半角 `?` 必须在内：`Likely,` 是唯一的纯英文拼写，而英文句子最常以 `?` 结尾。
// 句中出现的普通词「推断」不受影响——它压根不会命中上面的标记字面量（都带括号/逗号）。
const BOUNDARY_PUNCTUATION = new Set(["。", "；", "！", "？", ".", ";", "!", "?"]);

/** `text[0..end)` 去掉尾部空格/制表符后是否以句界结尾；空串返回 undefined（交给调用方继续回看）。 */
function trailingBoundary(text: string, end: number): boolean | undefined {
  let i = end;
  while (i > 0 && (text[i - 1] === " " || text[i - 1] === "\t")) {
    i -= 1;
  }
  if (i === 0) return undefined;
  const prev = text[i - 1];
  return prev === "\n" || BOUNDARY_PUNCTUATION.has(prev);
}

function hasSentenceOrParagraphBoundaryBefore(
  text: string,
  matchIndex: number,
  parent: Parent,
  index: number,
): boolean {
  const withinNode = trailingBoundary(text, matchIndex);
  if (withinNode !== undefined) return withinNode;
  // 本 text 节点起点 ≠ 段首：`[k1]（推断）`（remarkCitations 先跑，在 chip 处切断了
  // text 节点）、`**重点**（推断）` 这类句中位置的标记都落在新节点的 index 0。回看前面
  // 的兄弟节点：纯空白的 text 继续往前看；以句界结尾的 text 或软换行算段首；其它任何
  // phrasing 节点（链接/强调/行内代码……）都说明标记在句中。
  for (let j = index - 1; j >= 0; j -= 1) {
    const sibling = parent.children[j];
    if (sibling.type === "break") return true;
    if (sibling.type !== "text") return false;
    const trailing = trailingBoundary(sibling.value, sibling.value.length);
    if (trailing !== undefined) return trailing;
  }
  return true; // 段首
}

// mdast 没有这个节点类型，靠 data.hName/hProperties 让 mdast-util-to-hast 的
// unknown-node 兜底把它渲染成 <span class="...">（见该库 lib/index.js 文件头注释：
// 「unknown 节点若带 data.hName 则按该名生成元素，children 照常映射」）。
interface InferenceMarkerNode {
  type: "answerInferenceMarker";
  data: {
    hName: "span";
    hProperties: { className: [MarkerClassName] };
  };
  children: [Text];
}

function buildMarkerNode(literal: string, className: MarkerClassName): InferenceMarkerNode {
  return {
    type: "answerInferenceMarker",
    data: { hName: "span", hProperties: { className: [className] } },
    children: [{ type: "text", value: literal }],
  };
}

/**
 * 创建 remark 插件（attacher，无 options）：把 text 节点里句首/段首的推断/通识标记
 * 切成独立节点。用法同 remarkGfmPlugin/remarkMath——裸引用放进 remarkPlugins 数组即可，
 * 不需要 `[remarkAnswerInference, options]` 元组形式。
 */
export function remarkAnswerInference() {
  return (tree: Root) => {
    visit(tree, "text", (node: Text, index, parent) => {
      if (!parent || index === undefined) return;
      const text = node.value;
      MARKER_PATTERN.lastIndex = 0;
      if (!MARKER_PATTERN.test(text)) return;
      MARKER_PATTERN.lastIndex = 0;

      const newChildren: PhrasingContent[] = [];
      let last = 0;
      let match: RegExpExecArray | null;

      while ((match = MARKER_PATTERN.exec(text)) !== null) {
        if (!hasSentenceOrParagraphBoundaryBefore(text, match.index, parent, index)) {
          // 句中出现的标记字面量（理论上不该出现，但不是我们的判断权限）原样留在文本里，
          // 不切节点、不动 `last`——留给后续的前缀切片或结尾切片一并带走。
          continue;
        }

        if (match.index > last) {
          newChildren.push({ type: "text", value: text.slice(last, match.index) });
        }

        const className = CLASS_NAME_BY_LITERAL[match[0]];
        newChildren.push(buildMarkerNode(match[0], className) as unknown as PhrasingContent);
        last = match.index + match[0].length;
      }

      if (newChildren.length === 0) return; // 没有命中句首/段首的标记，节点原样保留

      if (last < text.length) {
        newChildren.push({ type: "text", value: text.slice(last) });
      }

      parent.children.splice(index, 1, ...newChildren);
      return [SKIP, index + newChildren.length];
    });
  };
}
