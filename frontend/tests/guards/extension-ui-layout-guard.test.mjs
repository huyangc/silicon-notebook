// workspace.side_panel 落点与视觉的布局守卫。
//
// 起因(PR #563 的回归):那版给 `.workspace-grid` 加了三条
// `:has(> .workspace-extension-outlet-workspace-side_panel)` 规则,工作区因此多出
// 一整列 `minmax(190px, 18%)`——列里只有一个入口,其余整列空白,问答面板被挤窄。
// 同一版的卡片样式还写死了 `#fff` / `#667085` / `#0f6d7a` / `rgba(34,48,68,.12)`,
// 违反「视觉必须复用系统现有色板、边框、圆角、排版」。
//
// 因此本守卫钉三条不变量,判据都是**语义**(PostCSS 选择器/声明、TypeScript AST),
// 不含行号——规则上下挪动不该报红,把列加回来、把颜色写死、把入口挪回工作区网格
// 才该报红:
//
//   ① globals.css 里(含 @media 内)不存在任何 selector 同时含 `:has(` 与
//      `workspace-extension-outlet` 的规则——即扩展点不得再撑出自己的一列;
//   ② `.agent-profile-workspace-plugin` 前缀的规则里没有颜色字面量,颜色只能取
//      `var(--…)` / `inherit` / `transparent` / `currentColor` / `none`;
//   ③ page.tsx 里 slot 为 `workspace.side_panel` 的 outlet 位于 `.sources-panel`
//      的 JSX 子树内(它现在是来源栏里的一行入口,不是工作区网格的直接子级)。
//
// 覆盖边界:①②③ 都是形态判据,不证明像素结果——真正的排版由浏览器决定,jsdom 量
// 不到。运行时可见性(能力/权限/切库失效)由 extension-ui-host.component.test.tsx
// 覆盖,outlet 的 context/权限投影由 extension-ui-boundary.test.mjs 覆盖。
import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import postcss from "postcss";
import ts from "typescript";

import { parseModule } from "../../test-support/semantic-source.mjs";

const css = await readFile(new URL("../../app/globals.css", import.meta.url), "utf8");
const stylesheet = postcss.parse(css);

const PLUGIN_PREFIX = ".agent-profile-workspace-plugin";
const OUTLET_TOKEN = "workspace-extension-outlet";

// 颜色字面量:十六进制、rgb()/rgba()、hsl()/hsla(),以及最常见的一批具名色。
// 具名色不穷举——穷举 CSS 那 148 个名字只会给人一种「已经查全了」的错觉,而真正
// 会被写进来的是 #fff / rgba(…) 这两种(PR #563 用的就是它们)。
const COLOR_LITERAL = /#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\s*\(|\b(?:white|black|red|blue|green|gray|grey|silver|orange|purple|yellow)\b/;

function ruleSelectors() {
  const rules = [];
  stylesheet.walkRules((rule) => rules.push(rule));
  return rules;
}


test("扩展点不再撑出自己的工作区列（任何位置、含 @media 内）", () => {
  const offenders = ruleSelectors()
    .filter((rule) => rule.selector.includes(":has(") && rule.selector.includes(OUTLET_TOKEN))
    .map((rule) => {
      const scope = rule.parent?.type === "atrule"
        ? `@${rule.parent.name} ${rule.parent.params}`
        : "<top-level>";
      return `${scope} → ${rule.selector}`;
    });
  assert.deepEqual(
    offenders,
    [],
    "workspace.side_panel 不得再用 :has() 给 .workspace-grid 加列——那一列只放一个入口、其余整列空白",
  );
  // 空转保护:选择器扫描本身必须真的看得见这份样式表。
  assert.ok(ruleSelectors().length > 100, "样式表没解析出规则（守卫失效）");
});


test("理解入口的样式只用系统 token，没有颜色字面量", () => {
  const rules = ruleSelectors().filter((rule) => rule.selector.includes(PLUGIN_PREFIX));
  // 匹配为 0 说明类被改名/删了 —— 守卫必须响亮失败,不能静默变成空断言。
  assert.ok(rules.length > 0, `没找到 ${PLUGIN_PREFIX} 的样式规则（改名或删除？守卫失效）`);

  const offenders = [];
  for (const rule of rules) {
    for (const node of rule.nodes ?? []) {
      if (node.type !== "decl") continue;
      if (COLOR_LITERAL.test(node.value)) {
        offenders.push(`${rule.selector} { ${node.prop}: ${node.value} }`);
      }
    }
  }
  assert.deepEqual(
    offenders,
    [],
    "入口样式必须走 :root 的 token（var(--…)）或既有按钮类，不得写死颜色",
  );
});


test("理解入口挂在来源栏内，不是工作区网格的直接子级", async () => {
  const page = await parseModule("page.tsx");

  let outlet;
  function visit(node) {
    if (
      (ts.isJsxSelfClosingElement(node) || ts.isJsxOpeningElement(node))
      && node.tagName.getText(page) === "WorkspaceExtensionOutlet"
    ) {
      const slot = node.attributes.properties
        .filter(ts.isJsxAttribute)
        .find((attribute) => attribute.name.getText(page) === "slot")
        ?.initializer;
      if (slot && ts.isStringLiteral(slot) && slot.text === "workspace.side_panel") {
        outlet = node;
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(page);
  assert.ok(outlet, "没找到 workspace.side_panel 的 outlet（改名或删除？守卫失效）");

  function classNameOf(element) {
    const opening = ts.isJsxElement(element) ? element.openingElement : element;
    return opening.attributes.properties
      .filter(ts.isJsxAttribute)
      .find((attribute) => attribute.name.getText(page) === "className")
      ?.initializer?.getText(page) ?? "";
  }

  const ancestorClasses = [];
  let cursor = outlet.parent;
  while (cursor) {
    if (ts.isJsxElement(cursor) || ts.isJsxSelfClosingElement(cursor)) {
      ancestorClasses.push(classNameOf(cursor));
    }
    cursor = cursor.parent;
  }

  assert.ok(
    ancestorClasses.some((value) => value.includes("sources-panel")),
    "workspace.side_panel 必须落在来源栏（.sources-panel）的子树内",
  );
  // 反方向:它不能再是工作区网格的**直接**子级——那正是三列布局的接线方式。
  const directParentClass = ancestorClasses[0] ?? "";
  assert.ok(
    !directParentClass.includes("workspace-grid"),
    "workspace.side_panel 不得直接挂在 .workspace-grid 下（那会重新变成独立一列）",
  );
});
