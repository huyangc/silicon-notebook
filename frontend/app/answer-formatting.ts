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

    if (/^\$\$\s*$/.test(lines[index])) {
      const latex: string[] = [];
      index += 1;
      while (index < lines.length && !/^\$\$\s*$/.test(lines[index])) {
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
      !/^\$\$\s*$/.test(lines[index]) &&
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
