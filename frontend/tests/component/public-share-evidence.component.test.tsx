import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test } from "vitest";
import { PublicEvidence } from "../../app/public-evidence";
import { ImportedShareText } from "../../app/imported-share-text";

test("原文按需展开，引用段落在上下文中高亮，表格仍可读", async () => {
  const user = userEvent.setup();
  render(<PublicEvidence evidence={{ kind: "original", text: "前文。证据句。后文。\n\n| A | B |\n|---|---|\n| 1 | 2 |", quote: "证据句。", location: "p. 2", truncated: true }} />);
  expect(screen.queryByText("p. 2")).toBeNull();
  await user.click(screen.getByRole("button", { name: "查看原文" }));
  expect(screen.getByText("证据句。").tagName).toBe("MARK");
  expect(screen.getByText("p. 2")).toBeInTheDocument();
  expect(screen.getByRole("table")).toBeInTheDocument();
  expect(screen.getByText("上下文过长，此处仅展示部分内容。")).toBeInTheDocument();
});

test("知识解释不冒充文档原文，旧分享没有原文时如实说明", async () => {
  const user = userEvent.setup();
  const { rerender } = render(<PublicEvidence />);
  expect(screen.getByText("原文已不可用")).toBeInTheDocument();
  rerender(<PublicEvidence evidence={{ kind: "knowledge", text: "融合定义", quote: "", location: "", truncated: false }} />);
  await user.click(screen.getByRole("button", { name: "查看知识解释" }));
  expect(screen.getByText("融合定义")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "查看原文" })).toBeNull();
});

test("导入后不同轮次同号引用仍指向各自的证据", async () => {
  const user = userEvent.setup();
  render(<ImportedShareText text="第一轮 [t1-k1]，第二轮 [t2-k1]。" references={[
    { key: "t1-k1", title: "第一份文献", snippet: "证据甲" },
    { key: "t2-k1", title: "第二份文献", snippet: "证据乙" },
  ]} />);
  await user.click(screen.getByRole("button", { name: "查看引用 t2-k1" }));
  expect(screen.getByText("证据乙")).toBeInTheDocument();
  expect(screen.queryByText("证据甲")).toBeNull();
});

test("保存副本保留核对失败警示，不把它当作可查看的原文", async () => {
  const user = userEvent.setup();
  render(<ImportedShareText text="结论 [t1-k1]" references={[
    { key: "t1-k1", title: "旧资料", snippet: "当时摘录", verification: "source_gone",
      evidence: { kind: "original", text: "不应打开", quote: "", location: "", truncated: false } },
  ]} />);
  await user.click(screen.getByRole("button", { name: "查看引用 t1-k1" }));
  expect(screen.getByText("未通过核对：资料已删除")).toBeInTheDocument();
  expect(screen.getByText("当时摘录")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "查看原文" })).toBeNull();
});
