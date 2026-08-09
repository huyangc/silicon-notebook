import { useState } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test, vi } from "vitest";

import { EffortPicker, type EffortOption } from "../../app/effort-picker";


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

  // 窄屏夹取的接线测试。jsdom 不做布局(所有 rect 全零),所以这里按 class 桩掉
  // getBoundingClientRect —— 桩的是"量到什么",被测的是组件有没有找对裁剪祖先、
  // 有没有把算出来的位置真的写到 popover 上。夹取算术本身在
  // effort-picker-logic.test.mjs 里单测。
  function stubLayout({ chipLeft, panelLeft, panelRight, popoverWidth = 232 }: {
    chipLeft: number; panelLeft: number; panelRight: number; popoverWidth?: number;
  }) {
    const rect = (left: number, width: number) => ({
      left, right: left + width, width, top: 0, bottom: 40, height: 40, x: left, y: 0,
      toJSON: () => undefined,
    }) as DOMRect;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (
      this: HTMLElement,
    ) {
      if (this.classList.contains("effort-picker-popover")) return rect(0, popoverWidth);
      if (this.classList.contains("effort-picker")) return rect(chipLeft, 80);
      if (this.classList.contains("clipping-panel")) return rect(panelLeft, panelRight - panelLeft);
      return rect(0, 0);
    });
  }

  const openedPopoverLeft = (chipLeft: number) => {
    const popover = screen.getByRole("dialog");
    // 组件写的是相对控件根元素的 left；换算回视口坐标才能和面板边界比。
    return chipLeft + Number.parseFloat(popover.style.left);
  };

  test("a popover that would overflow the clipping panel is pulled back inside", async () => {
    // 复现实测那一幕：模式行折行后档位 chip 落到面板左缘附近。
    stubLayout({ chipLeft: 120, panelLeft: 100, panelRight: 900 });
    const user = userEvent.setup();
    render(
      <div className="clipping-panel" style={{ overflow: "hidden" }}>
        <ControlledPicker />
      </div>,
    );

    await user.click(screen.getByRole("button", { name: "检索档位：标准" }));
    const left = openedPopoverLeft(120);
    expect(left).toBeGreaterThanOrEqual(100);
    expect(left + 232).toBeLessThanOrEqual(900);
    // 未夹取时右对齐会落在 200-232 = -32，远在面板之外。
    expect(left).toBe(108);
  });

  test("the clamp reads the clipping ancestor, not just the viewport", async () => {
    // 面板比视口窄得多。只夹视口的实现在这里会放过一个溢出面板的位置。
    stubLayout({ chipLeft: 700, panelLeft: 600, panelRight: 780 });
    const user = userEvent.setup();
    render(
      <div className="clipping-panel" style={{ overflow: "hidden" }}>
        <ControlledPicker />
      </div>,
    );

    await user.click(screen.getByRole("button", { name: "检索档位：标准" }));
    // 180px 面板放不下 232px popover：贴左内缘，至少保住开头可读。
    expect(openedPopoverLeft(700)).toBe(608);
  });

  test("a popover with room to spare keeps the default right-aligned position", async () => {
    stubLayout({ chipLeft: 500, panelLeft: 100, panelRight: 1000 });
    const user = userEvent.setup();
    render(
      <div className="clipping-panel" style={{ overflow: "hidden" }}>
        <ControlledPicker />
      </div>,
    );

    await user.click(screen.getByRole("button", { name: "检索档位：标准" }));
    // 右对齐控件右缘（500 + 80 = 580）——与报告那侧观感一致，不该被挪动。
    expect(openedPopoverLeft(500) + 232).toBe(580);
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
