// 回归门:渲染模型产出文本的 Markdown 管线必须显式关掉 remark-gfm 的 `singleTilde`。
//
// 真机踩过(部署环境的 Ask 回答):remark-gfm 4.0.1 的删除线扩展默认
// `singleTilde: true`——**单个** `~` 就是删除线定界符(github.com 网页版行为,GFM 规范
// 本身不允许)。中文技术答案里区间 `7~5nm`/`80~90%`/`2~3 周` 与约等于 `~5W`/`~3GHz`
// 密度极高,同段出现两个词内 `~` 就配成一对,中间整段正文被 `<del>` 划掉。没有任何
// 报错,只是答案里凭空多出一条中划线。
//
// 判据分两条,缺一不可:
//   ① 任何 `<ReactMarkdown>` 的 `remarkPlugins` 都不得出现裸 `remarkGfm`——必须是
//      共享真源 `remarkGfmPlugin`,或就地带 `{ singleTilde: false }` 的元组;
//   ② 除真源模块本身外,生产代码不得直接 import `remark-gfm`——否则下一个渲染面可以
//      不经 JSX(例如自己 `unified().use(remarkGfm)`)绕过第 ① 条。
//
// 豁免只有一个且是刻意的:`knowhow-cell-editor.tsx` 的格子预览与后端
// `backend/app/services/knowhow/md_normalize.py` 是一对孪生实现,两侧的跨行强调配对
// 都按「单 `~` 是删除线」判定「规整会不会拆断渲染」。单方面改渲染器会让那套护栏与
// 实际渲染口径分叉(方向变保守、不损坏数据,但论证失真),要连孪生逻辑一起改,是独立
// 一件事。豁免登记在这里,不是忘了。
import test from "node:test";
import assert from "node:assert/strict";
import ts from "typescript";

import { appSourceModules } from "../../test-support/semantic-source.mjs";

/** GFM 插件的唯一定义点——只有它自己可以 import `remark-gfm`。 */
const PLUGIN_SOURCE = "markdown-gfm.ts";
/** 刻意保留裸 `remarkGfm` 的面(理由见文件头)。 */
const EXEMPT = new Set(["knowhow-cell-editor.tsx"]);
/** 渲染模型产出文本的面——少一个就是覆盖缩水(重命名/搬走时守卫必须响)。 */
const MODEL_TEXT_SURFACES = ["answer-markdown.tsx", "r/[token]/page.tsx", "report-view.tsx"];

function moduleSpecifiers(sourceFile) {
  return sourceFile.statements
    .filter((statement) => (
      ts.isImportDeclaration(statement)
      && ts.isStringLiteral(statement.moduleSpecifier)
    ))
    .map((statement) => statement.moduleSpecifier.text);
}

/** `remarkPlugins={...}` 的表达式节点(每个 `<ReactMarkdown>` 一个)。 */
function remarkPluginsExpressions(sourceFile) {
  const found = [];
  function visit(node) {
    if (
      (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node))
      && node.tagName.getText(sourceFile) === "ReactMarkdown"
    ) {
      for (const attribute of node.attributes.properties) {
        if (
          ts.isJsxAttribute(attribute)
          && attribute.name.getText(sourceFile) === "remarkPlugins"
          && attribute.initializer
          && ts.isJsxExpression(attribute.initializer)
          && attribute.initializer.expression
        ) {
          found.push(attribute.initializer.expression);
        }
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return found;
}

/** `{ singleTilde: false }` 字面量?(只认写死的 false——变量/展开一律不算证明) */
function disablesSingleTilde(node) {
  if (!node || !ts.isObjectLiteralExpression(node)) return false;
  return node.properties.some((property) => (
    ts.isPropertyAssignment(property)
    && property.name.getText(property.getSourceFile()) === "singleTilde"
    && property.initializer.kind === ts.SyntaxKind.FalseKeyword
  ));
}

/** 单个 `remarkPlugins` 数组项:裸 `remarkGfm` 或未关 singleTilde 的元组 -> 违规。 */
function violatingPluginEntry(entry) {
  // `[remarkGfm, opts] as [...]` —— 断言层不改语义,剥掉再看。
  const bare = ts.isAsExpression(entry) || ts.isParenthesizedExpression(entry)
    ? entry.expression
    : entry;
  if (ts.isIdentifier(bare) && bare.text === "remarkGfm") return "裸 remarkGfm";
  if (ts.isArrayLiteralExpression(bare)) {
    const [plugin, options] = bare.elements;
    if (plugin && ts.isIdentifier(plugin) && plugin.text === "remarkGfm" && !disablesSingleTilde(options)) {
      return "remarkGfm 元组未带 { singleTilde: false }";
    }
  }
  return undefined;
}

test("渲染模型文本的 ReactMarkdown 都关掉了 remark-gfm 的单波浪线删除线", async () => {
  const surfaces = [];
  const violations = [];
  for (const { path, module } of await appSourceModules()) {
    for (const expression of remarkPluginsExpressions(module)) {
      surfaces.push(path);
      if (EXEMPT.has(path)) continue;
      if (!ts.isArrayLiteralExpression(expression)) {
        violations.push(`${path}: remarkPlugins 不是数组字面量,守卫读不出插件配置`);
        continue;
      }
      for (const entry of expression.elements) {
        const reason = violatingPluginEntry(entry);
        if (reason) violations.push(`${path}: ${reason}`);
      }
    }
  }

  // 空转保护:三个模型文本渲染面必须都被扫到,重命名/搬走时守卫要响而不是静默缩水。
  for (const surface of MODEL_TEXT_SURFACES) {
    assert.ok(surfaces.includes(surface), `没扫到渲染面 ${surface}: ${surfaces.join(", ")}`);
  }
  assert.deepEqual(violations, []);
});

test("共享真源自己就带 { singleTilde: false }", async () => {
  const modules = await appSourceModules();
  const source = modules.find(({ path }) => path === PLUGIN_SOURCE);
  assert.ok(source, `真源模块不见了: ${PLUGIN_SOURCE}`);

  // 上面两条只认 `remarkGfmPlugin` 这个名字,真源自己把选项翻回 true 就整条链失守。
  let disabled = false;
  function visit(node) {
    if (disablesSingleTilde(node)) disabled = true;
    ts.forEachChild(node, visit);
  }
  visit(source.module);
  assert.ok(disabled, `${PLUGIN_SOURCE} 必须写死 { singleTilde: false }`);
});

test("除真源外的生产模块不直接 import remark-gfm", async () => {
  const importers = [];
  for (const { path, module } of await appSourceModules()) {
    if (path === PLUGIN_SOURCE || EXEMPT.has(path)) continue;
    if (moduleSpecifiers(module).includes("remark-gfm")) importers.push(path);
  }
  assert.deepEqual(importers, []);
});
