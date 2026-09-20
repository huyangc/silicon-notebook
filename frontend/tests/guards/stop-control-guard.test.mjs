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
import test from "node:test";
import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const APP_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../app");
const OWNER = "stop-control.tsx";

async function sourceFiles(dir) {
  const found = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) found.push(...await sourceFiles(full));
    else if (/\.tsx?$/.test(entry.name)) found.push(full);
  }
  return found;
}

/** 该文件从 lucide-react 导入的具名符号（按逗号切开后逐个精确比较，`CheckSquare`
 *  之类的长名字不会被误判成 `Square`）。 */
function lucideImports(source) {
  const names = [];
  for (const match of source.matchAll(/import\s*\{([^}]*)\}\s*from\s*"lucide-react"/g)) {
    for (const part of match[1].split(",")) {
      const name = part.trim().split(/\s+as\s+/)[0];
      if (name) names.push(name);
    }
  }
  return names;
}

test("the stop square is imported in exactly one place", async () => {
  const offenders = [];
  for (const file of await sourceFiles(APP_DIR)) {
    if (path.basename(file) === OWNER) continue;
    if (lucideImports(await readFile(file, "utf8")).includes("Square")) offenders.push(path.relative(APP_DIR, file));
  }
  assert.deepEqual(offenders, [], "draw a stop mark with <StopGlyph /> from stop-control.tsx");
  const owner = await readFile(path.join(APP_DIR, OWNER), "utf8");
  assert.ok(lucideImports(owner).includes("Square"));
});

test("both ask composers take the shared class and glyph", async () => {
  for (const relative of ["ask-composer.tsx", "ask/global-ask-workspace.tsx"]) {
    const source = await readFile(path.join(APP_DIR, relative), "utf8");
    assert.match(source, /STOP_CONTROL_CLASS/, `${relative} must use STOP_CONTROL_CLASS`);
    assert.match(source, /<StopGlyph\b/, `${relative} must render <StopGlyph />`);
  }
});

test("the stop look is declared once", async () => {
  const css = await readFile(path.join(APP_DIR, "globals.css"), "utf8");
  assert.equal(css.match(/^button\.stop-control\s*\{/gm)?.length, 1);
  assert.doesNotMatch(css, /\.send-button\.stop\b/);
});
