// 独立群组页的样式守卫。
//
// 钉的是一类**只在浏览器里才看得出来、组件测试永远发现不了**的退化,与
// group-layout-guard 同源但对象不同(那个盯 groups-panel/notebook-group-share 的
// 行布局,这个盯独立页 groups-page.tsx)。
//
// 第一条来自真实缺陷:页面上 7 处 `<span className="eyebrow">GROUP WORKSPACE</span>`
// 这类装饰性小标题,而 globals.css 里**从来没有** `.eyebrow` 规则 —— 于是它们以
// 继承来的 16px 正文字号裸奔在标题上方,看起来像忘了删的调试文本。testing-library
// 只看 DOM 与可访问名字,`getByText("GROUP WORKSPACE")` 一路通过;`tsc` 不检查
// className 字符串;没有任何既有门禁会红。判据因此是构造性的:**页面里出现的每一个
// class 名都必须在 globals.css 里真的作为类选择器出现过**。
//
// 第二、三条是这次改版踩到的两个布局陷阱,同样是「不报错、只是长错了」:
//   - **1200px 那条测量线必须量在不带内边距的块上**。`.collection-title` /
//     `.notebook-grid` 是 `.page` 的子块、自己没有内边距,所以 1200 就是 1200;而
//     `.group-page` 继承着 `.page` 的 `padding: 44px 24px`,全局
//     `* { box-sizing: border-box }` 会把那 48px 吃进 max-width 里,内容只剩 1152 ——
//     ≥1248px 的视口上比兄弟页每边窄 24px,看起来不像对齐、像缩了一档
//     (codex #589 R1 P2)。因此 cap 挂在 `.group-page > *` 上,不在 `.group-page` 上。
//     子项还必须显式 `width: 100%`:auto 外边距会关掉网格子项的 stretch,只给
//     max-width 会让它退回 fit-content,宽屏上缩成一条窄柱。
//   - `main.group-page` 会被 `.app` 的 1fr 行拉满视口高度;网格 auto 行默认 stretch,
//     短内容的页签(成员/设置)页头会被凭空撑开几十像素。
//
// 第四条盯的是**同一个动作长成两种样子**:「返回」在笔记本工作区是黑色药丸
// (`.back-home-button`),在群组页曾是一条灰色文字链(`.group-page-back`),而两处
// 点下去调的都是 `showCollection()`、都回到笔记本列表。样式因此只有一份声明和一个
// 名字,两个调用点共用;任一处改回自己的类名,或 CSS 里重新长出一份并列的返回样式,
// 都在这里报红。
//
// 第五条盯标题层级:页面标题「群组」已移进顶栏的 `.brand-title`(SN 徽标旁边),所以
// 群组页自己不再有 `<h1>`。这不是可选的整洁,而是「顶栏那一格现在是页面标题」这条
// 设计的另一半 —— 两处都写就是同一句话说两遍,两处都不写则整页没有标题层级。
//
// 覆盖边界(如实说明):本文件只覆盖 groups-page.tsx 的 class 名存在性与上面这几条
// 版式/结构声明,不检查具体间距/配色数值(那属于设计取舍,不是不变量),也不声称覆盖
// 群组特性的其它渲染面。
import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import ts from "typescript";

import { parseText } from "../../test-support/semantic-source.mjs";

const APP_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../app");
const VIEW = "groups-page.tsx";
const SHELL = "page.tsx";

