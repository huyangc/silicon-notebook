import { act, renderHook, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { beforeEach, expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({ fetchReleaseNotes: vi.fn() }));

vi.mock("../../app/release-notes-api.ts", () => ({
  fetchReleaseNotes: mocks.fetchReleaseNotes,
}));

import { useReleaseNotes } from "../../app/use-release-notes";

const payload = {
  available: true,
  build: { version: "v1", ordinal: 10 },
  notes: [{ id: "a", ordinal: 9, body: "x" }],
};

beforeEach(() => {
  mocks.fetchReleaseNotes.mockReset();
});

test("有待看说明时给出 notice", async () => {
  mocks.fetchReleaseNotes.mockResolvedValue(payload);
  const { result } = renderHook(() => useReleaseNotes("u1", true));
  await waitFor(() => expect(result.current.notice?.build.ordinal).toBe(10));
  act(() => result.current.clear());
  expect(result.current.notice).toBeNull();
});

test("同一用户已取完的不再取:u1 → u2 → u1 共两次请求", async () => {
  mocks.fetchReleaseNotes.mockResolvedValue(payload);
  const { result, rerender } = renderHook(
    ({ id }) => useReleaseNotes(id, true),
    { initialProps: { id: "u1" } },
  );
  await waitFor(() => expect(result.current.notice).not.toBeNull());
  rerender({ id: "u2" });
  await waitFor(() => expect(mocks.fetchReleaseNotes).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(result.current.notice).not.toBeNull());
  rerender({ id: "u1" });
  await act(async () => { await Promise.resolve(); });
  expect(mocks.fetchReleaseNotes).toHaveBeenCalledTimes(2);
  expect(result.current.notice).toBeNull();
});

test("StrictMode 重挂:起手就已登录也最终给出 notice", async () => {
  mocks.fetchReleaseNotes.mockResolvedValue(payload);
  const { result } = renderHook(() => useReleaseNotes("u1", true), { wrapper: StrictMode });
  await waitFor(() => expect(result.current.notice?.build.ordinal).toBe(10));
});

test("u1 → 空 → u1 且首个请求还在途:被取消的不算已问,会再取一次并显示", async () => {
  let resolveFirst: (value: unknown) => void = () => undefined;
  mocks.fetchReleaseNotes
    .mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve; }))
    .mockResolvedValueOnce(payload);
  const { result, rerender } = renderHook(
    ({ id }) => useReleaseNotes(id, true),
    { initialProps: { id: "u1" as string | null } },
  );
  rerender({ id: null });
  rerender({ id: "u1" });
  await waitFor(() => expect(result.current.notice?.build.ordinal).toBe(10));
  expect(mocks.fetchReleaseNotes).toHaveBeenCalledTimes(2);
  await act(async () => { resolveFirst(payload); await Promise.resolve(); });
  expect(mocks.fetchReleaseNotes).toHaveBeenCalledTimes(2);
});

test("未完成鉴权或未登录时不取", () => {
  mocks.fetchReleaseNotes.mockResolvedValue(payload);
  renderHook(() => useReleaseNotes(null, true));
  renderHook(() => useReleaseNotes("u1", false));
  expect(mocks.fetchReleaseNotes).not.toHaveBeenCalled();
});

test.each([
  ["清单不可用", { available: false, build: null, notes: [] }],
  ["没有待看说明", { available: true, build: { version: "v1", ordinal: 10 }, notes: [] }],
])("%s 时不出 notice", async (_name, response) => {
  mocks.fetchReleaseNotes.mockResolvedValue(response);
  const { result } = renderHook(() => useReleaseNotes("u1", true));
  await waitFor(() => expect(mocks.fetchReleaseNotes).toHaveBeenCalledTimes(1));
  await act(async () => { await Promise.resolve(); });
  expect(result.current.notice).toBeNull();
});

test("取失败静默：不抛错、无 notice", async () => {
  mocks.fetchReleaseNotes.mockRejectedValue(new Error("down"));
  const { result } = renderHook(() => useReleaseNotes("u1", true));
  await waitFor(() => expect(mocks.fetchReleaseNotes).toHaveBeenCalledTimes(1));
  await act(async () => { await Promise.resolve(); });
  expect(result.current.notice).toBeNull();
});

test("切换用户重新取，并丢弃旧用户在途结果", async () => {
  let resolveFirst: (value: unknown) => void = () => undefined;
  mocks.fetchReleaseNotes
    .mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve; }))
    .mockResolvedValueOnce({ ...payload, build: { version: "v2", ordinal: 20 } });
  const { result, rerender } = renderHook(
    ({ id }) => useReleaseNotes(id, true),
    { initialProps: { id: "u1" } },
  );
  rerender({ id: "u2" });
  await waitFor(() => expect(result.current.notice?.build.ordinal).toBe(20));
  await act(async () => { resolveFirst(payload); await Promise.resolve(); });
  expect(result.current.notice?.build.ordinal).toBe(20);
  expect(mocks.fetchReleaseNotes).toHaveBeenCalledTimes(2);
});
