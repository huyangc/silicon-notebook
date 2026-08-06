export type AnswerAnchorLike = {
  key: string;
  object_id: string;
  object_type: string;
  label: string;
  name?: string;
  definition?: string | null;
  snippet?: string | null;
  source_title?: string;
  source_file_name?: string;
  location_label?: string;
  source_id?: string;
  element_id?: string;
  tier?: string;
  // 多领域基准库(Task 14)：只在跨库命中(federated retrieval 从一个挂载的参考库
  // 找到、并非本次 ask 所在 notebook 的证据)时非空；本库内证据这个字段缺席
  // (后端 exclude_if，见 schemas.py AnswerAnchor.notebook_id)。引用徽章据此查
  // id→name 映射，标"来自「某某库」"。
  notebook_id?: string;
  // Task 12b（引用跳转扩面）：与 CitationLike.knowhow 同一 wire 形状/惯例——
  // 只有命中单行 knowhow 格子的知识对象锚点才有此字段（后端
  // `AnswerAnchor.knowhow`，evidence_context.py 的 knowledge_context/
  // parse_anchors 填充），合并多行/非 knowhow 锚点整体缺席。这是「答案 [k]
  // 标记命中」这条主路径的引用跳转入口，与 CitationLike 侧的回退列表入口
  // 互补（buildAnswerReferences 优先展示 anchor 分支）。
  knowhow?: { table_id: string; row_id: string } | null;
};

export type CitationLike = {
  label: string;
  source_id: string;
  element_id: string;
  location_label: string;
  quoted_span: string;
  source_file_name?: string;
  tier?: string;
  // 多领域基准库(Task 14)：与 AnswerAnchorLike.notebook_id 同一惯例——只在跨库
  // 命中时非空，供引用徽章查 id→name 映射标来源库名。见 schemas.py
  // Citation.notebook_id 的完整注释。
  notebook_id?: string;
  // Task 12（引用跳转）：命中 knowhow 格子的引用才有此字段（后端
  // `Citation.knowhow`，非 knowhow 引用整体缺席，见 evidence_context.py
  // citations_from 的 exclude_if 惯例）。字段名保持后端线上 snake_case——
  // 本类型本就是 wire 形状的镜像，camelCase 映射统一交给
  // knowhow-model.ts 的 mapCitationKnowhowRef 在使用侧做。
  knowhow?: { table_id: string; row_id: string } | null;
};

export type AnswerReference = {
  id: string;
  displayLabel: string;
  anchor?: AnswerAnchorLike;
  citation?: CitationLike;
};

export type MarkdownBlock =
  | { type: "paragraph"; text: string }
  | { type: "code"; language: string; code: string }
  | { type: "formula"; latex: string }
  | { type: "table"; headers: string[]; rows: string[][] }
  | { type: "unordered-list"; items: string[] }
  | { type: "ordered-list"; items: string[] };

const ANCHOR_MARKER_GROUP_RE = /(?:\[(?:k\d+\s*[,，]\s*)*k\d+\]|【(?:k\d+\s*[,，]\s*)*k\d+】)/g;

function anchorKeysFromMarker(marker: string): string[] {
  return marker.slice(1, -1).split(/[,，]/).map((part) => part.trim()).filter(Boolean);
}

export function buildAnswerReferences(
  answerText: string,
  anchors: AnswerAnchorLike[] = [],
  citations: CitationLike[] = [],
): AnswerReference[] {
  const anchorsByKey = new Map(anchors.map((anchor) => [anchor.key, anchor]));
  const references: AnswerReference[] = [];
  const seen = new Set<string>();

  for (const match of answerText.matchAll(ANCHOR_MARKER_GROUP_RE)) {
    const keys = anchorKeysFromMarker(match[0]);
    const matchedAnchors = keys.map((key) => anchorsByKey.get(key));

    // A grouped marker is one evidence claim. Never bind only its known subset:
    // if any key is unknown, leave the entire marker unbound.
    if (matchedAnchors.length === 0 || matchedAnchors.some((anchor) => !anchor)) continue;

    keys.forEach((key, index) => {
      if (seen.has(key)) return;
      seen.add(key);
      references.push({
        id: `anchor:${key}`,
        displayLabel: `[${references.length + 1}]`,
        anchor: matchedAnchors[index]!,
      });
    });
  }

  if (references.length > 0) return references;

  return citations.map((citation, index) => ({
    id: `citation:${citation.source_id}:${citation.element_id}:${index}`,
    displayLabel: `[${index + 1}]`,
    citation,
  }));
}

