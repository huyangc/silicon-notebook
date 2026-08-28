// 按钮反馈守卫 —— 钉住「按下有变化、松手会还原」这条全站基线，以及它与「结果反馈」
// 的分工。
//
// 缺陷来源:群组「成员」页的邀请链接「复制」按钮。按下去按钮纹丝不动,唯一的反馈是
// 页面**顶部**那条 notice 横幅 —— 在长页面上它常常滚出视口,即使在视口内也远离手指
// 落点。用户看到的是「按钮没反应」,于是判定没生效。同一形态在全站 40 多个按钮类上
// 都成立,所以修法不是给这一颗补样式,而是把按下态写在 `button` **元素**上。
//
// 因此这里钉两条:
//
// 1. **按下态是元素级的**。判据刻意要求选择器以裸 `button` 起头、不含类名:只有元素级
//    的规则才能让**以后新写的**按钮默认就有反馈。有人把它收窄成 `.new-pill:active`
//    仍然「有按下态」,但那条承诺当场失效,所以这里要报红。同时要求它排除 `:disabled`
//    —— 禁用按钮给出按下反馈等于骗用户「点得动」。
//
// 2. **结果态落在按钮自身**。按下态只回答「点上了没有」;复制成功/失败是另一回事,
//    必须出现在按钮自己身上(文案 + 配色),不能只靠那条会滚出视口的横幅。下面那张
//    表逐颗钉住已知的复制入口,连同「到点自己回到 idle」——结果态是 JS 状态,不像
//    :active 那样松手自动还原,忘了摘掉就会一直挂着,下一次点击反而看不出变化。
//    计时只有一份(`copy-result.ts` 的 `useCopyResult`),所以每个调用点还必须证明它
//    用的是那一份,而不是又抄了一个自己的定时器。
//
// 判据用 onClick 绑定的**源码文本**作身份,不用行号:按钮在文件里上下挪动不该报红。
// className 必须留字面量(不能拼模板串),否则这里与 group-page-style-guard 都看不见
// 它——这也是 `useCopyResult` 刻意只做 hook、不做组件的原因。样式表那侧只能读文本 ——
// CSS 没有可消费的 AST,jsdom 也不做特异性级联(与 group-page-style-guard /
// effort-picker-style-guard 同一条登记)。
//
// 覆盖边界(如实说明,不声称全覆盖):本守卫只证明「元素级按下态存在且是视觉变化」与
// 「表里这几颗复制按钮的结果态形态」。它**不**逐个按钮验证渲染后的实际观感,也不保证
// 别处**新写**的复制/保存按钮记得给结果态 —— 那类兜底是 `AGENTS.md` 的交互反馈规则
// 加代码评审,不是这份测试。
import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import ts from "typescript";

import { importsIn, jsxElements, parseModule } from "../../test-support/semantic-source.mjs";


const APP_DIR = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../app",
);
// 注释先剥掉,免得注释里写过的选择器冒充规则。
const STYLESHEET = (await readFile(path.join(APP_DIR, "globals.css"), "utf8"))
  .replace(/\/\*[\s\S]*?\*\//g, "");

/** 扁平扫出 (选择器, 声明体)。选择器组按逗号拆开,`@` 开头的块整体跳过 —— 它们多一层
 *  花括号,这个扫描读不了,而全站基线本来也不该藏在条件块里。 */
function styleRules() {
  const found = [];
  const pattern = /([^{}]+)\{([^{}]*)\}/g;
  let match;
  while ((match = pattern.exec(STYLESHEET)) !== null) {
    const selectors = match[1].trim();
    if (selectors.startsWith("@")) continue;
    for (const one of selectors.split(",")) {
      found.push({ selector: one.trim(), body: match[2] });
    }
  }
  return found;
}

const RULES = styleRules();

/** 元素级按下态:裸 `button` 起头,后面只允许接 `:not(...)` 与 `:active`。带类名的
 *  变体(`.new-pill:active`)不算 —— 那正是这条守卫要拦的收窄。 */
const ELEMENT_PRESSED = /^button(?::not\([^)]*\))*:active$/;

