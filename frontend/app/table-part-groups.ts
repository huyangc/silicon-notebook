// 来源详情把超长表格拆成多个 table 元素(backend/app/services/parsers.py 的
// `table_part`/`table_parts`/`table_group` 三个 metadata 键,见 docs/product-and-api.md
// 「来源上传与解析」)——检索粒度的切分,不该在阅读视图里露出「同一张表被切成好几张
// 卡片、后面几张还各带一份重复表头」的痕迹。这个模块把 withSectionDividers 的输出
// 里连续的表格分段重新拼成一组,交给 page.tsx 渲染成一张卡片、一张 <table>。
//
// 只做分组与 HTML 拼接,不碰 DOM、不认识 React。

type ElementLike = {
  id: string;
  element_type: string;
  metadata?: Record<string, unknown> | null;
};

type Sectioned<T> = { element: T; divider: string | null };

export type TablePartGroup<T> =
  | { kind: "single"; element: T; divider: string | null }
  | { kind: "table_group"; elements: T[]; divider: string | null };

function tableGroupKey(item: ElementLike): string | null {
  if (item.element_type !== "table") return null;
  const value = (item.metadata ?? {}).table_group;
  return typeof value === "string" && value.trim() !== "" ? value : null;
}

function tablePartNumber(item: ElementLike): number | null {
  const value = (item.metadata ?? {}).table_part;
  return typeof value === "number" && Number.isInteger(value) && value > 0 ? value : null;
}

// 与单元素渲染路径(ElementBody 的 `if (html)`)同一条真值判断:没有 table_html
// 就没有东西可合并显示,退化为 single 交给那条路径处理(它会落到默认分支)。
function hasTableHtml(item: ElementLike): boolean {
  const value = (item.metadata ?? {}).table_html;
  return typeof value === "string" && value !== "";
}

function canJoinGroup(item: ElementLike): boolean {
  return tableGroupKey(item) !== null && tablePartNumber(item) !== null && hasTableHtml(item);
}

/**
 * 把连续的、属于同一张表(`table_group` 相同)、分段号连续递增、且都带 table_html
 * 的 table 元素合并成一组。分页窗口从中间的分段开始(组首不是 part 1)也按窗口内
 * 能连上的部分合并。只有 1 个元素的组退化为 single,交给现有单元素渲染路径。
 */
export function groupTableParts<T extends ElementLike>(items: readonly Sectioned<T>[]): TablePartGroup<T>[] {
  const groups: TablePartGroup<T>[] = [];
  let i = 0;
  while (i < items.length) {
    const head = items[i];
    if (!canJoinGroup(head.element)) {
      groups.push({ kind: "single", element: head.element, divider: head.divider });
      i += 1;
      continue;
    }
    const key = tableGroupKey(head.element);
    const run: T[] = [head.element];
    let expected = (tablePartNumber(head.element) as number) + 1;
    let j = i + 1;
    while (
      j < items.length &&
      canJoinGroup(items[j].element) &&
      tableGroupKey(items[j].element) === key &&
      tablePartNumber(items[j].element) === expected
    ) {
      run.push(items[j].element);
      expected += 1;
      j += 1;
    }
    groups.push(
      run.length > 1
        ? { kind: "table_group", elements: run, divider: head.divider }
        : { kind: "single", element: head.element, divider: head.divider },
    );
    i = j;
  }
  return groups;
}

const ALLOWED_TABLE_TAGS = new Set(["table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption"]);
const TAG_PATTERN = /<(\/?)([a-z][a-z0-9]*)([^>]*)>/gi;

