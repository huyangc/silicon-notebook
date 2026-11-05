import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test } from "vitest";
import { PublicEvidence } from "../../app/public-evidence";
import { ImportedShareText } from "../../app/imported-share-text";
import { PublicCitationPopover } from "../../app/public-citation-popover";

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

test.each([
  { name: "粗体", quote: "**证据句。**", expected: "证据句。", selector: "strong" },
  { name: "跨段", quote: "第一段证据。\n\n第二段证据。", expected: "第一段证据。第二段证据。", selector: "p" },
  { name: "表格", quote: "| 参数 | 数值 |\n|---|---|\n| 功耗 | 20 |", expected: "参数数值功耗20", selector: "table" },
  { name: "行内公式", quote: "$E=mc^2$", expected: null, selector: ".katex" },
  { name: "块级公式", quote: "$$\nE=mc^2\n$$", expected: null, selector: ".katex-display" },
  { name: "行内代码", quote: "`<img src=x>`", expected: "<img src=x>", selector: "code" },
  { name: "围栏代码", quote: "```js\nconst answer = 42;\n```", expected: "const answer = 42;\n", selector: "pre code" },
])("$name 引用在保留原文结构的同时高亮", async ({ quote, expected, selector }) => {
  const user = userEvent.setup();
  const { container } = render(<PublicEvidence evidence={{ kind: "original", text: `未引用的前文。\n\n${quote}\n\n未引用的后文。`, quote, location: "", truncated: false }} />);
  await user.click(screen.getByRole("button", { name: "查看原文" }));
  const marks = Array.from(container.querySelectorAll('mark, [role="mark"]'));
  expect(marks.length).toBeGreaterThan(0);
  expect(container.querySelector(selector)).toBeInTheDocument();
  if (expected !== null) expect(marks.map((mark) => mark.textContent).join("")).toBe(expected);
  else expect(marks.some((mark) => mark.querySelector(selector))).toBe(true);
  expect(marks.some((mark) => mark.textContent?.includes("未引用的"))).toBe(false);
});

test("高亮不会开放原文内的链接和远程图片", async () => {
  const user = userEvent.setup();
  const quote = "[证据](https://example.invalid/private) ![原图](https://example.invalid/image.png)";
  render(<PublicEvidence evidence={{ kind: "original", text: quote, quote, location: "", truncated: false }} />);
  await user.click(screen.getByRole("button", { name: "查看原文" }));
  expect(screen.queryByRole("link")).toBeNull();
  expect(screen.queryByRole("img")).toBeNull();
  expect(screen.getByText("证据")).toBeInTheDocument();
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

test.each([
  { kind: "image_description" as const, legacy: false },
  { kind: "original" as const, legacy: true },
])("公开图片引用将 $kind 证据标为图片说明", async ({ kind, legacy }) => {
  const user = userEvent.setup();
  render(<PublicCitationPopover reference={{
    key: "k1", title: "引用图片", file_name: "figure.pdf", location: "第 2 页", snippet: "解析器生成的图注",
    is_image_reference: legacy,
    evidence: { kind, text: "图中显示功耗趋势。", quote: "功耗趋势", location: "第 2 页", truncated: false },
  }} displayLabel="[1]" anchorRect={new DOMRect(10, 10, 20, 20)} onClose={() => {}} />);
  expect(screen.queryByRole("button", { name: "查看原文" })).toBeNull();
  await user.click(screen.getByRole("button", { name: "查看图片说明" }));
  expect(screen.getByRole("button", { name: "收起图片说明" })).toBeInTheDocument();
  expect(screen.getByText("功耗趋势")).toBeInTheDocument();
  expect(screen.queryByRole("img")).toBeNull();
});

test.each([
  { kind: "image_description", legacy: false },
  { kind: "original", legacy: true },
])("保存的图片引用将 $kind 证据标为图片说明", async ({ kind, legacy }) => {
  const user = userEvent.setup();
  render(<ImportedShareText text="图示 [t1-k1]" references={[
    { key: "t1-k1", title: "引用图片", snippet: "解析器生成的图注", is_image_reference: legacy,
      evidence: { kind, text: "图中显示功耗趋势。", quote: "功耗趋势", location: "", truncated: false } },
  ]} />);
  await user.click(screen.getByRole("button", { name: "查看引用 t1-k1" }));
  expect(screen.queryByRole("button", { name: "查看原文" })).toBeNull();
  await user.click(screen.getByRole("button", { name: "查看图片说明" }));
  expect(screen.getByText("功耗趋势")).toBeInTheDocument();
});
