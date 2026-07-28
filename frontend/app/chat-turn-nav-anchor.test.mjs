// 提问导航侧栏的锚定守卫。
//
// 背景(实际发生过的遮挡,不是假想):`.chat-turn-nav` 导航的是 `.chat-body` 里的
// 轮次,却曾用 `position: absolute; top: 50%` 对着**整个** `.chat-panel` 居中。
// 面板下方会长出很高的兄弟——问题理解的确认卡 `.ask-intent-review` 最高占 58vh
// ——卡片一出现就把对话区压扁,导轨仍停在面板中央,浮出的提问卡(`right: 26px`,
// 最宽 288px)正好压住确认卡右上角那排 chip。
//
// 正确的锚是网格行:`.chat-panel` 的 `grid-template-rows` 第 2 行就是 `.chat-body`
// (会话抽屉 `.chat-session-popover` 是 absolute 浮层,不占行,所以行号稳定)。
//
// 为什么不用 jsdom 量位置:jsdom 不做 grid/flex 布局,量出来的 rect 恒为 0,据此
// 写断言只会得到稳定的假绿。所以静态断言 CSS 声明本身。
//
// 覆盖边界(如实说明):本守卫钉住「导轨锚在对话区那一行」这一条。它不能证明渲染
// 结果真的不重叠——若日后有人给 `.chat-body` 之外的兄弟也加上 `grid-row: 2`,
// 或改了 `.chat-panel` 的行定义,仍需人工看一眼。
import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  callsIn,
  findFunction,
  jsxElements,
  parseModule,
  variableInitializersIn,
} from "./test/semantic-source.mjs";


const APP_DIR = path.dirname(fileURLToPath(import.meta.url));
const CSS = await readFile(path.join(APP_DIR, "globals.css"), "utf8");


/** 取某条选择器的声明块(只支持本文件用到的单选择器形态)。 */
function ruleBody(selector) {
  const at = CSS.indexOf(`\n${selector} {`);
  assert.notEqual(at, -1, `globals.css 里找不到规则 ${selector}`);
  const open = CSS.indexOf("{", at);
  const close = CSS.indexOf("}", open);
  assert.ok(close > open, `${selector} 的声明块没有闭合`);
  return CSS.slice(open + 1, close);
}


function declaration(selector, property) {
  const match = ruleBody(selector)
    .match(new RegExp(`(?:^|;)\\s*${property}\\s*:\\s*([^;]+)`));
  return match ? match[1].trim() : null;
}


test("提问导航锚在对话区那一行,不对整个面板居中", () => {
  // 对话区是第 2 行:第 1 行是 64px 的面板头。
  assert.match(
    declaration(".chat-panel", "grid-template-rows") ?? "",
    /^64px\s+minmax\(0,\s*1fr\)/,
    "面板行定义变了,导航的 grid-row 锚点需要重新确认",
  );
  assert.equal(declaration(".chat-turn-nav", "grid-row"), "2");

  // 关键的反向断言:一旦改回对整个面板绝对居中,遮挡立刻复现。
  assert.notEqual(
    declaration(".chat-turn-nav", "position"), "absolute",
    "导轨又脱离了网格行,会重新对整个面板居中",
  );
  assert.equal(
    declaration(".chat-turn-nav", "top"), null,
    "导轨不该再用 top 定位——它的纵向位置由所在网格行决定",
  );

  // 浮出的提问卡以导轨为定位父(top 由 JS 按 nav 的 rect 算),导轨必须成为定位上下文。
  assert.equal(declaration(".chat-turn-nav", "position"), "relative");
  assert.equal(declaration(".chat-turn-nav-loupe", "position"), "absolute");
});


test("确认卡确实高到会压到面板中线,遮挡不是假想", () => {
  // 这条钉住上面那个背景仍然成立:确认卡一旦不再是「很高的兄弟」,
  // 锚定守卫的理由就该重写,而不是留着一条没人记得为什么的断言。
  assert.match(declaration(".ask-intent-review", "max-height") ?? "", /58vh/);
});


test("在途 turn 有刻度可跳:导航的判据与渲染它的判据一致", async () => {
  const page = await parseModule("page.tsx");
  // 渲染在途 turn 与喂给导航的问题列表必须同源;只按 asking 算会漏掉理解阶段
  // 那一轮——它同样在 DOM 里、同样带 chatTurnDomId,却没有刻度可跳。
  const [nav] = jsxElements(page, "ChatTurnNav");
  assert.ok(nav, "ChatTurnNav 不见了");
  assert.match(nav.bindings.questions, /^askInFlight && pendingQuestion \?/);
  // askInFlight 本身要覆盖理解阶段的两个状态,否则上面那条对齐是空的。
  const inFlight = variableInitializersIn(page)
    .find((item) => item.name === "askInFlight");
  assert.ok(inFlight, "askInFlight 不见了");
  for (const state of ["intentChecking", "askIntentReview"]) {
    assert.match(inFlight.initializer, new RegExp(state), `askInFlight 漏了 ${state}`);
  }
  assert.ok(callsIn(findFunction(page, "runAsk")).includes("setPendingQuestion"));
});
