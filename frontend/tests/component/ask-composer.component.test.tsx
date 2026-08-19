import { useState } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test, vi } from "vitest";

import { AskComposer } from "../../app/ask-composer";


function ControlledComposer({
  onSubmit,
  onAbort = () => undefined,
  running = false,
  disabled = false,
  submitBlocked = false,
}: {
  onSubmit: () => void;
  onAbort?: () => void;
  running?: boolean;
  disabled?: boolean;
  submitBlocked?: boolean;
}) {
  const [value, setValue] = useState("question");
  return (
    <AskComposer
      value={value}
      placeholder="提问"
      onChange={setValue}
      onSubmit={onSubmit}
      onAbort={onAbort}
      running={running}
      disabled={disabled}
      submitBlocked={submitBlocked}
    >
      <span>2 个来源</span>
    </AskComposer>
  );
}


describe("AskComposer", () => {
  test("Enter submits while Shift+Enter inserts a newline", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(<ControlledComposer onSubmit={onSubmit} />);
    const input = screen.getByRole("textbox", { name: "提问" });

    await user.type(input, "{Shift>}{Enter}{/Shift}");
    expect(onSubmit).not.toHaveBeenCalled();
    expect(input).toHaveValue("question\n");

    await user.type(input, "{Enter}");
    expect(onSubmit).toHaveBeenCalledOnce();
  });

  test("running state locks input and turns send into interrupt", async () => {
    const user = userEvent.setup();
    const onAbort = vi.fn();
    render(
      <ControlledComposer
        onSubmit={() => undefined}
        onAbort={onAbort}
        running
      />,
    );

    expect(screen.getByRole("textbox", { name: "提问" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "中断生成" }));
    expect(onAbort).toHaveBeenCalledOnce();
  });

  test("submitBlocked stops BOTH送出路径，但输入框保持可编辑", async () => {
    // 提问超出 ASK_INPUT_LIMITS.questionMaxChars 时的形态。两条提交路径都要挡住：
    // 只 gate 发送键会让超限的问题从 Enter 照样发出去（Enter 的 handler 在组件内，
    // 调用方够不着）。输入框**必须**保持可编辑——用户此刻正要做的就是把它改短，
    // 这也是它与 `disabled` 分成两个 prop 的全部理由。
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(<ControlledComposer onSubmit={onSubmit} submitBlocked />);

    const input = screen.getByRole("textbox", { name: "提问" });
    expect(input).not.toBeDisabled();

    const send = screen.getByRole("button", { name: "发送" });
    expect(send).toBeDisabled();
    await user.click(send);
    expect(onSubmit).not.toHaveBeenCalled();

    await user.type(input, "{Enter}");
    expect(onSubmit).not.toHaveBeenCalled();

    // 空转保护：同一组件在未被拦时两条路都照常提交（见首个用例的 Enter，
    // 这里补按钮那条），否则一个恒禁用的实现也能过上面全部断言。
    render(<ControlledComposer onSubmit={onSubmit} />);
    await user.click(screen.getAllByRole("button", { name: "发送" })[1]);
    expect(onSubmit).toHaveBeenCalledOnce();
  });

  test("disabled locks input and send even with text present", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(<ControlledComposer onSubmit={onSubmit} disabled />);

    // 硬约束(无来源且无参考库):即便已有文本，输入框与发送键都被锁死。
    expect(screen.getByRole("textbox", { name: "提问" })).toBeDisabled();

    const send = screen.getByRole("button", { name: "发送" });
    expect(send).toBeDisabled();
    await user.click(send);
    expect(onSubmit).not.toHaveBeenCalled();
  });
});