/** 一条声明体里出现的属性名。 */
function declaredProperties(body) {
  const names = new Set();
  for (const one of body.split(";")) {
    const colon = one.indexOf(":");
    if (colon <= 0) continue;
    const name = one.slice(0, colon).trim();
    if (name) names.add(name);
  }
  return names;
}

test("按下态写在 button 元素上，新按钮默认就有", () => {
  const pressed = RULES.filter((rule) => ELEMENT_PRESSED.test(rule.selector));
  assert.equal(
    pressed.length,
    1,
    "globals.css 里应当**恰好有一条**元素级的 button:active 规则,实际"
    + `${pressed.length} 条(选择器:${pressed.map((rule) => rule.selector).join("、") || "无"})`
    + " —— 收窄成类选择器、或散成多条各说各话的基线,都会让新写的按钮重新失去按下反馈",
  );
});

test("按下态排除禁用按钮", () => {
  const [pressed] = RULES.filter((rule) => ELEMENT_PRESSED.test(rule.selector));
  assert.ok(pressed, "没有元素级 button:active 规则(上一条断言已解释后果)");
  assert.match(
    pressed.selector,
    /:not\(:disabled\)/,
    `按下态选择器 ${pressed.selector} 没有排除 :disabled`
    + " —— 禁用按钮给出按下反馈,等于告诉用户「点得动」",
  );
});

test("按下态是真的视觉变化，而不是空规则", () => {
  const [pressed] = RULES.filter((rule) => ELEMENT_PRESSED.test(rule.selector));
  assert.ok(pressed, "没有元素级 button:active 规则(上一条断言已解释后果)");
  const properties = declaredProperties(pressed.body);
  const visual = ["translate", "scale", "rotate", "opacity", "filter", "background", "box-shadow"]
    .filter((name) => properties.has(name));
  assert.ok(
    visual.length >= 2,
    `按下态只声明了 ${[...properties].join("、") || "空"}`
    + " —— 需要至少两项视觉变化:单看位移在整块卡片按钮上几乎不可见,单看变淡在浅色"
    + "描边按钮上同样弱,两者叠加才对深浅两种底色都成立",
  );
});

test("按下态不用 transform 简写，免得替换掉按钮自己的定位", () => {
  // 真实缺陷(codex #612 R1 P2):`.answer-image-preview-step`(多图预览的上一张/下一张)
  // 靠 `top: 50%` + `transform: translateY(-50%)` 垂直居中。全站基线一旦写 transform
  // 简写,就把那条居中整条替换掉——44px 的绝对定位控件在按下的瞬间往下跳约 22px,
  // 靠近上缘按下时它会从指针底下挪走,连这一次点击都取消。
  //
  // 判据钉的是**形态**而不是那一颗按钮:任何拿 transform 定位自己的按钮都会中招,
  // 而 translate/scale/rotate 这三个独立属性按 translate → rotate → scale → transform
  // 的次序合成,各按钮自己的 transform 原样保留。
  const [pressed] = RULES.filter((rule) => ELEMENT_PRESSED.test(rule.selector));
  assert.ok(pressed, "没有元素级 button:active 规则(上一条断言已解释后果)");
  assert.equal(
    declaredProperties(pressed.body).has("transform"),
    false,
    "全站按下态用了 transform 简写 —— 它会替换掉按钮自己的定位 transform"
    + "(如 .answer-image-preview-step 的 translateY(-50%)),按下时控件会跳位甚至"
    + "从指针底下挪走。改用 translate / scale 独立属性,它们与既有 transform 合成",
  );
});

// onClick 源码文本里能唯一认出这个入口的片段 → 它是哪一颗、为什么结果不能只发横幅。
//
// ⚠ 表里刻意**不**收 `toggleShare`(报告的「分享」)与 `announceShareLink` 的另一个
// 调用方:那一颗按下后整排控件会换样(「分享」→「取消分享」,并多出一颗「复制链接」),
// 「有没有发生事情」已经答得很清楚,再叠一层结果态只会打架。
const COPY_BUTTONS = [
  {
    module: "groups-page.tsx",
    match: "copy-invite",
    why: "群组邀请链接:结果曾经只发页面顶部 notice 横幅,长页面上它会滚出视口",
  },
  {
    module: "page.tsx",
    match: "copyShareLink()",
    why: "笔记本分享弹窗里的链接:结果曾经只发 toast",
  },
  {
    module: "page.tsx",
    match: "handleShareLinkCopy(item.share_token",
    why: "「已分享」弹窗每行的链接:同一个 handler,结果还必须按行分格(否则整列一起变绿)",
  },
  {
    module: "report-view.tsx",
    match: "copyShareLink()",
    why: "报告分享链接:复制发生在跨域 effect 里,结果原路带回来画在按钮上",
  },
  {
    module: "report-view.tsx",
    match: "copyReportContent(",
    why: "报告正文:失败此前被 .catch 吞掉,按钮纹丝不动",
  },
];

