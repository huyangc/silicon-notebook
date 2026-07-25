import type { KnowhowColumn, KnowhowRow } from "./knowhow-model.ts";

export const KNOWHOW_WIDTH_SAMPLE_LIMIT = 64;
export const KNOWHOW_WIDTH_SAMPLE_HEAD = 48;
export const KNOWHOW_WIDTH_SAMPLE_TAIL = 16;
export const KNOWHOW_WIDTH_LINE_LIMIT = 8;
export const KNOWHOW_WIDTH_GRAPHEME_LIMIT = 120;
// Must be applied before newline normalization, Markdown regexes, splitting, or
// grapheme segmentation. This is the per-cell byte-work fuse; the later limits
// only constrain the semantic sample inside this already bounded prefix.
export const KNOWHOW_WIDTH_CODE_UNIT_LIMIT = 4_096;

const RAW_LINE_GRAPHEME_LIMIT = KNOWHOW_WIDTH_GRAPHEME_LIMIT;
const UNIT_PX = 7.5;
const CELL_HORIZONTAL_PADDING_PX = 24;
const HEADER_AFFORDANCE_PX = 52;
const WIDTH_ALIGNMENT_PX = 4;

export type KnowhowColumnWidth = { columnId: string; widthPx: number };

export type KnowhowColumnWidths = {
  columns: KnowhowColumnWidth[];
  statusWidthPx: number;
  tableWidthPx: number;
};

type WidthRange = { min: number; max: number };

const DESKTOP_WIDTHS: Record<KnowhowColumn["role"], WidthRange> = {
  anchor: { min: 180, max: 320 },
  procedure: { min: 200, max: 520 },
  entity: { min: 160, max: 360 },
  attribute: { min: 160, max: 420 },
};

const COMPACT_WIDTHS: Record<KnowhowColumn["role"], WidthRange> = {
  anchor: { min: 160, max: 280 },
  procedure: { min: 176, max: 380 },
  entity: { min: 144, max: 320 },
  attribute: { min: 144, max: 320 },
};

let graphemeSegmenter: Intl.Segmenter | null | undefined;

function* graphemes(value: string): Iterable<string> {
  if (graphemeSegmenter === undefined) {
    graphemeSegmenter = typeof Intl.Segmenter === "function"
      ? new Intl.Segmenter(undefined, { granularity: "grapheme" })
      : null;
  }
  if (!graphemeSegmenter) {
    yield* value;
    return;
  }
  for (const part of graphemeSegmenter.segment(value)) yield part.segment;
}

function takeGraphemes(value: string, limit: number): string {
  if (limit <= 0) return "";
  const bounded: string[] = [];
  for (const grapheme of graphemes(value)) {
    bounded.push(grapheme);
    if (bounded.length >= limit) break;
  }
  return bounded.join("");
}