// Source-tier distribution for this answer's badge. Partitions the SAME references the
// user actually sees (the `[k]` list from `buildAnswerReferences`) by tier, so
// `personal + base` ALWAYS equals the reference count — it can never exceed what's shown.
// (The previous impl summed `anchors ∪ citations` — two overlapping views of the same
// chunks keyed on different id spaces, object_id vs source_id — so a source present in
// both got counted twice, inflating base and producing totals above the visible count,
// e.g. "个人 15 · 基准库 7 = 22" for a 15-reference answer.) Unset/unknown tier counts as
// "personal" (matches the backend's `Citation.tier` / `AnswerAnchor.tier` default),
// mirroring the report-mode badge whose `personal = total − base` can never over-count.
export function computeSourceTierCounts(
  references: AnswerReference[],
): { personal: number; base: number } {
  let base = 0;
  for (const reference of references) {
    const tier = reference.anchor?.tier ?? reference.citation?.tier;
    if (tier === "base") base += 1;
  }
  return { personal: references.length - base, base };
}

export function referenceByAnchorKey(references: AnswerReference[]): Record<string, AnswerReference> {
  return Object.fromEntries(
    references
      .filter((reference) => reference.anchor?.key)
      .map((reference) => [reference.anchor!.key, reference]),
  );
}

export function referenceByCitationKey(references: AnswerReference[]): Record<string, AnswerReference> {
  const entries: Array<[string, AnswerReference]> = [];
  for (const reference of references) {
    if (reference.anchor?.key) {
      entries.push([reference.anchor.key, reference]);
    }
    const displayNumber = reference.displayLabel.match(/^\[(\d+)\]$/)?.[1];
    if (displayNumber) {
      entries.push([displayNumber, reference]);
    }
  }
  return Object.fromEntries(entries);
}

export function renderTextWithReferenceNumbers(text: string, references: AnswerReference[]): string {
  const byKey = referenceByAnchorKey(references);
  return text.replace(ANCHOR_MARKER_GROUP_RE, (token) => {
    const keys = anchorKeysFromMarker(token);
    const matchedReferences = keys.map((key) => byKey[key]);
    if (matchedReferences.length === 0 || matchedReferences.some((reference) => !reference)) {
      return token;
    }
    const displayNumbers = matchedReferences.map(
      (reference) => reference!.displayLabel.match(/^\[(\d+)\]$/)?.[1],
    );
    return displayNumbers.every(Boolean)
      ? `[${displayNumbers.join(", ")}]`
      : matchedReferences.map((reference) => reference!.displayLabel).join(", ");
  });
}

