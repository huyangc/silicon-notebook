import { afterEach, expect, test, vi } from "vitest";
import { observeAnswerMemoryFlags } from "../../app/answer-memory-refresh";
import type { AnswerMemoryLinksResponse } from "../../app/workspace-model";

afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

test("aborting an observer discards a late candidate and removes visibility and timed reads", async () => {
  vi.useFakeTimers();
  const controller = new AbortController();
  const result = deferred<AnswerMemoryLinksResponse>();
  const loadBatch = vi.fn(() => result.promise);
  const publish = vi.fn();
  observeAnswerMemoryFlags({ batches: [["answer-1"]], loadBatch, signal: controller.signal, followSuggestions: true, publish, reportError: vi.fn() });
  expect(loadBatch).toHaveBeenCalledTimes(1);
  controller.abort();
  result.resolve({ links: {}, states: { "answer-1": { memory_id: "candidate-1", status: "candidate", suggested: true } } });
  await vi.advanceTimersByTimeAsync(60000);
  document.dispatchEvent(new Event("visibilitychange"));
  expect(publish).not.toHaveBeenCalled();
  expect(loadBatch).toHaveBeenCalledTimes(1);
});

test("hidden documents defer suggestion reads until visible, with no parallel lookups", async () => {
  vi.useFakeTimers();
  const hidden = vi.spyOn(document, "hidden", "get").mockReturnValue(true);
  const controller = new AbortController();
  const result = deferred<AnswerMemoryLinksResponse>();
  const loadBatch = vi.fn(() => result.promise);
  const publish = vi.fn();
  observeAnswerMemoryFlags({ batches: [["answer-1"]], loadBatch, signal: controller.signal, followSuggestions: true, publish, reportError: vi.fn() });
  await vi.advanceTimersByTimeAsync(60000);
  expect(loadBatch).not.toHaveBeenCalled();
  hidden.mockReturnValue(false);
  document.dispatchEvent(new Event("visibilitychange"));
  document.dispatchEvent(new Event("visibilitychange"));
  expect(loadBatch).toHaveBeenCalledTimes(1);
  result.resolve({ links: {}, states: { "answer-1": { memory_id: "candidate-1", status: "candidate", suggested: true } } });
  await vi.advanceTimersByTimeAsync(0);
  expect(publish).toHaveBeenCalledWith({ "answer-1": "suggested" });
  controller.abort();
});
