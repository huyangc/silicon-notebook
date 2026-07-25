export const KNOWHOW_DIFF_MAX_CHARS = 20_000;
export const KNOWHOW_DIFF_MAX_LINES = 400;
export const KNOWHOW_DIFF_MAX_LINE_STEPS = 200_000;
export const KNOWHOW_DIFF_MAX_TOKENS_PER_LINE = 256;
export const KNOWHOW_DIFF_MAX_TOKEN_CELLS_PER_LINE = 65_536;
export const KNOWHOW_DIFF_MAX_TOKEN_CELLS_TOTAL = 250_000;
export const KNOWHOW_DIFF_COARSE_CONTEXT_LINES = 8;
export const KNOWHOW_DIFF_COARSE_BLOCK_LINES = 32;
export const KNOWHOW_DIFF_COARSE_LINE_CHARS = 1_000;

export type KnowhowDiffKind = "context" | "delete" | "add";

export type KnowhowDiffToken = {
  text: string;
  changed: boolean;
  whitespace: boolean;
};

export type KnowhowDiffLine = {
  kind: KnowhowDiffKind;
  text: string;
  tokens?: KnowhowDiffToken[];
  omittedLineCount?: number;
  truncated?: boolean;
};

export type KnowhowMarkdownDiff = {
  lines: KnowhowDiffLine[];
  degraded: boolean;
  degradationReason?: "characters" | "lines" | "line_steps";
};

function normalizedLines(value: string): string[] {
  return value.replace(/\r\n?/g, "\n").split("\n");
}

function commonPrefixLength(before: string[], after: string[]): number {
  const limit = Math.min(before.length, after.length);
  let index = 0;
  while (index < limit && before[index] === after[index]) index += 1;
  return index;
}

function commonSuffixLength(before: string[], after: string[], prefix: number): number {
  const limit = Math.min(before.length, after.length) - prefix;
  let count = 0;
  while (count < limit && before[before.length - 1 - count] === after[after.length - 1 - count]) count += 1;
  return count;
}

function coarseDiff(
  before: string[],
  after: string[],
  reason: KnowhowMarkdownDiff["degradationReason"],
): KnowhowMarkdownDiff {
  const prefix = commonPrefixLength(before, after);
  const suffix = commonSuffixLength(before, after, prefix);
  const boundedLine = (kind: KnowhowDiffKind, text: string): KnowhowDiffLine => ({
    kind,
    text: text.length > KNOWHOW_DIFF_COARSE_LINE_CHARS ? text.slice(0, KNOWHOW_DIFF_COARSE_LINE_CHARS) : text,
    truncated: text.length > KNOWHOW_DIFF_COARSE_LINE_CHARS || undefined,
  });
  const boundedBlock = (kind: KnowhowDiffKind, values: string[], maximum: number): KnowhowDiffLine[] => {
    if (values.length <= maximum) return values.map((text) => boundedLine(kind, text));
    const leading = Math.ceil(maximum / 2);
    const trailing = Math.floor(maximum / 2);
    const omitted = values.length - leading - trailing;
    return [
      ...values.slice(0, leading).map((text) => boundedLine(kind, text)),
      { kind, text: `… 已省略 ${omitted} 行 …`, omittedLineCount: omitted },
      ...values.slice(values.length - trailing).map((text) => boundedLine(kind, text)),
    ];
  };
  const prefixLines = before.slice(0, prefix);
  const suffixLines = suffix > 0 ? before.slice(before.length - suffix) : [];
  const lines: KnowhowDiffLine[] = [
    ...boundedBlock("context", prefixLines, KNOWHOW_DIFF_COARSE_CONTEXT_LINES),
    ...boundedBlock("delete", before.slice(prefix, before.length - suffix), KNOWHOW_DIFF_COARSE_BLOCK_LINES),
    ...boundedBlock("add", after.slice(prefix, after.length - suffix), KNOWHOW_DIFF_COARSE_BLOCK_LINES),
  ];
  if (suffixLines.length > 0) {
    lines.push(...boundedBlock("context", suffixLines, KNOWHOW_DIFF_COARSE_CONTEXT_LINES));
  }
  return { lines, degraded: true, degradationReason: reason };
}