const view = parseText(await readFile(path.join(APP_DIR, VIEW), "utf8"), VIEW);
// 返回控件的另一个调用点在工作区顶栏里,共用同一份样式,所以这条守卫必须同时看两个文件。
const shell = parseText(await readFile(path.join(APP_DIR, SHELL), "utf8"), SHELL);
// 样式表没有可消费的 AST,jsdom 也不做级联,文本是唯一诚实的输入(与
// group-layout-guard 同一条登记)。注释先剥掉,免得注释里写过的类名冒充规则。
const CSS = (await readFile(path.join(APP_DIR, "globals.css"), "utf8"))
  .replace(/\/\*[\s\S]*?\*\//g, "");

/** 收集 className 上出现的每一个静态 class token。
 *
 *  刻意不按标签名枚举(group-layout-guard 那份 TAGS 清单会让新标签静默逃逸):这里
 *  走整棵 AST 找 `className` 属性,模板串取它的固定片段、三元取两个分支的字面量,
 *  动态拼出来的部分本来就无从检查、直接跳过。 */
function classTokens(sourceFile) {
  const tokens = new Set();
  // ⚠ 形参不叫 text/source/content:static-source-policy 把「在这类名字上调
  // split/slice/indexOf」判成对生产源码做位置查询。这里切的是 className 的值,不是源码。
  const add = (classList) => {
    for (const token of classList.split(/\s+/)) {
      if (token) tokens.add(token);
    }
  };

  function collectFromExpression(node) {
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
      add(node.text);
      return;
    }
    if (ts.isTemplateExpression(node)) {
      add(node.head.text);
      for (const span of node.templateSpans) {
        collectFromExpression(span.expression);
        add(span.literal.text);
      }
      return;
    }
    if (ts.isConditionalExpression(node)) {
      collectFromExpression(node.whenTrue);
      collectFromExpression(node.whenFalse);
      return;
    }
    if (ts.isParenthesizedExpression(node)) {
      collectFromExpression(node.expression);
    }
  }

  function visit(node) {
    if (ts.isJsxAttribute(node) && node.name.getText(sourceFile) === "className" && node.initializer) {
      if (ts.isStringLiteral(node.initializer)) add(node.initializer.text);
      else if (ts.isJsxExpression(node.initializer) && node.initializer.expression) {
        collectFromExpression(node.initializer.expression);
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);

  return [...tokens];
}

/** globals.css 里是否存在以该 token 命名的类选择器。
 *
 *  `active` / `manage` / `compact` 这类修饰符只以复合选择器出现(`.group-access-chip.manage`),
 *  所以判据是「`.token` 后面不再接标识符字符」,不要求它独占一段选择器。 */
function hasClassRule(token) {
  return new RegExp(`\\.${token.replace(/[-]/g, "\\-")}(?![\\w-])`).test(CSS);
}

test("groups-page.tsx 用到的每个 class 都在 globals.css 里真的有规则", () => {
  const orphans = classTokens(view).filter((token) => !hasClassRule(token));
  assert.deepEqual(
    orphans,
    [],
    `groups-page.tsx 挂了 globals.css 里不存在的 class:${orphans.join("、")}`
    + " —— 这类元素会以继承来的正文样式裸奔,组件测试与 tsc 都看不见",
  );
});

/** 取某个选择器的声明体(同一选择器出现多次时合并)。 */
function ruleBody(selector) {
  const bodies = [];
  const pattern = /([^{}]+)\{([^{}]*)\}/g;
  let match;
  while ((match = pattern.exec(CSS)) !== null) {
    const selectors = match[1].trim();
    if (selectors.startsWith("@")) continue;
    if (selectors.split(",").map((one) => one.trim()).includes(selector)) bodies.push(match[2]);
  }
  assert.notEqual(bodies.length, 0, `globals.css 里找不到 ${selector} 规则`);
  return bodies.join("\n");
}

test("1200px 测量线量在不带内边距的子项上,不量在继承了 .page 留白的 main 上", () => {
  const child = ruleBody(".group-page > *");
  assert.match(child, /max-width:\s*1200px/, "与「笔记本列表」同一条测量线");
  assert.match(
    child,
    /width:\s*100%/,
    "只给 max-width + auto 外边距会关掉网格子项的 stretch —— 在宽屏上缩成一条窄柱",
  );
  assert.match(child, /margin-inline:\s*auto/, "没有它子项会靠左而不是居中");
  // 把 cap 挪回 .group-page 自己身上就会重新把 .page 的 48px 留白吃进 1200 里
  // (全局 box-sizing: border-box),内容只剩 1152。
  assert.doesNotMatch(
    ruleBody(".group-page"),
    /max-width/,
    ".group-page 自己带着 .page 的 24px 左右留白,在它身上限宽会让内容比兄弟页每边窄 24px",
  );
});

test(".group-page 的行不跟着视口拉伸", () => {
  assert.match(
    ruleBody(".group-page"),
    /align-content:\s*start/,
    "main 会被 .app 的 1fr 行拉满视口高度,auto 行默认 stretch 会把短页签的页头凭空撑开",
  );
});


/** 找出所有 <button>(或任意标签)上写死的 className 字面量。 */
function buttonClassNames(sourceFile) {
  const found = [];
  function visit(node) {
    if (ts.isJsxAttribute(node)
      && node.name.getText(sourceFile) === "className"
      && node.initializer
      && ts.isStringLiteral(node.initializer)) {
      found.push(node.initializer.text);
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return found;
}

test("「返回主页」在两个页面上是同一个控件,不是两份长得不一样的样式", () => {
  // 两处点下去都是 showCollection() → 笔记本列表。同一个动作只该有一份样式。
  for (const [label, sourceFile] of [["群组页", view], ["工作区顶栏", shell]]) {
    assert.ok(
      buttonClassNames(sourceFile).includes("back-home-button"),
      `${label}没有使用共享的 .back-home-button —— 同一个返回动作又长出了第二种样子`,
    );
  }
  assert.match(ruleBody(".back-home-button"), /border-radius:\s*999px/, "共享控件仍是那枚药丸");
  // 旧的灰色文字链必须真的消失,而不是留在样式表里等人再挂回去。
  assert.equal(
    /\.group-page-back(?![\w-])/.test(CSS),
    false,
    "globals.css 里仍有 .group-page-back —— 并列的第二份返回样式就是这次要消除的东西",
  );
});

test("页面标题只出现一次:在顶栏,不在页面里", () => {
  assert.equal(
    view.text.includes("<h1"),
    false,
    "groups-page.tsx 里又出现了 <h1> —— 标题已移进顶栏的 .brand-title,两处都写就是说两遍",
  );
  // 反过来:顶栏那一格必须真的是 h1,否则整页没有标题层级(只是一个看起来像标题的 div)。
  assert.match(
    shell.text,
    /<h1 className="brand-title">/,
    "顶栏的群组标题不是 <h1> —— 页面里已经没有别的标题了,标题层级会整个消失",
  );
});
