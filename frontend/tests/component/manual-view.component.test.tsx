import { readFile } from "node:fs/promises";
import path from "node:path";

import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";

import { ManualView } from "../../app/manual/manual-view.tsx";
import { headingSlug, isRepoDocLink } from "../../app/manual/manual-markdown.ts";

const SAMPLE = `# 使用手册

[返回 README](../README_zh.md) · 详见[产品参考](./product-and-api_zh.md#api)

## 目录

1. [这是什么](#1-这是什么)
2. [沉淀成果：记忆与 Knowhow 表](#2-沉淀成果记忆与-knowhow-表)

## 1. 这是什么

正文，见 [官网](https://example.com/) 与 [下一章](#2-沉淀成果记忆与-knowhow-表)。

## 2. 沉淀成果：记忆与 Knowhow 表

| 列 | 说明 |
| --- | --- |
| a | 2~3 周 |
`;

test("标题 id 与 GitHub 锚点口径一致,目录能跳到正文", () => {
  render(<ManualView markdown={SAMPLE} />);

  expect(screen.getByRole("heading", { level: 2, name: "1. 这是什么" })).toHaveAttribute("id", "1-这是什么");
  expect(screen.getByRole("heading", { level: 2, name: "2. 沉淀成果：记忆与 Knowhow 表" }))
    .toHaveAttribute("id", "2-沉淀成果记忆与-knowhow-表");
  expect(screen.getByRole("link", { name: "下一章" })).toHaveAttribute("href", "#2-沉淀成果记忆与-knowhow-表");
});

test("指向仓库其它 Markdown 文档的链接按纯文本呈现,外链新开标签", () => {
  render(<ManualView markdown={SAMPLE} />);

  expect(screen.queryByRole("link", { name: "返回 README" })).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "产品参考" })).not.toBeInTheDocument();
  expect(screen.getByText("返回 README")).toHaveClass("manual-doc-ref");
  expect(screen.getByRole("link", { name: "官网" })).toHaveAttribute("target", "_blank");
});

test("表格沿用站内表格容器,单个波浪号不是删除线", () => {
  const { container } = render(<ManualView markdown={SAMPLE} />);

  expect(container.querySelector(".answer-table-wrap table.answer-table")).not.toBeNull();
  expect(container.querySelector("del")).toBeNull();
  expect(screen.getByText("2~3 周")).toBeInTheDocument();
});

test("headingSlug / isRepoDocLink 的边界", () => {
  expect(headingSlug("5. 问答模式：自动、高级、通用问答与逐步推理")).toBe("5-问答模式自动高级通用问答与逐步推理");
  expect(headingSlug("  Knowhow  表 ")).toBe("knowhow-表");
  expect(isRepoDocLink("./product-and-api_zh.md#api")).toBe(true);
  expect(isRepoDocLink("#1-这是什么")).toBe(false);
  expect(isRepoDocLink("https://example.com/readme.md")).toBe(true);
  expect(isRepoDocLink(undefined)).toBe(false);
});

// 真手册的每个目录锚点都必须落在某个标题上——手册是手写目录,章节改名、改号后
// 目录若没跟着改,站内页面就会有跳不动的条目。这里按页面自己的 id 口径对账。
test("真手册的目录锚点全部能命中标题", async () => {
  const manualPath = path.resolve(process.cwd(), "..", "docs", "user-manual_zh.md");
  const markdown = await readFile(manualPath, "utf8");
  const headingIds = new Set(
    [...markdown.matchAll(/^#{1,4}\s+(.+?)\s*$/gm)].map((match) => headingSlug(match[1])),
  );
  const anchors = [...markdown.matchAll(/\]\(#([^)]+)\)/g)].map((match) => match[1]);
  expect(anchors.length).toBeGreaterThan(0);
  const missing = anchors.filter((anchor) => !headingIds.has(anchor));
  expect(missing).toEqual([]);
});
