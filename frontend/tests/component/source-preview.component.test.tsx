import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import { SourcePreview } from "../../app/source-preview";
import { SourcePreviewLink } from "../../app/source-preview-link";
import type { SourceElement } from "../../app/workspace-model";

const element = (id: string, text: string, element_type = "paragraph", metadata = {}): SourceElement => ({
  id, text, element_type, metadata, source_id: "s", location_label: "PDF p.1 block 3",
});

test("source preview renders rich Markdown, safe links, formulas and technical ranges", () => {
  const { container } = render(<SourcePreview notebookId="n" elements={[
    element("one", "## 标题\n\n**重点** 与 ~~旧版~~，7~5nm，80~90%\n\n- 一项\n- 二项\n\n> 引用\n\n| A | B |\n| - | - |\n| 1 | 2 |\n\n```js\nconst x = 1;\n```\n\n$$x^2$$\n\n[危险](javascript:alert(1))"),
  ]} />);
  expect(screen.getByRole("heading", { name: "标题" })).toBeInTheDocument();
  expect(screen.getByText("重点").tagName).toBe("STRONG");
  expect(container.querySelectorAll("del")).toHaveLength(1);
  expect(container.textContent).toContain("7~5nm，80~90%");
  expect(screen.getAllByRole("listitem")).toHaveLength(2);
  expect(screen.getByRole("table")).toBeInTheDocument();
  expect(container.querySelector("blockquote")).toHaveTextContent("引用");
  expect(container.querySelector("pre code")).toHaveTextContent("const x = 1;");
  expect(container.querySelector(".katex-display")).not.toBeNull();
  expect(screen.getByText("危险")).not.toHaveAttribute("href", expect.stringContaining("javascript:"));
});

test("source reading preserves page dividers, anchors, headings, raw code and image descriptions", () => {
  const { container } = render(<SourcePreview notebookId="n" highlightedElementId="head" elements={[
    element("head", "**文档标题**", "heading", { page_number: 2, heading_level: 1 }),
    element("code", "**literal**", "code_block"),
    element("image", "", "image", { description: "说明\n```spice\nR1 1 0 1k\n```" }),
  ]} />);
  expect(screen.getByText("第 2 页")).toBeInTheDocument();
  expect(screen.getByRole("heading", { level: 3 })).toHaveTextContent("文档标题");
  expect(container.querySelector("#source-element-head")).toHaveClass("source-element-card--highlighted");
  expect(screen.queryByText("PDF p.1 block 3")).toBeNull();
  expect(container.querySelector("pre code")).toHaveTextContent("**literal**");
  expect(container.querySelector('pre[data-language="spice"]')).toHaveTextContent("R1 1 0 1k");
});

test("fullscreen entry opens an authenticated reader in a separate tab with active notebook context", () => {
  render(<SourcePreviewLink notebookId="active" sourceId="mounted" />);
  const link = screen.getByRole("link", { name: "全屏阅读" });
  expect(link).toHaveAttribute("href", "/notebooks/active/sources/mounted");
  expect(link).toHaveAttribute("target", "_blank");
  expect(link).toHaveAttribute("rel", "noopener noreferrer");
});

test("parsed normalized text uses preserved Markdown for rich previews", () => {
  render(<SourcePreview notebookId="n" elements={[
    element("heading", "标题", "heading", { heading_level: 1, preview_markdown: "# **标题**" }),
    element("p", "重点与链接", "paragraph", { preview_markdown: "**重点** 与 [链接](https://example.com)" }),
    element("t", "A | B ; 1 | 2", "table", { preview_markdown: "| A | B |\n| --- | --- |\n| 1 | 2 |" }),
  ]} />);
  expect(screen.getByRole("heading", { level: 3 })).toHaveTextContent("标题");
  expect(screen.getByText("重点").tagName).toBe("STRONG");
  expect(screen.getByRole("link", { name: "链接" })).toHaveAttribute("href", "https://example.com");
  expect(screen.getByRole("table")).toBeInTheDocument();
});

test("source Markdown keeps image targets as text and Setext headings below the page title", () => {
  const { container } = render(<SourcePreview notebookId="n" elements={[
    element("setext", "标题", "heading", { preview_markdown: "标题\n===\n" }),
    element("mixed", "正文与图", "paragraph", { preview_markdown: "**正文** ![图注](https://images.example.com/a.png)" }),
    element("ref", "链接", "paragraph", { preview_markdown: "[链接][ref]\n\n[ref]: https://example.com" }),
  ]} />);
  expect(screen.getByRole("heading", { level: 3 })).toHaveTextContent("标题");
  expect(container.querySelector("img")).toBeNull();
  expect(screen.getByText("图注")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "链接" })).toHaveAttribute("href", "https://example.com");
});

test("imported share paragraphs keep formatting and their citation control", () => {
  render(<SourcePreview notebookId="n" elements={[
    element("shared", "重点 [t1-k1]", "paragraph", {
      preview_markdown: "**重点** [t1-k1]",
      share_references: [{ key: "t1-k1", title: "参考来源", snippet: "原文" }],
    }),
  ]} />);
  expect(screen.getByText("重点").tagName).toBe("STRONG");
  const citation = screen.getByRole("button", { name: "查看引用 t1-k1" });
  expect(citation.closest("p")).toContainElement(screen.getByText("重点"));
  expect(screen.getAllByText("[t1-k1]")).toHaveLength(1);
});

test("separately paginated list items retain their effective numbering", () => {
  render(<SourcePreview notebookId="n" elements={[
    element("one", "First", "list_item", { preview_markdown: "1. First", ordered_list_ordinal: 1 }),
    element("two", "Second", "list_item", { preview_markdown: "1. Second", ordered_list_ordinal: 2 }),
    element("three", "Third", "list_item", { preview_markdown: "1. Third", ordered_list_ordinal: 3 }),
  ]} />);
  expect(screen.getAllByRole("list").map((list) => (list as HTMLOListElement).start)).toEqual([1, 2, 3]);
});

test("imported citation syntax inside code stays literal", () => {
  const { container } = render(<SourcePreview notebookId="n" elements={[
    element("shared", "", "paragraph", { preview_markdown: "`[t1-k1]` 与正文 [t1-k1]",
      share_references: [{ key: "t1-k1", title: "参考来源" }] }),
  ]} />);
  expect(container.querySelector("code")).toHaveTextContent("[t1-k1]");
  expect(screen.getAllByRole("button", { name: "查看引用 t1-k1" })).toHaveLength(1);
});

test("quoted source headings and list ordinals retain their containers", () => {
  const { container } = render(<SourcePreview notebookId="n" elements={[
    element("heading", "Quoted heading", "heading", { heading_level: 2, preview_markdown: "> ## Quoted heading" }),
    element("one", "First", "list_item", { preview_markdown: "> 3. First", ordered_list_ordinal: 3 }),
    element("two", "Second", "list_item", { preview_markdown: "> 1. Second", ordered_list_ordinal: 4 }),
  ]} />);
  const heading = screen.getByRole("heading", { level: 4, name: "Quoted heading" });
  expect(heading.closest("blockquote")).not.toBeNull();
  expect(container.textContent).not.toContain("> ##");
  expect(screen.getAllByRole("list").map((list) => (list as HTMLOListElement).start)).toEqual([3, 4]);
});
