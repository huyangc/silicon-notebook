export function takeNdjsonLines(buffer: string): { lines: string[]; remainder: string } {
  const parts = buffer.split("\n");
  const remainder = parts.pop() ?? "";
  return {
    lines: parts.map((line) => line.trim()).filter(Boolean),
    remainder,
  };
}