export function parseMarkdownBlocks(markdown: string): MarkdownBlock[] {
  const lines = markdown.replace(/\r\n/g, "\n").split("\n");
  const blocks: MarkdownBlock[] = [];
  let index = 0;

  while (index < lines.length) {
    if (!lines[index].trim()) {
      index += 1;
      continue;
    }

    const fence = lines[index].match(/^```([A-Za-z0-9_-]+)?\s*$/);
    if (fence) {
      const code: string[] = [];
      index += 1;
      while (index < lines.length && !/^```\s*$/.test(lines[index])) {
        code.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      blocks.push({ type: "code", language: fence[1] ?? "", code: code.join("\n") });
      continue;
    }

    // Single-line display formula: `$$ ... $$` or `\[ ... \]` on one line.
    const singleLineFormula = matchSingleLineFormula(lines[index]);
    if (singleLineFormula) {
      blocks.push({ type: "formula", latex: singleLineFormula.trim() });
      index += 1;
      continue;
    }

    // Multi-line display formula opened by a bare `$$` or `\[` line.
    const fenceOpen = /^\$\$\s*$/.test(lines[index]) ? "$$" : /^\\\[\s*$/.test(lines[index]) ? "\\[" : "";
    if (fenceOpen) {
      const closeRe = fenceOpen === "$$" ? /^\$\$\s*$/ : /^\\\]\s*$/;
      const latex: string[] = [];
      index += 1;
      while (index < lines.length && !closeRe.test(lines[index])) {
        latex.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      blocks.push({ type: "formula", latex: latex.join("\n").trim() });
      continue;
    }

    if (isTableStart(lines, index)) {
      const headers = splitTableRow(lines[index]);
      index += 2;
      const rows: string[][] = [];
      while (index < lines.length && isTableRow(lines[index])) {
        rows.push(splitTableRow(lines[index]));
        index += 1;
      }
      blocks.push({ type: "table", headers, rows });
      continue;
    }

    if (/^\s*[-*]\s+/.test(lines[index])) {
      const items: string[] = [];
      while (index < lines.length && /^\s*[-*]\s+/.test(lines[index])) {
        items.push(lines[index].replace(/^\s*[-*]\s+/, "").trim());
        index += 1;
      }
      blocks.push({ type: "unordered-list", items });
      continue;
    }

    if (/^\s*\d+\.\s+/.test(lines[index])) {
      const items: string[] = [];
      while (index < lines.length && /^\s*\d+\.\s+/.test(lines[index])) {
        items.push(lines[index].replace(/^\s*\d+\.\s+/, "").trim());
        index += 1;
      }
      blocks.push({ type: "ordered-list", items });
      continue;
    }

    const paragraph: string[] = [];
    while (
      index < lines.length &&
      lines[index].trim() &&
      !/^```/.test(lines[index]) &&
      !isDisplayFormulaLine(lines[index]) &&
      !isTableStart(lines, index) &&
      !/^\s*[-*]\s+/.test(lines[index]) &&
      !/^\s*\d+\.\s+/.test(lines[index])
    ) {
      paragraph.push(lines[index].trim());
      index += 1;
    }
    blocks.push({ type: "paragraph", text: paragraph.join(" ") });
  }

  return blocks;
}

// Extract the latex body of a one-line `$$ ... $$` or `\[ ... \]`, else null.
function matchSingleLineFormula(line: string): string | null {
  const dollar = line.match(/^\s*\$\$([\s\S]+?)\$\$\s*$/);
  if (dollar) return dollar[1];
  const bracket = line.match(/^\s*\\\[([\s\S]+?)\\\]\s*$/);
  if (bracket) return bracket[1];
  return null;
}

// True when a line begins (or is) a display-formula block, so paragraphs stop here.
function isDisplayFormulaLine(line: string): boolean {
  return (
    /^\$\$\s*$/.test(line) || // bare `$$` opener
    /^\\\[\s*$/.test(line) || // bare `\[` opener
    matchSingleLineFormula(line) !== null
  );
}

export type InlineSegment = { type: "text" | "math"; value: string };

// Split prose into plain-text and inline-math segments, recognizing `$...$`
// and `\( ... \)`. Text without delimiters is returned as a single text
// segment, so normal prose (and `[k]` markers) is never mangled.
export function splitInlineLatex(text: string): InlineSegment[] {
  const segments: InlineSegment[] = [];
  // Single-line `$...$` (no embedded `$` or newline) or `\( ... \)`.
  const pattern = /\$([^$\n]+?)\$|\\\(([\s\S]+?)\\\)/g;
  let lastIndex = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index == null) continue;
    if (match.index > lastIndex) {
      segments.push({ type: "text", value: text.slice(lastIndex, match.index) });
    }
    const body = match[1] ?? match[2] ?? "";
    segments.push({ type: "math", value: body.trim() });
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < text.length) {
    segments.push({ type: "text", value: text.slice(lastIndex) });
  }
  if (segments.length === 0) segments.push({ type: "text", value: text });
  return segments;
}

function isTableStart(lines: string[], index: number): boolean {
  return isTableRow(lines[index]) && index + 1 < lines.length && /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(lines[index + 1]);
}

function isTableRow(line: string): boolean {
  return line.includes("|") && splitTableRow(line).length >= 2;
}

function splitTableRow(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
}
