import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { jsxElements, parseModule } from "../../test-support/semantic-source.mjs";

// PageHeader 用的是主界面同款 .topbar(position: sticky)。站内页的锚点目标若只留很小的
// scroll-margin-top,跳转后标题会落在吸顶顶栏背后看不见(codex R3 P2)。手册目录是唯一的
// 锚点页。样式表没有可消费的 AST,jsdom 也不做滚动布局,文本是唯一诚实的输入;
// PageHeader 的结构断言走 semantic-source。
const APP_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../app");

async function stylesheet(relativePath) {
  return (await readFile(path.join(APP_DIR, relativePath), "utf8")).replace(/\/\*[\s\S]*?\*\//g, "");
}

function pxDeclaration(css, pattern, label) {
  const found = pattern.exec(css);
  assert.ok(found, `找不到 ${label}`);
  return Number(found[1]);
}

test("手册标题的锚点偏移不小于吸顶 PageHeader 的高度", async () => {
  const header = await parseModule("components/PageHeader.tsx");
  assert.equal(
    jsxElements(header, "header").some((element) => element.attributes.className === "topbar page-header"),
    true,
    "PageHeader 不再挂 .topbar —— 若已不吸顶,可放宽此守卫",
  );

  const headerCss = await stylesheet("components/page-header.css");
  const manualCss = await stylesheet("manual/manual.css");
  const headerHeight = pxDeclaration(headerCss, /\.page-header\s*\{[^}]*min-height:\s*(\d+)px/, ".page-header 的 min-height");
  const offset = pxDeclaration(manualCss, /scroll-margin-top:\s*(\d+)px/, "manual.css 的 scroll-margin-top");
  assert.ok(
    offset >= headerHeight + 8,
    `手册锚点 scroll-margin-top=${offset}px,吸顶顶栏高 ${headerHeight}px —— 跳转后标题会被顶栏盖住`,
  );
});
