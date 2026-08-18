import { useState } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test, vi } from "vitest";

import {
  BundleChoicePanel,
  BundleReceiptsPanel,
  type BundleChoiceState,
  type BundleReceiptEntry,
} from "../../app/bundle-upload-panels";
import type { InlineReceipt } from "../../app/md-bundle";


function emptyReceipt(): InlineReceipt {
  return { inlined: [], missing: [], unsupported: [], remote: [], noAlt: [] };
}

const CANDIDATES = [
  { path: "docs/intro.md", bytes: new Uint8Array(0) },
  { path: "notes/todo.md", bytes: new Uint8Array(0) },
];

function ControlledChoice({
  onConfirm = () => undefined,
  onCancel = () => undefined,
}: {
  onConfirm?: () => void;
  onCancel?: () => void;
}) {
  const [selected, setSelected] = useState<Set<string>>(new Set(CANDIDATES.map((c) => c.path)));
  const choice: BundleChoiceState = { label: "export.zip", candidates: CANDIDATES, selected };
  return (
    <BundleChoicePanel
      choice={choice}
      onToggle={(path) => setSelected((prev) => {
        const next = new Set(prev);
        if (next.has(path)) next.delete(path); else next.add(path);
        return next;
      })}
      onConfirm={onConfirm}
      onCancel={onCancel}
    />
  );
}


describe("BundleChoicePanel", () => {
  test("列出压缩包名与全部候选，默认全选，可分别取消勾选", async () => {
    const user = userEvent.setup();
    render(<ControlledChoice />);

    expect(screen.getByText(/export\.zip/)).toBeInTheDocument();
    expect(screen.getByText(/里有 2 个 Markdown 文件/)).toBeInTheDocument();
    const checkboxes = screen.getAllByRole("checkbox");
    expect(checkboxes).toHaveLength(2);
    for (const box of checkboxes) expect(box).toBeChecked();
    expect(screen.getByRole("button", { name: "添加所选（2）" })).toBeInTheDocument();

    await user.click(checkboxes[0]);
    expect(checkboxes[0]).not.toBeChecked();
    expect(screen.getByRole("button", { name: "添加所选（1）" })).toBeInTheDocument();
  });

  test("取消勾选到零时确认按钮禁用，不能提交空选择", async () => {
    const user = userEvent.setup();
    render(<ControlledChoice />);
    const checkboxes = screen.getAllByRole("checkbox");
    await user.click(checkboxes[0]);
    await user.click(checkboxes[1]);
    const confirm = screen.getByRole("button", { name: "添加所选（0）" });
    expect(confirm).toBeDisabled();
  });

  test("确认/取消按钮各自触发对应回调", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(<ControlledChoice onConfirm={onConfirm} onCancel={onCancel} />);

    await user.click(screen.getByRole("button", { name: "取消" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "添加所选（2）" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });
});


describe("BundleReceiptsPanel", () => {
  test("空列表不渲染任何东西（关闭弹窗后不留残影）", () => {
    const { container } = render(<BundleReceiptsPanel receipts={[]} onDismiss={() => undefined} />);
    expect(container).toBeEmptyDOMElement();
  });

  test("持久列出每份 md 的配对概览与逐条明细，而不是只发一次性提示", () => {
    const receipts: BundleReceiptEntry[] = [
      {
        key: "a",
        bundleLabel: "export.zip",
        fileName: "intro.md",
        receipt: {
          inlined: [{ src: "a.png", path: "a.png", mime: "image/png", bytes: 10, encodedBytes: 20, alt: "图 A", line: 0 }],
          missing: [{ src: "b.png", resolved: "b.png", suggestions: ["imgs/b.png"], line: 1 }],
          unsupported: [{ src: "c.svg", reason: "unsupported_image_type", path: "c.svg", line: 2 }],
          remote: [{ src: "https://example.com/d.png", line: 3 }],
          noAlt: [{ src: "e.png", path: "e.png", line: 4 }],
        },
      },
    ];
    render(<BundleReceiptsPanel receipts={receipts} onDismiss={() => undefined} />);

    expect(screen.getByText("压缩包/文件夹图片配对结果")).toBeInTheDocument();
    expect(screen.getByText("intro.md")).toBeInTheDocument();
    expect(screen.getByText(/1 张已内联 \/ 1 张未找到 \/ 1 张不支持 \/ 1 张云端链接未拉取/)).toBeInTheDocument();
    expect(screen.getByText(/未找到「b\.png」，近似候选：imgs\/b\.png/)).toBeInTheDocument();
    expect(screen.getByText(/不支持的图片格式/)).toBeInTheDocument();
    expect(screen.getByText(/云端图片链接未拉取/)).toBeInTheDocument();
    expect(screen.getByText(/「e\.png」无图注，上传后无法被检索/)).toBeInTheDocument();
  });

  test("多个 md 各自一段，互不混淆；点击「知道了」触发关闭回调", async () => {
    const user = userEvent.setup();
    const onDismiss = vi.fn();
    const receipts: BundleReceiptEntry[] = [
      { key: "a", bundleLabel: "x.zip", fileName: "one.md", receipt: emptyReceipt() },
      { key: "b", bundleLabel: "x.zip", fileName: "two.md", receipt: emptyReceipt() },
    ];
    render(<BundleReceiptsPanel receipts={receipts} onDismiss={onDismiss} />);

    expect(screen.getByText("one.md")).toBeInTheDocument();
    expect(screen.getByText("two.md")).toBeInTheDocument();
    expect(screen.getAllByText("未在正文中发现本地图片")).toHaveLength(2);

    await user.click(screen.getByRole("button", { name: "知道了" }));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });
});
