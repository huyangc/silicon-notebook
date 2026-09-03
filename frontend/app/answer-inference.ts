// answer-inference.ts
//
// remark 插件：把答案文本里**句首/段首**的推断标记（`（推断）`/`(推断)`/`Likely,`，与
// `prompts.py` L0 规则 2 的三种拼写一致）和通识标记（`【通识】`，报告规则 4）切成独立
// 行内节点，供 AnswerMarkdown / ReportMarkdown 与两个公开分享页渲染成弱化色小标签
// （样式见 globals.css `.answer-inference` / `.answer-general-knowledge`）。只切节点，不改
// 文本内容——标记本身仍逐字留在 DOM 文本里，`copyAnswer` 走 `renderTextWithReferenceNumbers`，
// 与渲染树无关，复制结果不受影响。
//
// ⚠️ attacher 形态（同 answer-citations.ts 文件头的踩坑注释）：unified 的插件必须是
// 「attacher(options) → transformer(tree)」两层。这里没有 options，但仍要保留两层——
// 写成单层 `(tree) => {...}` 直接塞进 remarkPlugins 数组，会被 unified 当成「attacher
// 本身」调用一次，返回值（undefined）才被当成 transformer，插件整体失效、答案照原样
// 不做任何切分。正确形态是 `() => (tree) => {...}`。
//
// 遍历不用 unist-util-visit：句首判据需要整条祖先链（标记嵌在 `**…**`/链接里时，
// 「本容器起点」不等于段首，要继续往外看容器前面的文本），visit 只给直接 parent。
import type { PhrasingContent, Root, RootContent, Text } from "mdast";

// mdast 的 `Parent` 是接口而非联合成员,当不了 `RootContent` 上的类型谓词
// (TS2677:谓词类型必须可赋给参数类型);这里只需要「有 children 的节点」这个结构。
interface NodeWithChildren {
  type: string;
  children: RootContent[];
}

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

// 行内容器：它们的起点不是段首，判据要继续往外看容器前面的内容。其它有 children 的
// 节点（paragraph/heading/listItem/tableCell/blockquote/root……）都是块级容器，起点即段首。
const INLINE_CONTAINERS = new Set(["strong", "emphasis", "delete", "link", "linkReference"]);

const MARKER_NODE_TYPE = "answerInferenceMarker";

/** `text[0..end)` 去掉尾部空格/制表符后是否以句界结尾；只剩空白时返回 undefined（交给调用方继续回看）。 */
function trailingBoundary(text: string, end: number): boolean | undefined {
  let i = end;
  while (i > 0 && (text[i - 1] === " " || text[i - 1] === "\t")) {
    i -= 1;
  }
  if (i === 0) return undefined;
  const prev = text[i - 1];
  return prev === "\n" || BOUNDARY_PUNCTUATION.has(prev);
}

function hasChildren(node: unknown): node is NodeWithChildren {
  return Array.isArray((node as NodeWithChildren).children);
}

/**
 * 从 `nodes[end - 1]` 往前回看，判定紧邻的前文是否以句界结尾。
 * 纯空白的 text 继续往前；软换行算段首；有 children 的节点深入它的**末尾**内容
 * （`**结论。**（推断）` 的句号在 strong 里面）；其它叶子（行内代码/行内公式/图片/
 * 已切出的标记……）都说明标记在句中。整段回看完仍没答案时返回 undefined。
 */
function boundaryBeforeIndex(nodes: RootContent[], end: number): boolean | undefined {
  for (let j = end - 1; j >= 0; j -= 1) {
    const sibling = nodes[j];
    if (sibling.type === "break") return true;
    if (sibling.type === "text") {
      const trailing = trailingBoundary(sibling.value, sibling.value.length);
      if (trailing !== undefined) return trailing;
      continue;
    }
    if (hasChildren(sibling)) {
      const inner = boundaryBeforeIndex(sibling.children, sibling.children.length);
      if (inner !== undefined) return inner;
      continue; // 空容器，继续往前看
    }
    return false;
  }
  return undefined;
}

