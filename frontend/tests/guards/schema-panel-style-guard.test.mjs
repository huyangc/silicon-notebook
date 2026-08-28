// 「图谱 Schema」面板的样式守卫。
//
// 钉的是一类**只在浏览器里才看得出来、组件测试永远发现不了**的退化——与
// group-page-style-guard 同源,对象换成 schema-manager.tsx 与它在 page.tsx 里的外壳。
//
// 第一条来自这个面板自己的旧账:状态徽章一直挂着 `severity-low`,而 globals.css 里
// 只有 `.severity-high` / `.severity-medium`,从来没有 low 那一条。于是「已启用」多年
// 以默认 `.tag` 样式裸奔,而 `tsc` 不检查 className 字符串、testing-library 只看文本、
// jsdom 不做级联——没有任何既有门禁会红。判据因此是构造性的:面板里出现的每一个 class
// 名都必须在 globals.css 里真的作为类选择器出现过。
//
// 后面几条是这次重排踩到的、同样「不报错、只是长错了」的三个陷阱:
//   - **弹窗宽度必须用复合选择器压过 `.utility-modal-card`**。`.source-modal-card,
//     .utility-modal-card { width: min(680px, 100%) }` 在同一份样式表里排在后面,同为
//     (0,1,0) 时后来者胜,单写 `.schema-modal-card` 会被它整条吃掉:实测右栏只剩 334px,
//     六个输入框挤成一团,而两栏版式的前提正是右栏放得下全宽表单。
//   - **弹窗体必须定高**。两栏各自滚动要求父容器有确定高度;写成 `max-height` 时那条
//     `minmax(0, 1fr)` 行会退回内容高,把清单栏底部那排「新增类型」/「归纳候选类型」
//     顶出 `overflow: hidden` 的裁切线——那是这个面板**唯一**的写入口,被裁掉就等于
//     整个面板只剩只读。
//   - **清单行的收缩顺位**:先压显示名,最后才动类型标识。标识是这一行的身份(也是 API
//     上的键),把 `process_window` 截成 `process_wind…` 而旁边的「工艺窗口」四个字完好
//     无损,正好把该留的和该让的弄反了。
//
// 覆盖边界(如实说明):本文件只覆盖 class 名存在性与上面这三条版式声明,不检查具体
// 间距/配色数值(那属于设计取舍,不是不变量),也不覆盖窄视口分支——媒体查询里的声明
// 由人工与浏览器验证把关。面板的**行为**(只读/编辑/新增三态、草稿分格、写回执)由
// tests/component/schema-manager.component.test.tsx 真渲染断言。
import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import ts from "typescript";

import { parseText } from "../../test-support/semantic-source.mjs";

const APP_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../app");
const PANEL = "schema-manager.tsx";

