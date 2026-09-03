// inference-list-markers.ts
//
// 真机踩过(2026-09-03,PR #669 之后本地真机核实):模型偶尔把推断/通识标记写在列表
// 序号**前面**——「（推断）1. 世界模型…」「（推断）2. …」，行间只有单换行。Markdown
// 不认「（推断）1.」是列表项起点,`.answer-markdown p` 也没有 `pre-wrap`,于是七条本该
// 分行的内容渲染成一整段连在一起的文字;标签本身能切出来(换行算句界),但可读性没了。
//
// 规格 docs/superpowers/specs/2026-09-03-ask-understanding-echo-and-mcp-clarification-design_zh.md
// 的 T2-d 把这个整成两层修:① prompt 侧治源头(要求模型把标记写在序号之后);
// ② 本文件是前端兜底,覆盖历史回答与 prompt 侧未生效的个例。
//
// 承诺:本函数只**交换标记与列表语法两个 token 的位置**,不新增、不删除、不改写任何
// 字符——正文、原有的空白(含制表符)全部原样保留,各自跟着自己后面的内容走;未命中的行
// 原样返回,一个字符都不经过本函数。
//
// 不碰的区域(与同管线邻居 `math-markdown.ts` 的围栏口径一致):
//   * 围栏代码块(``` / ~~~)内的行——模型在代码块里逐字引用「（推断）1. …」当反例时,
//     改了就等于把反例改成正例。围栏状态在本函数内自己维护,不与 math 归一共享。
//   * 4 空格及以上缩进的行——顶层它是缩进代码块。代价是列表项内部用 4 空格缩进的子列表
//     (那里 4 空格是子列表而不是代码块)不归一,`> ` 引用块前缀也不归一;两者都是
//     「保持现状(该行不成列表)」而不是「改坏」,登记为已知覆盖缺口,不为它们维护容器栈。
//
// 用法:渲染前串在 `normalizeMathMarkdown` 之后调用——`normalizeMathMarkdown` 已经把
// `\r\n`/`\r` 归一成 `\n`,本函数按 `\n` 逐行处理,不单独处理 `\r\n`。
//
// 四种标记字面量与 `answer-inference.ts` 的 `MARKERS`(L0 规则 2 的三种拼写 +
// 报告规则 4 的【通识】)保持一致,但两个模块不共享常量——这里只需要字面量列表本身
// 拼进一个正则,`answer-inference.ts` 的 `MarkerSpec[]` 还带 className,没有共同的
// 更小抽象值得为四个字符串新增一处间接层。改其中一种拼写时两处都要改,已知耦合。
const INFERENCE_MARKER_LITERALS = ["（推断）", "(推断)", "Likely,", "【通识】"];

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// 行首(允许至多 3 个空格缩进)紧跟四种标记字面量之一,标记后可有若干空格/制表符,
// 再紧接列表语法(`\d{1,9}[.)]` 或 `-`/`*`/`+`)或 ATX 标题语法(`#`×1–6),其后须有至少
// 一个空格或制表符才算进入正文(CommonMark 两者都接受)——不满足这条时说明标记后面根本
// 不是列表项/标题(例如「（推断）1个方案」「（推断）#1 方案」),原样放过,交给渲染前串联的
// `remarkAnswerInference` 按句首标记处理。标记挡在 `##` 前面时整行退化成普通段落,与
// 列表是同一类缺陷(codex #670 R3 P2)。
const INFERENCE_LIST_MARKER_LINE = new RegExp(
  `^( {0,3})(${INFERENCE_MARKER_LITERALS.map(escapeRegExp).join("|")})([ \\t]*)(\\d{1,9}[.)]|[-*+]|#{1,6})([ \\t]+)(.*)$`,
);

// 围栏:至多 3 空格缩进后 3 个及以上的 ` 或 ~;围栏也可以直接开在列表项那一行
// (`- ```text` / `1. ```text`,codex #670 R2 P2),所以允许一个可选的列表语法前缀,
// 否则围栏内缩进的「（推断）1. …」会被改写、闭合围栏还会被当成新的开启。
// 反引号围栏的 info string 里不得再出现反引号(CommonMark:那不是围栏),否则会把后面的
// 整段都当成代码块跳过;波浪线围栏无此限制。闭合须同字符、长度不短于开启、其后只有空白;
// 闭合行允许的缩进随开启行的列表前缀宽度走(`10. ```text` 的闭合缩进 4 空格,
// codex #670 R3 P2),否则围栏状态永不清除、后文全部漏修。
const FENCE_OPEN = /^( {0,3}(?:(?:\d{1,9}[.)]|[-*+])[ \t]+ {0,3})?)(`{3,}|~{3,})(.*)$/;

interface FenceState {
  run: string;
  /** 开启行 run 之前的字符数(缩进 + 列表前缀),闭合行的缩进上限是它加 3。 */
  prefixWidth: number;
}

function opensFence(line: string): FenceState | null {
  const match = line.match(FENCE_OPEN);
  if (!match) return null;
  const [, prefix, run, info] = match;
  if (run[0] === "`" && info.includes("`")) return null;
  return { run, prefixWidth: prefix.length };
}

function closesFence(line: string, fence: FenceState): boolean {
  const match = line.match(/^( *)(`{3,}|~{3,})[ \t]*$/);
  if (!match) return false;
  const [, indent, run] = match;
  return (
    indent.length <= fence.prefixWidth + 3
    && run[0] === fence.run[0]
    && run.length >= fence.run.length
  );
}

/**
 * 把「标记在列表序号前」的行归一成「序号在前、标记在后」:
 * `（推断）1. 世界模型闭环。` -> `1. （推断） 世界模型闭环。`
 *
 * 已经是「序号在前」的行、段首无序号的行、句中出现标记字面量的行、4 空格缩进的
 * 代码块行、围栏代码块内的行,均原样返回。空输入原样返回。
 */
export function normalizeInferenceListMarkers(markdown: string): string {
  if (!markdown) return markdown;
  let fence: FenceState | null = null;
  return markdown
    .split("\n")
    .map((line) => {
      if (fence !== null) {
        if (closesFence(line, fence)) fence = null;
        return line;
      }
      const opening = opensFence(line);
      if (opening !== null) {
        fence = opening;
        return line;
      }
      const match = line.match(INFERENCE_LIST_MARKER_LINE);
      if (!match) return line;
      // 只交换标记与列表语法两个 token,两段原有空白各自跟着自己后面的内容走:
      // 「（推断）1. 内容」→「1. （推断）内容」,与模型写对时的形态逐字相同;
      // 「（推断）1.\t内容」→「1.\t（推断）内容」,制表符原样保留。不合成任何字符。
      const [, indent, marker, gapAfterMarker, listSyntax, gapAfterList, content] = match;
      return `${indent}${listSyntax}${gapAfterList}${marker}${gapAfterMarker}${content}`;
    })
    .join("\n");
}
