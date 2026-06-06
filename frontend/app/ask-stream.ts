export type ReasoningTraceStep = {
  step_type: string;
  summary: string;
  detail: Record<string, unknown>;
};

export type AskStreamEvent<TResponse> =
  | { event: "progress"; step: ReasoningTraceStep }
  | { event: "final"; response: TResponse }
  | { event: "error"; error: string };

export function takeNdjsonLines(buffer: string): { lines: string[]; remainder: string } {
  const parts = buffer.split("\n");
  const remainder = parts.pop() ?? "";
  return {
    lines: parts.map((line) => line.trim()).filter(Boolean),
    remainder,
  };
}
