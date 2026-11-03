import { collectSavedAnswerFlags, type AnswerMemoryFlag } from "./memory-model.ts";
import type { AnswerMemoryLinksResponse } from "./workspace-model.ts";

// A finite follow-up window for the detached suggestion job, not continuous polling.
export const ANSWER_MEMORY_REFRESH_DELAYS_MS = [1000, 3000, 10000] as const;

/** Shell-owned Answer→Memory observation. The caller retains actor/epoch publication guards. */
export function observeAnswerMemoryFlags({ batches, loadBatch, signal, followSuggestions, publish, reportError }: {
  batches: string[][];
  loadBatch: (ids: string[]) => Promise<AnswerMemoryLinksResponse>;
  signal: AbortSignal;
  followSuggestions: boolean;
  publish: (flags: Record<string, AnswerMemoryFlag>) => void;
  reportError: (cause: unknown) => void;
}) {
  let timer: ReturnType<typeof setTimeout> | undefined;
  let inFlight = false;
  let attempt = 0;
  async function refresh(follow: boolean) {
    if (signal.aborted || inFlight) return;
    inFlight = true;
    let unresolved = true;
    try {
      if (followSuggestions && document.hidden) return;
      const flags = await collectSavedAnswerFlags(batches, loadBatch, signal);
      if (flags === null || signal.aborted) return;
      publish(flags);
      unresolved = batches.some((batch) => batch.some((id) => !flags[id]));
    } catch (cause) {
      if (!signal.aborted) reportError(cause);
    } finally {
      inFlight = false;
      if (follow && unresolved && !signal.aborted && attempt < ANSWER_MEMORY_REFRESH_DELAYS_MS.length) {
        timer = setTimeout(() => { void refresh(true); }, ANSWER_MEMORY_REFRESH_DELAYS_MS[attempt++]);
      }
    }
  }
  const visible = () => { if (!document.hidden) void refresh(false); };
  const stop = () => {
    clearTimeout(timer);
    document.removeEventListener("visibilitychange", visible);
    signal.removeEventListener("abort", stop);
  };
  if (signal.aborted) return;
  signal.addEventListener("abort", stop, { once: true });
  if (followSuggestions) document.addEventListener("visibilitychange", visible);
  void refresh(followSuggestions);
}