const MODULES = new Map();
for (const name of new Set(COPY_BUTTONS.map((entry) => entry.module))) {
  MODULES.set(name, await parseModule(name));
}
const SHARED = await parseModule("copy-result.ts");

for (const entry of COPY_BUTTONS) {
  test(`${entry.module} 的复制按钮(${entry.match})把结果画在自己身上`, () => {
    const buttons = jsxElements(MODULES.get(entry.module), "button")
      .filter((element) => element.bindings?.onClick?.includes(entry.match));
    assert.equal(
      buttons.length,
      1,
      `${entry.module} 里 onClick 命中 "${entry.match}" 的 button 应有 1 颗,`
      + `实际 ${buttons.length} 颗(${entry.why})`,
    );
    const className = buttons[0].bindings?.className ?? "";
    for (const token of ["copy-result-copied", "copy-result-failed"]) {
      assert.ok(
        className.includes(token),
        `${entry.module} 的这颗复制按钮 className 里没有字面量 ${token} —— `
        + `${entry.why}。注意 class 必须写成字面量,拼模板串这里就看不见了`,
      );
    }
  });
}

test("复制结果的两种配色在样式表里真的有规则", () => {
  for (const token of ["copy-result-copied", "copy-result-failed"]) {
    const matched = RULES.filter((one) => one.selector.includes(`.${token}`));
    assert.ok(
      matched.length > 0,
      `globals.css 里没有 .${token} 规则,按钮会挂着一个不存在的类裸奔`,
    );
    assert.ok(
      matched.some((one) => declaredProperties(one.body).has("background")),
      `.${token} 没有任何一条规则改 background —— 结果态只剩文案,弱到与「没反应」难以区分`,
    );
    // `.report-action:hover:not(:disabled)` 是 (0,3,0),会在刚点完、指针还停在按钮上
    // 的那一秒把底色改回浅灰。结果态必须有一条同样带 :hover 的选择器压回来。
    assert.ok(
      matched.some((one) => one.selector.includes(":hover")),
      `.${token} 没有 :hover 变体 —— 点完指针必然停在按钮上,悬停态会吃掉结果配色`,
    );
  }
});

test("回到 idle 的计时只有一份，调用点都用它", () => {
  // :active 松手自动还原;JS 状态不会,忘了摘掉就一直挂着「已复制」,下一次点击反而
  // 看不出变化。判据是「共享模块里有一个 setTimeout 把它设回 idle」,不钉具体毫秒数
  // (那是取舍,不是不变量),再加上每个调用点确实 import 了它。
  const resets = [];
  const walk = (node) => {
    if (
      ts.isCallExpression(node)
      && /(^|\.)setTimeout$/.test(node.expression.getText(SHARED))
      && node.arguments.length > 0
      && /"idle"/.test(node.arguments[0].getText(SHARED))
    ) {
      resets.push(node);
    }
    node.forEachChild(walk);
  };
  walk(SHARED);
  assert.ok(
    resets.length > 0,
    "copy-result.ts 里没有把结果设回 idle 的 setTimeout 回调 —— 「已复制」会一直挂在"
    + "按钮上,下一次点击就再也看不出有没有点上",
  );

  for (const name of MODULES.keys()) {
    const uses = importsIn(MODULES.get(name)).some(
      (item) => item.module.includes("copy-result") && item.imported === "useCopyResult",
    );
    assert.ok(
      uses,
      `${name} 没有从 copy-result 引入 useCopyResult —— 各写各的定时器,`
      + "「结果停留多久」「谁负责摘掉」就会在几个文件之间悄悄漂移",
    );
  }
});
