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
//   ② `.workspace-extension-entry` 前缀的规则里没有颜色字面量,颜色只能取
//      `var(--…)` / `inherit` / `transparent` / `currentColor` / `none`;
//   ③ page.tsx 里 slot 为 `workspace.side_panel` 的 outlet 位于 `.sources-panel`
//      的 JSX 子树内(它现在是来源栏里的一行入口,不是工作区网格的直接子级)。
//      「固定区、不在滚动的 .source-list 里」那一半由 extension-ui-boundary.test.mjs
//      的直接父级断言(`div.workspace-panel-body.sources-body`)承重,本条只钉子树与
//      「不是网格直接子级」两个方向;
//   ④ 插件侧的**全部**模块(内建 `features/*/workspace-plugin.ts`、仓库外插件包
//      `features/ext-*/` 的每个 .ts/.tsx、以及插件唯一能 import 的共享 UI
//      `features/extension-sdk/ui.tsx`)不带内联 `style`、字符串里没有颜色字面量——
//      否则 ② 可以被绕开(把颜色从 CSS 搬进 createElement 的 props)。扫描面必须覆盖
//      仓库外插件包:那正是「不许写 CSS」这条约束的目标读者,只扫内建插件等于只检查
//      唯一一个本来就守规矩的模块;
//   ⑤ `ui.tsx` 只用系统弹窗骨架类,并且给存储键加上 `extension.` 前缀——插件不得
//      经共享 UI 拿到一个自定义类名的口子,弹窗位置记忆也不许与核心弹窗撞键。
//
// 覆盖边界:①②③ 都是形态判据,不证明像素结果——真正的排版由浏览器决定,jsdom 量
// 不到。运行时可见性(能力/权限/切库失效)由 extension-ui-host.component.test.tsx
// 覆盖,outlet 的 context/权限投影由 extension-ui-boundary.test.mjs 覆盖。
import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import postcss from "postcss";
import ts from "typescript";

import { appSourceModules, parseModule, parseText } from "../../test-support/semantic-source.mjs";

const css = await readFile(new URL("../../app/globals.css", import.meta.url), "utf8");
const stylesheet = postcss.parse(css);

const PLUGIN_PREFIX = ".workspace-extension-entry";
const OUTLET_TOKEN = "workspace-extension-outlet";
/** 插件唯一能 import 的共享 UI；它替插件写 JSX，所以它自己也在「不许内联颜色」之内。 */
const SHARED_PLUGIN_UI = "features/extension-sdk/ui.tsx";
/**
 * `ui.tsx` 允许出现的 className 字面量：系统弹窗骨架类 + SDK 内容组件层的
 * `.extension-*` 基座类。前者承载共享 modal shell，后者承载经评审进入 SDK 的内容
 * 结构；除此之外不再给它第三类 className 逃逸口。
 */
const SHARED_UI_CLASSES = new Set([
  "utility-modal",
  "utility-modal-card",
  "source-modal-header",
  "source-detail-body",
  "icon-button",
  "extension-form-row",
  "extension-input",
  "extension-result-list",
  "extension-result-item",
  "extension-result-item-row",
  "extension-result-item-checkbox",
  "extension-result-item-copy",
  "extension-result-item-title",
  "extension-result-item-meta",
  "extension-result-item-summary",
  "extension-actions",
  "extension-alert",
  "extension-alert--error",
  "extension-alert--warning",
  "extension-alert--status",
  "extension-empty",
]);

/**
 * `ui.tsx` 唯一放行的内联 `style` 形态：**只含 `zIndex` 一个键**。
 *
 * 它是承重的而不是方便：插件弹窗现在与核心弹窗同处一个 primary 冲突组，层级由
 * `use-root-modal-coordinator` 逐次算出来（同层多开时还要按 order 加 rank），落不到
 * DOM 上就只能吃样式表里那个静态的 `.utility-modal { z-index: 60 }`，排序当场失效。
 * 每个核心弹窗曲面写的都是同一个形态（`style={{ zIndex: … }}`）。
 *
 * 判据钉死"恰好一个键、且叫 zIndex"：放宽成"含 zIndex 即可"就等于把整个内联样式的
 * 口子开回来——一条 `{ zIndex, background: "#fff" }` 会照样通过。
 */
