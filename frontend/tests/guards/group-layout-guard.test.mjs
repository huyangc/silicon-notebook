// 群组界面的版式守卫。
//
// 钉的是一条**只在浏览器里才看得出来、组件测试永远发现不了**的退化:群组弹窗与
// 「共享给群组」的每一行,原来都写成块级的 `.checklist-row` 加内联
// `style={{ alignItems: "center", gap: 8 }}` —— 这两个属性在块级盒子上一个字都不
// 生效,于是整行连成一条没有间距的文字(真机形态:「notebook项目1 人组管理员 已展开」)。
// testing-library 只看 DOM 与可访问名字,这种「样式静默无效」在它眼里完全正常。
//
// 覆盖边界(如实说明):本文件只覆盖下面这两个文件里的四条形态判据,不声称覆盖群组
// 特性的全部界面,也不检查具体的间距数值(那属于设计取舍,不是不变量)。
import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { jsxElements, parseModule } from "../../test-support/semantic-source.mjs";

/** 群组界面的两个渲染面:弹窗本体,以及分享弹窗里的「共享给群组」一节。 */
const GROUP_VIEWS = ["groups-panel.tsx", "notebook-group-share.tsx"];

/** 这两个文件里出现过的全部 JSX 标签(新增标签时补进来即可)。 */
const TAGS = ["div", "span", "p", "label", "button", "input", "select", "textarea", "h3", "h4", "section"];

const APP_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../app");
const CSS = await readFile(path.join(APP_DIR, "globals.css"), "utf8");

/** 粗粒度切出顶层规则块(与 effort-picker-style-guard / source-agent-badge-guard 同一
 *  实现,够用——本文件关心的规则都不在 @media 嵌套内)。样式表没有可消费的 AST,
 *  jsdom 也不做级联,文本是唯一诚实的输入;本文件因此按 static-source-policy 的
 *  `DIRECT_READ_ALLOWLIST` 模式登记(对两个 tsx 的断言仍走语义解析)。 */
function cssRules(css) {
  const rules = [];
  const withoutComments = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const pattern = /([^{}]+)\{([^{}]*)\}/g;
  let match;
  while ((match = pattern.exec(withoutComments)) !== null) {
    const selectorList = match[1].trim();
    if (selectorList.startsWith("@")) continue;
    rules.push({
      selectors: selectorList.split(",").map((one) => one.trim()).filter(Boolean),
      body: match[2],
    });
  }
  return rules;
}

const RULES = cssRules(CSS);

/** 某个选择器的声明体(同一选择器出现多次时合并)。 */
function ruleBody(selector) {
  const bodies = RULES
    .filter((rule) => rule.selectors.includes(selector))
    .map((rule) => rule.body);
  assert.notEqual(bodies.length, 0, `globals.css 里找不到 ${selector} 规则`);
  return bodies.join("\n");
}

async function elementsOf(view) {
  const source = await parseModule(view);
  return TAGS.flatMap((tag) =>
    jsxElements(source, tag).map((element) => ({ tag, ...element })));
}

/** className 可能是静态字符串,也可能是模板/三元(那时落在 bindings 上)。 */
const classText = (element) =>
  `${element.attributes?.className ?? ""} ${element.bindings?.className ?? ""}`;

test("行的横向布局由 .group-row 自己给出,不靠调用点的内联样式", () => {
  const body = ruleBody(".group-row");
  assert.match(body, /display:\s*flex/, ".group-row 不再是 flex —— 整行会重新叠成一条文字");
  assert.match(body, /align-items:\s*center/);
  assert.match(body, /flex-wrap:\s*wrap/, "不换行的话窄弹窗里按钮会被挤出可视区");
  // 主区必须能收缩,否则长组名会把右侧按钮顶出去。
  assert.match(ruleBody(".group-row-main"), /min-width:\s*0/);
});

test("群组界面不再用块级的 .checklist-row 当行,也不用内联样式排版", async () => {
  for (const view of GROUP_VIEWS) {
    for (const element of await elementsOf(view)) {
      assert.ok(
        !classText(element).includes("checklist-row"),
        `${view}: 又用 .checklist-row 当行了 —— 它是块级盒子,行内的对齐与间距会静默失效`,
      );
      assert.ok(
        !(element.bindings?.style ?? "").includes("alignItems"),
        `${view}: 内联 alignItems 排版 —— 横向布局必须写在 .group-row 一类的类上`,
      );
    }
  }
});

test("只读标签用 .group-chip,不用主按钮的 .new-pill 冒充", async () => {
  for (const view of GROUP_VIEWS) {
    for (const element of await elementsOf(view)) {
      if (!classText(element).includes("new-pill")) continue;
      assert.equal(
        element.tag,
        "button",
        `${view}: 非按钮元素挂了 .new-pill —— 那是 42px 高的实心黑主按钮,`
        + "一个点不动的标签会比旁边真正能点的按钮还重",
      );
    }
  }
});

test("详情标题是标题元素,不是会把组名大写的 .section-title", async () => {
  const panel = await parseModule("groups-panel.tsx");
  // .section-title 带 text-transform:uppercase,而这里的标题紧挨着用户内容(组名),
  // 同一条规则会把「notebook」渲染成「NOTEBOOK」。
  assert.match(ruleBody(".section-title"), /text-transform:\s*uppercase/);
  for (const element of TAGS.flatMap((tag) => jsxElements(panel, tag))) {
    assert.ok(
      !classText(element).includes("section-title"),
      "groups-panel.tsx 又用回了 .section-title —— 组名会被整块大写",
    );
  }
  assert.equal(jsxElements(panel, "h3").length, 2, "小节标题与详情标题各一处 h3");
  ruleBody(".group-detail-title h3");
});