function stripMarkdownControls(value: string): string {
  // Detect fences before removing paired inline-code delimiters: otherwise the
  // first two backticks could be mistaken for an empty inline-code pair.
  if (/^\s*(`{3,}|~{3,})\s*\w*\s*$/.test(value)) return "";
  let text = value
    .replace(/^\s{0,3}(?:#{1,6}\s+|>\s?|[-+*]\s+|\d+[.)]\s+)/, "")
    .replace(/^\s*\[[ xX]\]\s+/, "")
    .replace(/!\[([^\]]*)\]\([^\n)]*\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^\n)]*\)/g, "$1")
    .replace(/(`{1,3})([^`\n]*?)\1/g, "$2")
    .replace(/(\*{1,3}|_{1,3}|~~)([^\n]*?)\1/g, "$2");

  // A Markdown table renders cells independently. Estimate the widest cell instead
  // of treating every pipe-delimited cell as one long visual line.
  const pipeCount = (text.match(/\|/g) ?? []).length;
  if (pipeCount >= 2 || text.trim().startsWith("|") || text.trim().endsWith("|")) {
    const cells = text.split("|").map((cell) => cell.trim());
    text = cells.reduce((widest, cell) =>
      estimateGraphemeUnits(cell) > estimateGraphemeUnits(widest) ? cell : widest, "");
  }

  // GFM delimiter rows and fenced-code delimiters are structural, not visible copy.
  if (/^\s*(?::?-{3,}:?\s*)+$/.test(text)) return "";
  return text;
}

function isWideGrapheme(value: string): boolean {
  return /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}\p{Extended_Pictographic}]/u.test(value)
    || /[\u3000-\u303f\uff01-\uff60\uffe0-\uffe6]/u.test(value);
}

export function estimateGraphemeUnits(value: string): number {
  let units = 0;
  for (const grapheme of graphemes(value.replace(/\t/g, "  "))) {
    if (isWideGrapheme(grapheme)) units += 2;
    else if (/^[A-Za-z0-9_]$/.test(grapheme)) units += 1;
    else if (/^[\x20]$/.test(grapheme)) units += 0.5;
    else if (/^[\x21-\x2f\x3a-\x40\x5b-\x60\x7b-\x7e]$/.test(grapheme)) units += 0.75;
    else if (/^\s$/u.test(grapheme)) units += 0.5;
    else units += 1;
  }
  return units;
}

/** Estimate only the longest rendered line, with hard per-cell work limits. */
export function estimateMarkdownVisibleLineUnits(markdown: string): number {
  const normalized = markdown.slice(0, KNOWHOW_WIDTH_CODE_UNIT_LIMIT).replace(/\r\n?/g, "\n");
  const lines = normalized.split("\n", KNOWHOW_WIDTH_LINE_LIMIT);
  let widest = 0;
  for (const rawLine of lines) {
    const boundedRaw = takeGraphemes(rawLine, RAW_LINE_GRAPHEME_LIMIT);
    const visible = takeGraphemes(stripMarkdownControls(boundedRaw), KNOWHOW_WIDTH_GRAPHEME_LIMIT);
    widest = Math.max(widest, estimateGraphemeUnits(visible));
  }
  return widest;
}

/** Deterministic first-48/last-16 sample; short inputs are never duplicated. */
export function sampleVisibleKnowhowRows(rows: KnowhowRow[]): KnowhowRow[] {
  if (rows.length <= KNOWHOW_WIDTH_SAMPLE_LIMIT) return rows.slice();
  return [
    ...rows.slice(0, KNOWHOW_WIDTH_SAMPLE_HEAD),
    ...rows.slice(-KNOWHOW_WIDTH_SAMPLE_TAIL),
  ];
}

function alignWidth(width: number): number {
  return Math.ceil(width / WIDTH_ALIGNMENT_PX) * WIDTH_ALIGNMENT_PX;
}

function clampWidth(width: number, range: WidthRange): number {
  return alignWidth(Math.min(range.max, Math.max(range.min, width)));
}

export function computeKnowhowColumnWidths({
  columns,
  visibleRowsSample,
  anchorColumnId,
  compact,
}: {
  columns: KnowhowColumn[];
  visibleRowsSample: KnowhowRow[];
  anchorColumnId: string | null;
  compact: boolean;
}): KnowhowColumnWidths {
  const ranges = compact ? COMPACT_WIDTHS : DESKTOP_WIDTHS;
  const result = columns.map((column): KnowhowColumnWidth => {
    try {
      const role = column.id === anchorColumnId ? "anchor" : column.role;
      const contentUnits = visibleRowsSample.reduce(
        (widest, row) => Math.max(widest, estimateMarkdownVisibleLineUnits(row.cells[column.id] ?? "")),
        0,
      );
      const headerUnits = estimateGraphemeUnits(takeGraphemes(column.name, KNOWHOW_WIDTH_GRAPHEME_LIMIT));
      const contentPx = Math.ceil(contentUnits * UNIT_PX) + CELL_HORIZONTAL_PADDING_PX;
      const headerPx = Math.ceil(headerUnits * UNIT_PX) + CELL_HORIZONTAL_PADDING_PX + HEADER_AFFORDANCE_PX;
      return { columnId: column.id, widthPx: clampWidth(Math.max(contentPx, headerPx), ranges[role]) };
    } catch {
      return { columnId: column.id, widthPx: 200 };
    }
  });
  const statusWidthPx = compact ? 104 : 112;
  return {
    columns: result,
    statusWidthPx,
    tableWidthPx: result.reduce((total, column) => total + column.widthPx, statusWidthPx),
  };
}
