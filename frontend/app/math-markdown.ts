/**
 * Normalize model- and parser-produced math before it reaches remark-math or
 * the direct KaTeX renderers.
 *
 * remark-math treats a one-line `$$...$$` immediately adjacent to prose as
 * inline math.  Long equations then inherit inline layout and can be clipped
 * by the notebook panels.  Expanding a whole-line pair into normal display
 * fences keeps it a display block without changing the LaTeX body.
 */
export function normalizeMathMarkdown(markdown: string): string {
  if (!markdown) return markdown;

  let normalized = markdown.replace(/\r\n?/g, "\n");

  // Some model gateways return the markdown payload one JSON-string layer too
  // deep. Only decode this shape when an escaped line break occurs outside a
  // math span. The single-pass decoder preserves JSON-escaped LaTeX commands
  // (`\\alpha` -> `\alpha`) and equation line breaks (`\\\\` -> `\\`).
  if (
    !normalized.includes("\n")
    && hasEscapedLineBreakOutsideMath(normalized)
    && /\$\$|\\+\[|\\+\(/.test(normalized)
  ) {
    normalized = decodeJsonStringLayer(normalized).replace(/\r\n?/g, "\n");
  }

  const lines = normalized.split("\n");
  const output: string[] = [];
  let fence: FenceState | null = null;

  for (const line of lines) {
    if (fence) {
      if (isClosingFence(line, fence)) fence = null;
      output.push(line);
      continue;
    }

    const openingFence = parseOpeningFence(line);
    if (openingFence) {
      const validInfoString = openingFence.marker === "~" || !openingFence.info.includes("`");
      if (validInfoString) {
        fence = openingFence;
        output.push(line);
        continue;
      }
    }

    const oneLineDisplay = line.match(
      /^((?:(?: {0,3}>[ \t]?)+)? {0,3})(?:([-+*]|\d{1,9}[.)])([ \t]+))?\$\$\s*(\S(?:.*?\S)?)\s*\$\$\s*$/,
    );
    if (oneLineDisplay) {
      const containerPrefix = oneLineDisplay[1];
      const listMarker = oneLineDisplay[2] ?? "";
      const listSpacing = oneLineDisplay[3] ?? "";
      const firstPrefix = `${containerPrefix}${listMarker}${listSpacing}`;
      const continuationPrefix = listMarker
        ? `${containerPrefix}${" ".repeat(listMarker.length + listSpacing.length)}`
        : containerPrefix;
      output.push(
        `${firstPrefix}$$`,
        `${continuationPrefix}${oneLineDisplay[4]}`,
        `${continuationPrefix}$$`,
      );
      continue;
    }

    output.push(line);
  }

  return output.join("\n");
}

type FenceState = {
  marker: "`" | "~";
  length: number;
  quoteDepth: number;
  listContentIndent: number | null;
  info: string;
};

function splitBlockquotePrefix(line: string): { quoteDepth: number; rest: string } {
  let quoteDepth = 0;
  let rest = line;
  while (true) {
    const marker = rest.match(/^ {0,3}>[ \t]?/);
    if (!marker) break;
    quoteDepth += 1;
    rest = rest.slice(marker[0].length);
  }
  return { quoteDepth, rest };
}

function parseOpeningFence(line: string): FenceState | null {
  const { quoteDepth, rest } = splitBlockquotePrefix(line);
  const match = rest.match(
    /^( {0,3})(?:([-+*]|\d{1,9}[.)])([ \t]+))?(`{3,}|~{3,})(.*)$/,
  );
  if (!match) return null;
  const listContentIndent = match[2]
    ? match[1].length + match[2].length + whitespaceColumns(match[3])
    : null;
  return {
    marker: match[4][0] as "`" | "~",
    length: match[4].length,
    quoteDepth,
    listContentIndent,
    info: match[5],
  };
}

function isClosingFence(line: string, fence: FenceState): boolean {
  const { quoteDepth, rest: containerRest } = splitBlockquotePrefix(line);
  if (quoteDepth !== fence.quoteDepth) return false;
  let rest = containerRest;
  if (fence.listContentIndent !== null) {
    const continuation = consumeIndentColumns(rest, fence.listContentIndent);
    if (continuation === null) return false;
    rest = continuation;
  }
  const closing = rest.match(/^ {0,3}(`+|~+)[ \t]*$/);
  return Boolean(
    closing
    && closing[1][0] === fence.marker
    && closing[1].length >= fence.length,
  );
}

function whitespaceColumns(value: string): number {
  let columns = 0;
  for (const character of value) {
    columns += character === "\t" ? 4 - (columns % 4) : 1;
  }
  return columns;
}

function consumeIndentColumns(value: string, requiredColumns: number): string | null {
  let columns = 0;
  let index = 0;
  while (index < value.length && columns < requiredColumns) {
    if (value[index] === " ") {
      columns += 1;
    } else if (value[index] === "\t") {
      columns += 4 - (columns % 4);
    } else {
      return null;
    }
    index += 1;
  }
  return columns >= requiredColumns ? value.slice(index) : null;
}

function hasEscapedLineBreakOutsideMath(value: string): boolean {
  let displayMath = false;
  let displayContentStart = -1;
  let displayHasLeadingBreak = false;
  let slashMath: "[" | "(" | null = null;
  let slashMathContentStart = -1;
  let slashMathHasLeadingBreak = false;

  for (let index = 0; index < value.length; index += 1) {
    if (!slashMath && value.startsWith("$$", index)) {
      displayMath = !displayMath;
      if (displayMath) {
        displayContentStart = index + 2;
        displayHasLeadingBreak = false;
      } else {
        displayContentStart = -1;
        displayHasLeadingBreak = false;
      }
      index += 1;
      continue;
    }

    if (value[index] !== "\\") continue;
    let runEnd = index;
    while (value[runEnd] === "\\") runEnd += 1;
    const runLength = runEnd - index;
    const next = value[runEnd];

    // A slash-delimited math wrapper is itself JSON-escaped in this shape.
    if (runLength % 2 === 0 && (next === "[" || next === "(")) {
      slashMath = next;
      slashMathContentStart = runEnd + 1;
      slashMathHasLeadingBreak = false;
    } else if (
      runLength % 2 === 0
      && ((slashMath === "[" && next === "]") || (slashMath === "(" && next === ")"))
    ) {
      slashMath = null;
      slashMathContentStart = -1;
      slashMathHasLeadingBreak = false;
    } else if (
      runLength % 2 === 1
      && (next === "n" || next === "r")
    ) {
      let separatorEnd = runEnd + 1;
      if (next === "r" && value.slice(separatorEnd, separatorEnd + 2) === "\\n") {
        separatorEnd += 2;
      }
      const before = value.slice(0, index).trimEnd();
      const after = value.slice(separatorEnd).trimStart();
      if (displayMath) {
        if (value.slice(displayContentStart, index).trim() === "") {
          displayHasLeadingBreak = true;
        }
        if (displayHasLeadingBreak && after.startsWith("$$")) return true;
      } else if (slashMath) {
        if (value.slice(slashMathContentStart, index).trim() === "") {
          slashMathHasLeadingBreak = true;
        }
        const closingSlashMath = slashMath === "[" ? /^(?:\\{1,2})\]/ : /^(?:\\{1,2})\)/;
        if (slashMathHasLeadingBreak && closingSlashMath.test(after)) return true;
      }
      // Requiring the escaped newline to border a math block distinguishes a
      // serialized paragraph boundary from ordinary prose LaTeX such as
      // `\newcommand` that happens to coexist with a formula later in the text.
      if (!displayMath && !slashMath && (endsWithMathBlock(before) || startsWithMathBlock(after))) {
        return true;
      }
    }
    index = runEnd - 1;
  }
  return false;
}

function startsWithMathBlock(value: string): boolean {
  return /^(?:(?: {0,3}>[ \t]?)+)? {0,3}(?:(?:[-+*]|\d{1,9}[.)])[ \t]+)?(?:\$\$|\\{1,2}\[)/.test(value);
}

function endsWithMathBlock(value: string): boolean {
  return /(?:\$\$|\\{1,2}\])$/.test(value);
}

function decodeJsonStringLayer(value: string): string {
  let output = "";
  for (let index = 0; index < value.length; index += 1) {
    const current = value[index];
    if (current !== "\\" || index + 1 >= value.length) {
      output += current;
      continue;
    }

    const escaped = value[index + 1];
    const replacements: Record<string, string> = {
      "\\": "\\",
      '"': '"',
      "/": "/",
      b: "\b",
      f: "\f",
      n: "\n",
      r: "\r",
      t: "\t",
    };
    if (escaped in replacements) {
      output += replacements[escaped];
      index += 1;
      continue;
    }

    if (escaped === "u") {
      const hex = value.slice(index + 2, index + 6);
      if (/^[0-9a-fA-F]{4}$/.test(hex)) {
        output += String.fromCharCode(Number.parseInt(hex, 16));
        index += 5;
        continue;
      }
    }

    // Preserve non-JSON escapes instead of eating a legitimate LaTeX slash.
    output += `\\${escaped}`;
    index += 1;
  }
  return output;
}

/** Remove delimiters that belong to Markdown, not to KaTeX's input grammar. */
export function unwrapStandaloneLatex(value: string): string {
  const latex = value.trim();
  const wrappers: Array<[string, string]> = [
    ["$$", "$$"],
    ["\\[", "\\]"],
    ["\\(", "\\)"],
    ["$", "$"],
  ];

  for (const [open, close] of wrappers) {
    if (latex.startsWith(open) && latex.endsWith(close) && latex.length > open.length + close.length) {
      return latex.slice(open.length, -close.length).trim();
    }
  }
  return latex;
}
