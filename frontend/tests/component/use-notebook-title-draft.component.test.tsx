import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";

import { useNotebookTitleDraft } from "../../app/use-notebook-title-draft";

afterEach(cleanup);

test("an untouched header title follows automatic metadata refresh", () => {
  const { result, rerender } = renderHook(useNotebookTitleDraft, {
    initialProps: { id: "a", name: "第一篇来源" },
  });
  rerender({ id: "a", name: "全部来源" });
  expect(result.current[0]).toBe("全部来源");
});

test("automatic refresh preserves an in-progress manual title, including an empty draft", () => {
  const { result, rerender } = renderHook(useNotebookTitleDraft, {
    initialProps: { id: "a", name: "第一篇来源" },
  });
  act(() => result.current[1]("手动标题"));
  rerender({ id: "a", name: "全部来源" });
  expect(result.current[0]).toBe("手动标题");
  act(() => result.current[1](""));
  rerender({ id: "a", name: "新增来源" });
  expect(result.current[0]).toBe("");
});

test("switching notebooks replaces the old draft and subsequent automatic updates remain visible", () => {
  const { result, rerender } = renderHook(useNotebookTitleDraft, {
    initialProps: { id: "a", name: "旧笔记本" },
  });
  act(() => result.current[1]("尚未保存"));
  rerender({ id: "b", name: "新笔记本" });
  expect(result.current[0]).toBe("新笔记本");
  rerender({ id: "b", name: "新笔记本来源摘要" });
  expect(result.current[0]).toBe("新笔记本来源摘要");
});
