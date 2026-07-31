// 提问框里唯一的检索语法:英文双引号括起来的内容整体检索,不做分词。
//
// 这是后端 `backend/app/core/query_syntax.py` 的镜像,存在的理由只有一个:让用户在
// 按下发送**之前**就看到哪几段真的被识别成了精确短语。识别规则有边界(太短的、
// 引号太密的都不算),没有这面镜子,不被识别就是一次静默失败——用户以为自己下了约束,
// 检索侧却当普通词处理了。
//
// 两边靠 query-syntax.test.mjs / test_query_syntax.py 的同一批用例钉住;改任一侧
// 的规则都要同时改另一侧。

// 与后端 MAX_QUOTED_PHRASES 一致:超过这个数量就整段不识别(JSON 之类的机器文本里,
// 引号是标点不是约束)。
export const MAX_QUOTED_PHRASES = 4;
// 与后端 MIN_LEXICAL_TERM_CHARS 一致:SQLite 的三字符索引更短的根本索引不到。
export const MIN_PHRASE_CHARS = 3;
// 与后端 MAX_EXACT_PHRASE_CHARS 一致。
export const MAX_PHRASE_CHARS = 256;

// 只认英文半角双引号,且不跨行——与后端同一条正则的理由见那边的注释。
const QUOTED = /"([^"\n]+)"/g;

export type QuotedPhraseAnalysis = {
  // 真正会作为完整短语参与检索的那几段(按出现顺序,忽略大小写去重)。
  phrases: string[];
  // 文本里成对引号的总数——用来区分「没写引号」和「写了但没被识别」。
  spans: number;
  // spans 超过上限:整段语法不生效,按普通检索处理。
  tooMany: boolean;
};

export function analyzeQuotedPhrases(text: string): QuotedPhraseAnalysis {
  const source = text ?? "";
  const matches = [...source.matchAll(QUOTED)];
  if (matches.length === 0 || matches.length > MAX_QUOTED_PHRASES) {
    return { phrases: [], spans: matches.length, tooMany: matches.length > MAX_QUOTED_PHRASES };
  }
  const phrases: string[] = [];
  const seen = new Set<string>();
  for (const match of matches) {
    const value = match[1].split(/\s+/).filter(Boolean).join(" ");
    // 按**码点**计长,不用 `value.length`:JS 的 length 数的是 UTF-16 code unit,
    // 后端 Python 的 len 数的是码点,一段非 BMP 文字(CJK 扩展 B 等)会被后端接受
    // 却在这里判为超长——回执与真实检索报出不同的短语集合。(codex #410 round-2 P3)
    const length = [...value].length;
    if (length < MIN_PHRASE_CHARS || length > MAX_PHRASE_CHARS) continue;
    // `toLowerCase()` 与后端的 `.lower()` 对齐(后端刻意不用 casefold,理由见那边)。
    const folded = value.toLowerCase();
    if (seen.has(folded)) continue;
    seen.add(folded);
    phrases.push(value);
  }
  return { phrases, spans: matches.length, tooMany: false };
}

export function quotedPhrases(text: string): string[] {
  return analyzeQuotedPhrases(text).phrases;
}

// 输入框下方那行提示:没写引号时什么都不显示,写了才解释结果。
// 返回 null = 不渲染。
export function quotedPhraseHint(text: string): string | null {
  if (!(text ?? "").includes("\"")) return null;
  const { phrases, tooMany } = analyzeQuotedPhrases(text);
  if (phrases.length > 0) {
    return `精确短语：${phrases.map((phrase) => `「${phrase}」`).join("")}（整体检索，不拆词）`;
  }
  if (tooMany) {
    return `引号超过 ${MAX_QUOTED_PHRASES} 处，本次按普通检索处理`;
  }
  return `未识别到精确短语：需成对的英文双引号，且引号内至少 ${MIN_PHRASE_CHARS} 个字`;
}
