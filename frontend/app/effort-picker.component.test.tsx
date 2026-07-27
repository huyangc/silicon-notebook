import { useState } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test, vi } from "vitest";

import { EffortPicker, type EffortOption } from "./effort-picker";


const OPTIONS: readonly EffortOption[] = [
  { id: "overview", label: "概览", hint: "快速确认方向" },
  { id: "standard", label: "标准", hint: "日常问答默认" },
  { id: "deep", label: "深入", hint: "多方向查证" },
];


function ControlledPicker({
  onChange = () => undefined,
  disabled = false,
}: {
  onChange?: (id: string) => void;
  disabled?: boolean;
}) {
  const [value, setValue] = useState("standard");
  return (
    <EffortPicker
      chipLabel="档位"
      title="检索档位"
      options={OPTIONS}
      value={value}
      onChange={(id) => { setValue(id); onChange(id); }}
      disabled={disabled}
    />
  );
}


describe("EffortPicker", () => {
  test("chip shows the current grade and opens the slider popover", async () => {
    const user = userEvent.setup();
    render(<ControlledPicker />);

    const chip = screen.getByRole("button", { name: "检索档位：标准" });
    expect(screen.queryByRole("dialog")).toBeNull();

    await user.click(chip);
    const popover = screen.getByRole("dialog", { name: "检索档位" });
    expect(popover).toBeInTheDocument();

    const slider = screen.getByRole("slider", { name: "检索档位" });
    // 滑块位置是档位下标(0..n-1),不是档位 id。
    expect(slider).toHaveValue("1");
    expect(slider).toHaveAttribute("aria-valuetext", "标准");
    expect(screen.getByText("日常问答默认")).toBeInTheDocument();
  });

  test("dragging the slider reports the option id, not the index", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<ControlledPicker onChange={onChange} />);

    await user.click(screen.getByRole("button", { name: "检索档位：标准" }));
    // jsdom 不实现 range 的拖拽/方向键换档;直接派发 change,与真实拖动同一条路径。
    fireEvent.change(screen.getByRole("slider", { name: "检索档位" }), { target: { value: "2" } });

    expect(onChange).toHaveBeenCalledWith("deep");
    expect(screen.getByRole("button", { name: "检索档位：深入" })).toBeInTheDocument();
    expect(screen.getByText("多方向查证")).toBeInTheDocument();
  });

  test("Escape and outside clicks close the popover", async () => {
    const user = userEvent.setup();
    render(<><ControlledPicker /><button type="button">外部</button></>);

    await user.click(screen.getByRole("button", { name: "检索档位：标准" }));
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();

    await user.click(screen.getByRole("button", { name: "检索档位：标准" }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "外部" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  test("disabled locks the chip, and going disabled while open closes the popover", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<ControlledPicker />);

    await user.click(screen.getByRole("button", { name: "检索档位：标准" }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    // 提交开始时控件转禁用:popover 必须自己收起,否则它会浮在屏幕上且再也点不掉。
    rerender(<ControlledPicker disabled />);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByRole("button", { name: "检索档位：标准" })).toBeDisabled();
  });

  test("an unknown stored grade falls back to the first option instead of crashing", () => {
    render(
      <EffortPicker
        chipLabel="档位"
        title="检索档位"
        options={OPTIONS}
        value="retired-grade"
        onChange={() => undefined}
      />,
    );
    expect(screen.getByRole("button", { name: "检索档位：概览" })).toBeInTheDocument();
  });
});