function zIndexOnlyStyle(node, module) {
  if (!ts.isJsxAttribute(node)) return false;
  const initializer = node.initializer;
  if (!initializer || !ts.isJsxExpression(initializer) || !initializer.expression) return false;
  const object = initializer.expression;
  if (!ts.isObjectLiteralExpression(object) || object.properties.length !== 1) return false;
  const [only] = object.properties;
  return ts.isPropertyAssignment(only) && only.name.getText(module) === "zIndex";
}

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
      // 先剥掉 token 引用再判:`var(--blue)` / `var(--red)` 是 :root 合法 token,
      // 具名色那一支不能把它们当成字面量误报(否则守卫会逼人放宽正则)。
      if (COLOR_LITERAL.test(node.value.replace(/var\(--[^)]*\)/g, ""))) {
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
    if (ts.isJsxElement(cursor)) ancestorClasses.push(classNameOf(cursor));
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


test("插件侧模块不带内联 style，字符串里没有颜色字面量", async () => {
  const modules = await appSourceModules();
  // 三类扫描面:内建插件入口、仓库外插件包的**每个**模块、以及插件唯一能 import 的
  // 共享 UI。第二类是这条守卫真正的目标读者——它们由同步脚本从内网复制进来,谁也
  // 没有在 review 里看过;只扫内建插件等于只检查唯一一个本来就守规矩的模块。
  //
  // ⚠ 分工登记:`features/ext-*/` 是生成物、不入库——**G1 恒为空**(下面的空转保护
  // 只对内建插件与共享 UI 两档起作用,它们非空是硬性的);**G2 的
  // `scripts/check_sample_plugin.sh` 会给它非空样本**(X9 PR-B T3):该脚本用
  // `SILICON_NOTEBOOK_UI_PLUGINS` 指向 `examples/extensions/arxiv-search/ui/arxiv-search`
  // 真跑一遍 `sync-ui-plugins.mjs` 再执行 `node --test tests/guards/extension-*.test.mjs`,
  // 本条守卫因此在 G2 里第一次真正检查这一档。仓库外私有插件树同样在
  // `npm run sync:ui-plugins` 之后跑同一条命令验证。
  const plugins = modules.filter((row) => (
    /^features\/[^/]+\/workspace-plugin\.tsx?$/.test(row.path)
    || /^features\/ext-[a-z0-9-]+\/.+\.tsx?$/.test(row.path)
    || row.path === SHARED_PLUGIN_UI
  ));
  // 空转保护:registry 至少登记了一个真实内建插件,共享 UI 也必须在扫描面里——
  // 改名/挪走任何一个都要响亮失败,而不是静默把它排除出去。
  assert.ok(
    plugins.some((row) => /^features\/[^/]+\/workspace-plugin\.tsx?$/.test(row.path)),
    "没找到 features/*/workspace-plugin.ts(路径约定变了？守卫失效)",
  );
  assert.ok(
    plugins.some((row) => row.path === SHARED_PLUGIN_UI),
    `没找到 ${SHARED_PLUGIN_UI}(改名或删除？守卫失效)`,
  );
  // ⚠ 判据只认**具名** `style` 属性/键。`ui.tsx` 展开的 `{...floating.dragHandleProps}`
  // 带一个 `style`(`touchAction`/`cursor`),那是全仓浮窗的共享拖动契约、且不含颜色,
  // 刻意放行:它是 JsxSpreadAttribute,结构上就不在下面这个判据里。
  //
  // 第二条例外是**只给 `ui.tsx` 的**、且只放行 `style={{ zIndex }}` 这**一个**形态
  // (见 `zIndexOnlyStyle` 的注释:层级归 root-dialog 协调器算,不落 DOM 就排不了序)。
  // 插件侧的模块一个 `style` 都不许有,这条例外不外借。
  const offenders = [];
  for (const { path, module } of plugins) {
    function visit(node) {
      // createElement(tag, { style: … }) / JSX style={…}:颜色从 CSS 搬进 props 同样越线。
      if (
        (ts.isPropertyAssignment(node) || ts.isShorthandPropertyAssignment(node) || ts.isJsxAttribute(node))
        && node.name.getText(module) === "style"
        && !(path === SHARED_PLUGIN_UI && zIndexOnlyStyle(node, module))
      ) offenders.push(`${path}: inline style`);
      if (ts.isStringLiteralLike(node) && COLOR_LITERAL.test(node.text)) {
        offenders.push(`${path}: ${JSON.stringify(node.text)}`);
      }
      ts.forEachChild(node, visit);
    }
    visit(module);
  }
  assert.deepEqual(offenders, [], "插件组件的视觉只能来自系统类与 :root token,不得内联颜色");
});


test("ui.tsx 的内联 style 例外只放行 zIndex 这一个键", () => {
  // 例外本身是被 root-dialog 裁决逼出来的（层级要算、要落 DOM），但它极易被放宽成
  // 「含 zIndex 即可」——那等于把整个内联样式的口子重新开开。这条按真解析出的 JSX
  // 属性逐条判，正反两个方向都钉。
  const parse = (attribute) => {
    const module = parseText(`export const X = () => <section ${attribute} />;\n`, SHARED_PLUGIN_UI);
    let found;
    function visit(node) {
      if (ts.isJsxAttribute(node) && node.name.getText(module) === "style") found = node;
      ts.forEachChild(node, visit);
    }
    visit(module);
    assert.ok(found, `夹具里没解析出 style 属性：${attribute}`);
    return zIndexOnlyStyle(found, module);
  };
  assert.equal(parse("style={{ zIndex: dialog.zIndex }}"), true);
  assert.equal(parse("style={{ zIndex: 60 }}"), true);
  for (const attribute of [
    'style={{ zIndex: dialog.zIndex, background: "#fff" }}',
    'style={{ background: "#fff" }}',
    "style={{ ...base, zIndex: dialog.zIndex }}",
    "style={styles}",
    'style="z-index: 60"',
  ]) assert.equal(parse(attribute), false, attribute);
});


test("扩展 SDK 只用系统弹窗骨架类与已登记的 extension-ui-kit 基座类", async () => {
  const modules = await appSourceModules();
  const shared = modules.find((row) => row.path === SHARED_PLUGIN_UI);
  assert.ok(shared, `没找到 ${SHARED_PLUGIN_UI}(改名或删除？守卫失效)`);

  const classNames = [];
  let storageKeyExpression;
  function visit(node) {
    if (ts.isJsxAttribute(node) && node.name.getText(shared.module) === "className") {
      const initializer = node.initializer;
      // 只接受字符串字面量:模板串/表达式意味着类名是算出来的,那正是「插件带自己的
      // 类」会长的样子,判否比放行安全。
      assert.ok(
        initializer && ts.isStringLiteral(initializer),
        `${SHARED_PLUGIN_UI} 的 className 必须是字符串字面量,不得由表达式拼出`,
      );
      classNames.push(...initializer.text.split(/\s+/).filter(Boolean));
    }
    if (ts.isJsxAttribute(node) && node.name.getText(shared.module) === "storageKey") {
      storageKeyExpression = node.initializer?.getText(shared.module) ?? "";
    }
    ts.forEachChild(node, visit);
  }
  visit(shared.module);

  // 非空:扫不出任何 className 说明组件被改写成别的形态,守卫会静默变成空断言。
  assert.ok(classNames.length > 0, `${SHARED_PLUGIN_UI} 里一个 className 都没扫到(守卫失效)`);
  assert.deepEqual(
    [...new Set(classNames)].filter((name) => !SHARED_UI_CLASSES.has(name)).sort(),
    [],
    "扩展 SDK 只能用系统弹窗骨架类与已登记的 extension-ui-kit 基座类;需要新类就等于要给插件开一条写样式的口子",
  );
  // 存储键的两段都由 SDK 拼、插件改不了。`extension.` 前缀隔离的是「插件 vs 核心
  // 弹窗」;插件**之间**靠 `pluginId` 那一段隔离——少了它,两个插件各写一个
  // `storageKey="search"` 就共用同一格 sessionStorage、互相顶掉窗口位置,而这种失败
  // 事后完全看不出来(它只表现为位置偶尔"自己变了")。
  assert.ok(storageKeyExpression, `${SHARED_PLUGIN_UI} 必须给 FloatingModalCard 传 storageKey`);
  assert.ok(
    storageKeyExpression.includes("extension."),
    `扩展弹窗的 storageKey 必须带 extension. 前缀,实际是 ${storageKeyExpression}`,
  );
  assert.ok(
    storageKeyExpression.includes("pluginId"),
    `扩展弹窗的 storageKey 必须带 pluginId 段,否则两个插件会共用一格位置记忆,实际是 ${storageKeyExpression}`,
  );
});