interface ChainLink {
  parent: NodeWithChildren;
  index: number;
}

/**
 * 标记是否处在句首/段首。先看本 text 节点内标记之前的内容；本节点起点不等于段首
 * （`[k1]（推断）`——remarkCitations 先跑、在 chip 处切断了 text 节点；`**重点**（推断）`；
 * `参见 **（推断）这一节**`——标记落在 strong 里的 index 0），所以沿祖先链逐层回看
 * 前面的兄弟：行内容器的起点继续往外看，块级容器的起点才是段首。
 */
function isAtSentenceOrParagraphStart(
  text: string,
  matchIndex: number,
  chain: ChainLink[],
): boolean {
  const withinNode = trailingBoundary(text, matchIndex);
  if (withinNode !== undefined) return withinNode;
  for (let level = chain.length - 1; level >= 0; level -= 1) {
    const { parent, index } = chain[level];
    const before = boundaryBeforeIndex(parent.children, index);
    if (before !== undefined) return before;
    if (!INLINE_CONTAINERS.has(parent.type)) return true; // 块级容器起点 = 段首
  }
  return true;
}

// mdast 没有这个节点类型，靠 data.hName/hProperties 让 mdast-util-to-hast 的
// unknown-node 兜底把它渲染成 <span class="...">（见该库 lib/index.js 文件头注释：
// 「unknown 节点若带 data.hName 则按该名生成元素，children 照常映射」）。
interface InferenceMarkerNode {
  type: typeof MARKER_NODE_TYPE;
  data: {
    hName: "span";
    hProperties: { className: [MarkerClassName] };
  };
  children: [Text];
}

function buildMarkerNode(literal: string, className: MarkerClassName): InferenceMarkerNode {
  return {
    type: MARKER_NODE_TYPE,
    data: { hName: "span", hProperties: { className: [className] } },
    children: [{ type: "text", value: literal }],
  };
}

/** 把一个 text 节点按句首/段首标记切开；没有命中时返回 undefined（节点原样保留）。 */
function splitTextNode(node: Text, chain: ChainLink[]): PhrasingContent[] | undefined {
  const text = node.value;
  MARKER_PATTERN.lastIndex = 0;
  if (!MARKER_PATTERN.test(text)) return undefined;
  MARKER_PATTERN.lastIndex = 0;

  const newChildren: PhrasingContent[] = [];
  let last = 0;
  let match: RegExpExecArray | null;

  while ((match = MARKER_PATTERN.exec(text)) !== null) {
    if (!isAtSentenceOrParagraphStart(text, match.index, chain)) {
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

  if (newChildren.length === 0) return undefined;
  if (last < text.length) {
    newChildren.push({ type: "text", value: text.slice(last) });
  }
  return newChildren;
}

function transformChildren(parent: NodeWithChildren, chain: ChainLink[]): void {
  for (let index = 0; index < parent.children.length; index += 1) {
    const node = parent.children[index];
    const link: ChainLink = { parent, index };
    if (node.type === "text") {
      const replacement = splitTextNode(node, [...chain, link]);
      if (replacement) {
        parent.children.splice(index, 1, ...replacement);
        index += replacement.length - 1; // 切出的标记节点不再进入本循环
      }
      continue;
    }
    // 已切出的标记节点不再深入（否则它体内的「（推断）」会被再切一层）。
    if ((node.type as string) === MARKER_NODE_TYPE) continue;
    if (hasChildren(node)) transformChildren(node, [...chain, link]);
  }
}

/**
 * 创建 remark 插件（attacher，无 options）：把 text 节点里句首/段首的推断/通识标记
 * 切成独立节点。用法同 remarkGfmPlugin/remarkMath——裸引用放进 remarkPlugins 数组即可，
 * 不需要 `[remarkAnswerInference, options]` 元组形式。
 */
export function remarkAnswerInference() {
  return (tree: Root) => {
    transformChildren(tree, []);
  };
}