function backtrackMyers(trace: Map<number, number>[], before: string[], after: string[]): KnowhowDiffLine[] {
  let x = before.length;
  let y = after.length;
  const reversed: KnowhowDiffLine[] = [];
  for (let depth = trace.length - 1; depth >= 0; depth -= 1) {
    const previous = trace[depth];
    const diagonal = x - y;
    const down = diagonal === -depth
      || (diagonal !== depth && (previous.get(diagonal - 1) ?? -1) < (previous.get(diagonal + 1) ?? -1));
    const previousDiagonal = down ? diagonal + 1 : diagonal - 1;
    const previousX = previous.get(previousDiagonal) ?? 0;
    const previousY = previousX - previousDiagonal;
    while (x > previousX && y > previousY) {
      reversed.push({ kind: "context", text: before[x - 1] });
      x -= 1;
      y -= 1;
    }
    if (depth === 0) break;
    if (down) {
      reversed.push({ kind: "add", text: after[y - 1] });
      y -= 1;
    } else {
      reversed.push({ kind: "delete", text: before[x - 1] });
      x -= 1;
    }
  }
  return reversed.reverse();
}

function boundedMyers(before: string[], after: string[]): KnowhowDiffLine[] | null {
  const maximumDepth = before.length + after.length;
  let frontier = new Map<number, number>([[1, 0]]);
  const trace: Map<number, number>[] = [];
  let steps = 0;
  for (let depth = 0; depth <= maximumDepth; depth += 1) {
    trace.push(new Map(frontier));
    const next = new Map<number, number>();
    for (let diagonal = -depth; diagonal <= depth; diagonal += 2) {
      steps += 1;
      if (steps > KNOWHOW_DIFF_MAX_LINE_STEPS) return null;
      let x = diagonal === -depth
        || (diagonal !== depth && (frontier.get(diagonal - 1) ?? -1) < (frontier.get(diagonal + 1) ?? -1))
        ? frontier.get(diagonal + 1) ?? 0
        : (frontier.get(diagonal - 1) ?? 0) + 1;
      let y = x - diagonal;
      while (x < before.length && y < after.length && before[x] === after[y]) {
        x += 1;
        y += 1;
        steps += 1;
        if (steps > KNOWHOW_DIFF_MAX_LINE_STEPS) return null;
      }
      next.set(diagonal, x);
      if (x >= before.length && y >= after.length) return backtrackMyers(trace, before, after);
    }
    frontier = next;
  }
  return null;
}

function* graphemes(value: string): Iterable<string> {
  if (typeof Intl !== "undefined" && "Segmenter" in Intl) {
    const Segmenter = Intl.Segmenter as typeof Intl.Segmenter;
    for (const part of new Segmenter(undefined, { granularity: "grapheme" }).segment(value)) {
      yield part.segment;
    }
    return;
  }
  yield* value;
}

function isAsciiWord(value: string): boolean {
  return /^[A-Za-z0-9_]$/.test(value);
}

function isWhitespace(value: string): boolean {
  return value === " " || value === "\t";
}

function isFence(value: string): boolean {
  return value === "`" || value === "*" || value === "_" || value === "~";
}

export function tokenizeKnowhowDiffLine(value: string): string[] {
  const result: string[] = [];
  let runKind: "ascii" | "whitespace" | "fence" | null = null;
  let runFence = "";
  let runParts: string[] = [];

  const flushRun = () => {
    if (runParts.length > 0) result.push(runParts.join(""));
    runKind = null;
    runFence = "";
    runParts = [];
  };

  for (const grapheme of graphemes(value)) {
    const nextKind = isAsciiWord(grapheme)
      ? "ascii"
      : isWhitespace(grapheme)
        ? "whitespace"
        : isFence(grapheme)
          ? "fence"
          : null;
    const continuesRun = nextKind !== null
      && nextKind === runKind
      && (nextKind !== "fence" || grapheme === runFence);
    if (!continuesRun) flushRun();
    if (nextKind === null) result.push(grapheme);
    else {
      if (runKind === null) {
        runKind = nextKind;
        runFence = nextKind === "fence" ? grapheme : "";
      }
      runParts.push(grapheme);
    }
  }
  flushRun();
  return result;
}

