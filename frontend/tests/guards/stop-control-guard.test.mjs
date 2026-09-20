// 「停止」标志全站只有一份的守卫。
//
// 背景：全局问答的停止键曾是带字的黑色药丸（「停止」/「停止中…」/「取消问题理解」），
// 笔记本内问答的却是浅红底的图标键——同一个动作两种长相；带字的那颗比发送键宽一倍，
// 小窗里还会被挤成独占一行。统一之后，停止的图标与外观只从 `app/stop-control.tsx`
// 取，外观只有 globals.css 的 `button.stop-control` 一份。
//
// 断言：
//   1. lucide 的 `Square`（停止方块）只在 stop-control.tsx 里被导入——别处要画停止，
//      只能经 `StopGlyph`；
//   2. 两个问答输入区的停止键都挂 `STOP_CONTROL_CLASS`、内容是 `StopGlyph`；
//   3. 外观规则只有一份，旧的 `.send-button.stop` 不回潮。
//
// tsx 一侧的断言全部走 semantic-source 的语义解析（导入表、JSX 元素），不读裸文本；
// 只有第 3 条读 globals.css——样式表没有可消费的 AST。
import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { appSourceModules, importsFrom, jsxElements } from "../../test-support/semantic-source.mjs";

const APP_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../app");
const OWNER = "stop-control.tsx";
const COMPOSERS = ["ask-composer.tsx", "ask/global-ask-workspace.tsx"];

const importsSquare = (module) => importsFrom(module, "lucide-react").some((item) => item.imported === "Square");

test("the stop square is imported in exactly one place", async () => {
  const modules = await appSourceModules();
  const importers = modules.filter((item) => importsSquare(item.module)).map((item) => item.path);
  assert.deepEqual(importers, [OWNER], "draw a stop mark with <StopGlyph /> from stop-control.tsx");
});

test("both ask composers take the shared class and glyph", async () => {
  const modules = new Map((await appSourceModules()).map((item) => [item.path, item.module]));
  for (const relative of COMPOSERS) {
    const module = modules.get(relative);
    assert.ok(module, `${relative} is missing`);
    const specifier = relative.includes("/") ? "../stop-control" : "./stop-control";
    const imported = importsFrom(module, specifier).map((item) => item.imported);
    assert.deepEqual(imported, ["STOP_CONTROL_CLASS", "StopGlyph"], `${relative} must take both from stop-control`);
    assert.ok(jsxElements(module, "StopGlyph").length > 0, `${relative} must render <StopGlyph />`);
  }
});

test("the stop look is declared once", async () => {
  const css = await readFile(path.join(APP_DIR, "globals.css"), "utf8");
  assert.equal(css.match(/^button\.stop-control\s*\{/gm)?.length, 1);
  assert.doesNotMatch(css, /\.send-button\.stop\b/);
});
