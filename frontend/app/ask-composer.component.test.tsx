import { useState } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test, vi } from "vitest";

import { AskComposer } from "./ask-composer";


function ControlledComposer({
  onSubmit,
  onAbort = () => undefined,
  running = false,
}: {
  onSubmit: () => void;
  onAbort?: () => void;
  running?: boolean;
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
});