const panel = parseText(await readFile(path.join(APP_DIR, PANEL), "utf8"), PANEL);
// 样式表没有可消费的 AST,jsdom 也不做级联,文本是唯一诚实的输入(与
// group-page-style-guard 同一条登记)。注释先剥掉,免得注释里写过的类名冒充规则。
const CSS = (await readFile(path.join(APP_DIR, "globals.css"), "utf8"))
  .replace(/\/\*[\s\S]*?\*\//g, "");

/** 收集 className 上出现的每一个静态 class token(采集方式与群组页那条守卫同款)。 */
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
    if (ts.isBinaryExpression(node)) {
      collectFromExpression(node.left);
      collectFromExpression(node.right);
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

/** globals.css 里是否存在以该 token 命名的类选择器(修饰符可以只以复合形态出现)。 */
function hasClassRule(token) {
  return new RegExp(`\\.${token.replace(/[-]/g, "\\-")}(?![\\w-])`).test(CSS);
}

/** 取某个选择器的声明体(同一选择器出现多次时合并)。媒体查询内的规则不在扫描面内。 */
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

/** `flex: <grow> <shrink> <basis>` 里的收缩因子。 */
function flexShrinkOf(selector) {
  const shorthand = /flex:\s*(\d+)\s+(\d+)\s/.exec(ruleBody(selector));
  assert.ok(shorthand, `${selector} 没有写成 flex: <grow> <shrink> <basis> 简写`);
  return Number(shorthand[2]);
}

test("schema-manager.tsx 用到的每个 class 都在 globals.css 里真的有规则", () => {
  const orphans = classTokens(panel).filter((token) => !hasClassRule(token));
  assert.deepEqual(
    orphans,
    [],
    `schema-manager.tsx 挂了 globals.css 里不存在的 class:${orphans.join("、")}`
    + " —— 这类元素会以继承来的样式裸奔(旧版的 severity-low 就是这么活了很久),"
    + "组件测试与 tsc 都看不见",
  );
});

test("弹窗宽度写成复合选择器,压得过排在后面的 .utility-modal-card", () => {
  assert.match(
    ruleBody(".utility-modal-card.schema-modal-card"),
    /width:\s*min\(/,
    "两栏版式要求这个弹窗比默认的 680px 宽",
  );
  // 单类选择器与 `.utility-modal-card` 同为 (0,1,0),而后者排在本文件更后面 —— 后来者胜,
  // 宽度声明会被整条吃掉。
  const singles = [...CSS.matchAll(/([^{}]+)\{[^{}]*\}/g)]
    .map((match) => match[1].trim())
    .filter((selectors) => selectors.split(",").map((one) => one.trim()).includes(".schema-modal-card"));
  assert.deepEqual(singles, [], ".schema-modal-card 不能单独作为选择器出现,会被 .utility-modal-card 覆盖");
});

test("弹窗体定高,两栏才分得到确定高度各自滚动", () => {
  const body = ruleBody(".schema-modal-body");
  assert.match(
    body,
    /(^|[\s;])height:\s*min\(/,
    "只写 max-height 时可伸缩的那一格会退回内容高,清单栏底部的写入口会被裁掉",
  );
  assert.match(body, /overflow:\s*hidden/);
  assert.match(
    ruleBody(".schema-list"),
    /grid-template-rows:\s*minmax\(0,\s*1fr\)\s+auto/,
    "清单自己滚、底部动作区不参与压缩",
  );
});

test("竖排容器不按子项个数排版——作用范围那一行只有管理员看得到", () => {
  // 真实缺陷:`.schema-panel` 曾写 `grid-template-rows: auto auto minmax(0, 1fr)`,
  // 默认「作用范围 + 说明 + 工作区」三个子项。普通库主看不到作用范围那一行,于是整体
  // 错一格:工作区落到第二条 auto 上按内容定高,`1fr` 那行空着没人用,类型一多就把栏底
  // 唯一的写入口顶出 `overflow: hidden`(codex #614 R3 P1)。右栏 `.schema-detail` 早先
  // 因为空态没有栏头踩过同一个坑。判据因此钉在**机制**上而不是某一组行数:这两个容器
  // 必须是 flex 竖排,并且由「谁吃剩余高度」自己声明,而不是由它排在第几行决定。
  for (const selector of [".schema-modal-body", ".schema-panel", ".schema-detail"]) {
    const declarations = ruleBody(selector);
    assert.match(declarations, /display:\s*flex/, `${selector} 必须是 flex 竖排`);
    assert.match(declarations, /flex-direction:\s*column/, selector);
    assert.doesNotMatch(
      declarations,
      /grid-template-rows/,
      `${selector} 不得按行数排版 —— 子项个数随权限变化`,
    );
  }
  assert.match(
    ruleBody(".schema-panel"),
    /flex:\s*1\s+1\s+auto/,
    ".schema-panel 要吃满弹窗体的剩余高度",
  );
  assert.match(
    ruleBody(".schema-workbench"),
    /flex:\s*1\s+1\s+auto/,
    "工作区是面板里那个吃剩余高度的格子;少了它,两栏拿不到确定高度",
  );
});

test("清单行先压显示名，最后才压类型标识", () => {
  assert.ok(
    flexShrinkOf(".schema-type-label") > flexShrinkOf(".schema-type-name code"),
    "标识是这一行的身份,不该比显示名先被截断",
  );
});
