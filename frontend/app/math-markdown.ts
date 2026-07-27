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
  // deep.  Only decode this shape when the whole value has no real newline and
  // contains an escaped, non-command `\n` separator plus a math delimiter.
  // `\nabla` is deliberately excluded.
  if (
    !normalized.includes("\n")
    && /\\+n(?![A-Za-z])/.test(normalized)
    && /\$\$|\\+\[|\\+\(/.test(normalized)
  ) {
    normalized = normalized
      .replace(/\\+n(?![A-Za-z])/g, "\n")
      .replace(/\\{2,}(?=[A-Za-z[\]()])/g, "\\");
  }

  const lines = normalized.split("\n");
  const output: string[] = [];
  let fence: { marker: "`" | "~"; length: number } | null = null;

  for (const line of lines) {
    const codeFence = line.match(/^ {0,3}(`{3,}|~{3,})/);
    if (codeFence) {
      const marker = codeFence[1][0] as "`" | "~";
      if (!fence) {
        fence = { marker, length: codeFence[1].length };
      } else if (marker === fence.marker && codeFence[1].length >= fence.length) {
        fence = null;
      }
      output.push(line);
      continue;
    }

    if (!fence) {
      const oneLineDisplay = line.match(/^( {0,3})\$\$\s*(\S(?:.*?\S)?)\s*\$\$\s*$/);
      if (oneLineDisplay) {
        const indent = oneLineDisplay[1];
        output.push(`${indent}$$`, `${indent}${oneLineDisplay[2]}`, `${indent}$$`);
        continue;
      }
    }

    output.push(line);
  }

  return output.join("\n");
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
