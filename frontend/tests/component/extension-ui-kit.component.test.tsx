import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";

import {
  ExtensionActions,
  ExtensionAlert,
  ExtensionEmptyState,
  ExtensionResultItem,
  ExtensionResultList,
} from "../../features/extension-sdk/ui";


test("ExtensionAlert maps tones to the expected role and class", () => {
  render(
    <>
      <ExtensionAlert tone="error">错误</ExtensionAlert>
      <ExtensionAlert tone="warning">警告</ExtensionAlert>
      <ExtensionAlert tone="status">状态</ExtensionAlert>
    </>,
  );

  const alerts = screen.getAllByRole("alert");
  expect(alerts[0].className).toContain("extension-alert--error");
  expect(alerts[1].className).toContain("extension-alert--warning");
  expect(screen.getByRole("status").className).toContain("extension-alert--status");
});


test("ExtensionResultItem wires checkbox changes as a controlled row", () => {
  const onChange = vi.fn();
  render(
    <ExtensionResultList>
      <ExtensionResultItem
        checkbox={{ checked: false, onChange, ariaLabel: "选择论文 A" }}
        title="论文 A"
      />
    </ExtensionResultList>,
  );

  fireEvent.click(screen.getByRole("checkbox", { name: "选择论文 A" }));
  expect(onChange).toHaveBeenCalledWith(true);
  fireEvent.click(screen.getByText("论文 A"));
  expect(onChange).toHaveBeenLastCalledWith(true);
});


test("ExtensionResultItem renders summary with the clamp class", () => {
  render(
    <ExtensionResultList>
      <ExtensionResultItem
        title="论文 A"
        meta="作者 · 2026-08-25"
        summary="一段会被多行裁切的摘要。"
      />
    </ExtensionResultList>,
  );

  expect(screen.getByText("一段会被多行裁切的摘要。").className).toContain("extension-result-item-summary");
});


test("ExtensionResultItem tolerates missing optional props and ExtensionEmptyState stays stable", () => {
  render(
    <>
      <ExtensionResultList>
        <ExtensionResultItem title="只有标题" />
      </ExtensionResultList>
      <ExtensionActions>
        <button type="button">动作</button>
      </ExtensionActions>
      <ExtensionEmptyState>没有结果</ExtensionEmptyState>
    </>,
  );

  expect(screen.getByText("只有标题")).not.toBeNull();
  expect(screen.getByText("动作")).not.toBeNull();
  expect(screen.getByText("没有结果").className).toContain("extension-empty");
});