// 白名单标签的属性**重新生成**而不是从原标签里删黑名单:输出里的每个属性都来自下面
// 这几个取值受限的匹配,原标签的其余字节一个不留。旧实现只删 `\son…=`,`<td x/onclick=…>`
// 这种以 `/` 分隔的写法(浏览器照样当属性解析)因此原样放行。
function rebuildTableTagAttributes(rest: string): string {
  let attrs = "";
  for (const name of ["colspan", "rowspan"]) {
    const match = new RegExp(`(?:^|[\\s/])${name}\\s*=\\s*["']?(\\d{1,4})(?![\\d])`, "i").exec(rest);
    if (match) attrs += ` ${name}="${match[1]}"`;
  }
  const align = /(?:^|[\s/])align\s*=\s*["']?(left|center|right|justify)(?![a-z])/i.exec(rest);
  if (align) attrs += ` align="${align[1].toLowerCase()}"`;
  return attrs;
}

// 只保留静态表格标记:白名单标签按上面的规则重建,其余标签换成空格,注释与
// script/style 整块丢弃,文本里散落的 `<`/`>` 转义——后续任何剥标签/拼接都拼不出新标签。
// 供单元素和合并表格两条渲染路径共用。
export function sanitizeTableHtml(html: string): string {
  const withoutBlocks = html
    .replace(/<!--[\s\S]*?(?:-->|$)/g, "")
    .replace(/<(script|style)\b[\s\S]*?(?:<\/\1\s*>|$)/gi, "");
  const escapeText = (text: string) => text.replace(/</g, "&lt;").replace(/>/g, "&gt;");
  let output = "";
  let last = 0;
  for (const match of withoutBlocks.matchAll(TAG_PATTERN)) {
    output += escapeText(withoutBlocks.slice(last, match.index));
    last = match.index + match[0].length;
    const [, closing, rawName, rest] = match;
    const name = rawName.toLowerCase();
    // Stripped tags become a single space, not "": mammoth wraps each cell paragraph
    // in <p>, so dropping them outright glued a two-paragraph cell's "a" and "b" into
    // "ab". HTML collapses the extra whitespace on render, so a plain space is enough.
    if (!ALLOWED_TABLE_TAGS.has(name)) output += " ";
    else if (closing) output += `</${name}>`;
    else output += `<${name}${rebuildTableTagAttributes(rest)}>`;
  }
  return output + escapeText(withoutBlocks.slice(last));
}

function escapeAttr(value: string): string {
  return value.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// 剥掉外层 <table ...>/</table>、整段 <caption>…</caption>,以及内部的
// <thead>/<tbody>/<tfoot> 开闭标签(行本身保留)——拼进外层 <table> 时不能嵌套 tbody。
//
// 只能在 sanitize 之前对**原始**(未净化)html 做这一步,不能反过来。剥掉一个
// <tbody>/<caption> 标签有可能把它两侧本来无害的碎片重新拼成一个新标签——例如
// `<td><<tbody>img/src=x/onerror=alert(1)></td>` 剥掉 "<tbody>" 后就变成
// `<td><img/src=x/onerror=alert(1)></td>`,这个新出现的 <img> 标签必须还要经过
// sanitize 才能被拦下;如果 sanitize 先跑,它看到的是剥离前的原文,压根不知道这个
// <img> 会在剥离后冒出来。所以 sanitize 必须是净化流水线里写进 innerHTML 前的
// 最后一步,外层由 composeTablePartsHtml 自己拼的 `<table>`/`<tbody id class>`
// 在 sanitize 之后追加,不会被再次拼接进 sanitize 的输入。
function unwrapTableRows(rawHtml: string): string {
  const withoutTable = rawHtml
    .replace(/^\s*<table(\s[^>]*)?>/i, "")
    .replace(/<\/table>\s*$/i, "");
  const withoutCaption = withoutTable.replace(/<caption(\s[^>]*)?>[\s\S]*?<\/caption>/gi, "");
  return withoutCaption.replace(/<\/?(?:thead|tbody|tfoot)(\s[^>]*)?>/gi, "");
}

/**
 * 把同一组的多个表格分段拼成一张 <table>:每段一个 <tbody id="{domId}">,domId
 * 沿用 sourceElementDomId,让现有的 getElementById 滚动定位原样生效(第 1 段自带
 * 表头行,后面几段不重复表头,由解析层保证)。不接收高亮状态——高亮是频繁翻转的
 * 显示态(引用跳转后 2.6s 自动清除),掺进这段 HTML 字符串会让调用方每次高亮变化
 * 都要重新拼一遍整张合并表格再整体丢进 dangerouslySetInnerHTML,大表因此反复整段
 * 重排、还会打断用户在表格里的文本选区。高亮改由调用方按 domId 对已渲染出的
 * <tbody> 做 classList.toggle。
 */
export function composeTablePartsHtml(
  parts: { domId: string; html: string }[],
  sanitize: (html: string) => string,
): string {
  const bodies = parts.map((part) => {
    const rows = sanitize(unwrapTableRows(part.html));
    return `<tbody id="${escapeAttr(part.domId)}" class="table-part">${rows}</tbody>`;
  });
  return `<table>${bodies.join("")}</table>`;
}
