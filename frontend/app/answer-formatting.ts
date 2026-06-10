export type AnswerAnchorLike = {
  key: string;
  object_id: string;
  object_type: string;
  label: string;
  name?: string;
  definition?: string | null;
  snippet?: string | null;
  source_title?: string;
  location_label?: string;
  tier?: string;
};

export type CitationLike = {
  label: string;
  source_id: string;
  element_id: string;
  location_label: string;
  quoted_span: string;
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

export function buildAnswerReferences(
  answerText: string,
  anchors: AnswerAnchorLike[] = [],
  citations: CitationLike[] = [],
): AnswerReference[] {
  const anchorsByKey = new Map(anchors.map((anchor) => [anchor.key, anchor]));
  const references: AnswerReference[] = [];
  const seen = new Set<string>();

  for (const match of answerText.matchAll(/\[(k\d+)\]/g)) {
    const key = match[1];
    const anchor = anchorsByKey.get(key);
    if (!anchor || seen.has(key)) continue;
    seen.add(key);
    references.push({
      id: `anchor:${key}`,
      displayLabel: `[${references.length + 1}]`,
      anchor,
    });
  }

  if (references.length > 0) return references;

  return citations.map((citation, index) => ({
    id: `citation:${citation.source_id}:${citation.element_id}:${index}`,
    displayLabel: `[${index + 1}]`,
    citation,
  }));
}

export function referenceByAnchorKey(references: AnswerReference[]): Record<string, AnswerReference> {
  return Object.fromEntries(
    references
      .filter((reference) => reference.anchor?.key)
      .map((reference) => [reference.anchor!.key, reference]),
  );
}

export function renderTextWithReferenceNumbers(text: string, references: AnswerReference[]): string {
  const byKey = referenceByAnchorKey(references);
  return text.replace(/\[(k\d+)\]/g, (token, key: string) => byKey[key]?.displayLabel ?? token);
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