function tokenPieces(tokens: string[], changed: boolean[]): KnowhowDiffToken[] {
  return tokens.map((text, index) => ({ text, changed: changed[index], whitespace: /^[ \t]+$/.test(text) }));
}

function addTokenDiff(
  deleted: KnowhowDiffLine,
  added: KnowhowDiffLine,
  budget: { remaining: number },
): void {
  const before = tokenizeKnowhowDiffLine(deleted.text);
  const after = tokenizeKnowhowDiffLine(added.text);
  const cells = before.length * after.length;
  if (
    before.length > KNOWHOW_DIFF_MAX_TOKENS_PER_LINE
    || after.length > KNOWHOW_DIFF_MAX_TOKENS_PER_LINE
    || cells > KNOWHOW_DIFF_MAX_TOKEN_CELLS_PER_LINE
    || cells > budget.remaining
  ) return;
  budget.remaining -= cells;
  const width = after.length + 1;
  const matrix = new Uint16Array((before.length + 1) * width);
  for (let left = 1; left <= before.length; left += 1) {
    for (let right = 1; right <= after.length; right += 1) {
      const offset = left * width + right;
      matrix[offset] = before[left - 1] === after[right - 1]
        ? matrix[(left - 1) * width + right - 1] + 1
        : Math.max(matrix[(left - 1) * width + right], matrix[left * width + right - 1]);
    }
  }
  const beforeChanged = before.map(() => true);
  const afterChanged = after.map(() => true);
  let left = before.length;
  let right = after.length;
  while (left > 0 && right > 0) {
    if (before[left - 1] === after[right - 1]) {
      beforeChanged[left - 1] = false;
      afterChanged[right - 1] = false;
      left -= 1;
      right -= 1;
    } else if (matrix[(left - 1) * width + right] >= matrix[left * width + right - 1]) {
      left -= 1;
    } else {
      right -= 1;
    }
  }
  deleted.tokens = tokenPieces(before, beforeChanged);
  added.tokens = tokenPieces(after, afterChanged);
}

function enrichChangedBlocks(lines: KnowhowDiffLine[]): void {
  const budget = { remaining: KNOWHOW_DIFF_MAX_TOKEN_CELLS_TOTAL };
  let index = 0;
  while (index < lines.length) {
    if (lines[index].kind === "context") {
      index += 1;
      continue;
    }
    const start = index;
    while (index < lines.length && lines[index].kind !== "context") index += 1;
    const block = lines.slice(start, index);
    const deleted = block.filter((line) => line.kind === "delete");
    const added = block.filter((line) => line.kind === "add");
    for (let pair = 0; pair < Math.min(deleted.length, added.length); pair += 1) {
      addTokenDiff(deleted[pair], added[pair], budget);
    }
  }
}

export function diffKnowhowMarkdown(beforeMd: string, afterMd: string): KnowhowMarkdownDiff {
  const before = normalizedLines(beforeMd);
  const after = normalizedLines(afterMd);
  if (beforeMd.length > KNOWHOW_DIFF_MAX_CHARS || afterMd.length > KNOWHOW_DIFF_MAX_CHARS) {
    return coarseDiff(before, after, "characters");
  }
  if (before.length > KNOWHOW_DIFF_MAX_LINES || after.length > KNOWHOW_DIFF_MAX_LINES) {
    return coarseDiff(before, after, "lines");
  }
  const prefix = commonPrefixLength(before, after);
  const suffix = commonSuffixLength(before, after, prefix);
  const middleBefore = before.slice(prefix, before.length - suffix);
  const middleAfter = after.slice(prefix, after.length - suffix);
  const middle = boundedMyers(middleBefore, middleAfter);
  if (!middle) return coarseDiff(before, after, "line_steps");
  const lines: KnowhowDiffLine[] = [
    ...before.slice(0, prefix).map((text) => ({ kind: "context" as const, text })),
    ...middle,
    ...(suffix > 0 ? before.slice(before.length - suffix).map((text) => ({ kind: "context" as const, text })) : []),
  ];
  enrichChangedBlocks(lines);
  return { lines, degraded: false };
}
