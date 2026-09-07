// 回归门:正文引用图片的落位管线只有一条,而且四个 Markdown 渲染面各自的接与不接
// 都是**登记过的**决定。
//
// 背景(设计稿 2026-08-18 §2 的一期与二期):Ask 的正文引用图片由
// `rehype-citation-images` 统一落位(块级插槽、跨引用按资产去重、`CitationImageOrder`
// 记账供放大预览左右切换)。一期上线站内问答 + 公开会话分享页,二期把同一条管线接到
// 深度报告正文。四个面各有自己的 `<ReactMarkdown>` 实例(不复用同一个组件),所以
// 「谁接了」不是 tsc 能保证的东西——漏接只表现为那个面上一张图都不出现。
//
// 判据两条:
//   ① 接图的三个面,每个 `rehypePlugins` 数组里都要有 `rehypeCitationImages` 条目;
//   ② 公开报告页 `r/[token]` 必须**没有**它——那不是遗漏,是投影决定:
//      `backend/app/services/report_public_view.py` 的白名单里没有 images,也没有
//      element_id/source_id 之类可寻址的句柄,并且公开报告没有任何免登录的资产读取
//      端点(公开会话页有 `publicConversationImageUrl` 那条 alias 路径,报告没有对应
//      物)。要在那个面上图,得先扩投影 + 开一个新的匿名资产端点,是独立一件事。
//      这条反向断言的作用是:哪天真的做了,守卫会响,提醒把这段注释与文档一起改。
//
// 覆盖边界:本守卫只证明「插件挂上了/没挂」,不证明它拿到的 imageIdsByKey 是对的——
// 那由 `answer-citation-images` / `report-citation-images` 组件测试负责。
import test from "node:test";
import assert from "node:assert/strict";
import ts from "typescript";

import { appSourceModules } from "../../test-support/semantic-source.mjs";

/** 接了内联引用图片的面。少一个就是覆盖缩水(重命名/搬走时守卫必须响)。 */
const INLINE_IMAGE_SURFACES = [
  "answer-markdown.tsx",
  "report-view.tsx",
  "c/[token]/page.tsx",
];
/** 刻意不接的面(理由见文件头)。 */
const DELIBERATELY_WITHOUT = "r/[token]/page.tsx";
const PLUGIN_IDENTIFIER = "rehypeCitationImages";

/** `rehypePlugins={...}` 的表达式节点(每个 `<ReactMarkdown>` 一个)。 */
function rehypePluginsExpressions(sourceFile) {
  const found = [];
  function visit(node) {
    if (
      (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node))
      && node.tagName.getText(sourceFile) === "ReactMarkdown"
    ) {
      for (const attribute of node.attributes.properties) {
        if (
          ts.isJsxAttribute(attribute)
          && attribute.name.getText(sourceFile) === "rehypePlugins"
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

/** 数组里是否有 `rehypeCitationImages` 条目(裸标识符或 `[plugin, ...] as [...]` 元组)。 */
function hasPluginEntry(expression) {
  if (!ts.isArrayLiteralExpression(expression)) return false;
  return expression.elements.some((entry) => {
    const bare = ts.isAsExpression(entry) || ts.isParenthesizedExpression(entry)
      ? entry.expression
      : entry;
    if (ts.isIdentifier(bare)) return bare.text === PLUGIN_IDENTIFIER;
    if (ts.isArrayLiteralExpression(bare)) {
      const [head] = bare.elements;
      return Boolean(head) && ts.isIdentifier(head) && head.text === PLUGIN_IDENTIFIER;
    }
    return false;
  });
}

test("接内联引用图片的每个 ReactMarkdown 都挂了 rehypeCitationImages", async () => {
  const scanned = [];
  const violations = [];
  for (const { path, module } of await appSourceModules()) {
    if (!INLINE_IMAGE_SURFACES.includes(path)) continue;
    const expressions = rehypePluginsExpressions(module);
    if (expressions.length === 0) {
      violations.push(`${path}: 没有任何 <ReactMarkdown rehypePlugins={...}>`);
      continue;
    }
    scanned.push(path);
    for (const expression of expressions) {
      if (!ts.isArrayLiteralExpression(expression)) {
        violations.push(`${path}: rehypePlugins 不是数组字面量,守卫读不出插件列表`);
      } else if (!hasPluginEntry(expression)) {
        violations.push(`${path}: rehypePlugins 缺 ${PLUGIN_IDENTIFIER}`);
      }
    }
  }

  for (const surface of INLINE_IMAGE_SURFACES) {
    assert.ok(scanned.includes(surface), `没扫到渲染面 ${surface}: ${scanned.join(", ")}`);
  }
  assert.deepEqual(violations, []);
});

test("公开报告页仍然不接内联引用图片(公开投影无 images、无匿名资产端点)", async () => {
  let scanned = false;
  for (const { path, module } of await appSourceModules()) {
    if (path !== DELIBERATELY_WITHOUT) continue;
    scanned = true;
    const expressions = rehypePluginsExpressions(module);
    assert.ok(expressions.length > 0, `${path}: 没有任何 <ReactMarkdown rehypePlugins={...}>`);
    for (const expression of expressions) {
      assert.equal(
        hasPluginEntry(expression),
        false,
        `${path} 挂上了 ${PLUGIN_IDENTIFIER}——这需要公开报告投影先带上 images 并开出`
          + "匿名资产端点,同时更新 report_public_view.py 的白名单说明与 docs/product-and-api*.md",
      );
    }
  }
  assert.ok(scanned, `没扫到 ${DELIBERATELY_WITHOUT}`);
});

/**
 * `className` 是否命中 `answer-inline-images` 区块:接受
 *   - 字符串字面量,要求整串就是 `answer-inline-images`(与曾经的原始文本判据
 *     同样严格,不放过 `"answer-inline-images foo"` 这类拼接进别的 class 的写法);
 *   - 模板字面量(无替换或带替换),只要求 head/首段以 `answer-inline-images`
 *     开头——`className={`answer-inline-images ${x}`}` 这种常见的「基础 class +
 *     动态后缀」写法必须能被抓到。
 * 走 AST 而不是原始文本 `includes(...)`,顺带修掉了旧判据的一个假阳性:一段
 * 提到这行字符串的**注释**会被 `getFullText().includes(...)` 命中,却不是真实
 * JSX。注释不出现在这里遍历的语法节点里,天然不会被算作 owner。
 */
function classNameMatchesInlineImages(expression) {
  if (ts.isStringLiteral(expression)) return expression.text === "answer-inline-images";
  if (ts.isNoSubstitutionTemplateLiteral(expression)) {
    return expression.text.startsWith("answer-inline-images");
  }
  if (ts.isTemplateExpression(expression)) {
    return expression.head.text.startsWith("answer-inline-images");
  }
  return false;
}

/** 该模块里是否存在一个 `className=...` 命中上述判据的 JSX 属性。 */
function hasInlineImagesClassName(sourceFile) {
  let found = false;
  function visit(node) {
    if (
      !found
      && ts.isJsxAttribute(node)
      && node.name.getText(sourceFile) === "className"
      && node.initializer
    ) {
      if (ts.isStringLiteral(node.initializer)) {
        found = classNameMatchesInlineImages(node.initializer);
      } else if (
        ts.isJsxExpression(node.initializer)
        && node.initializer.expression
      ) {
        found = classNameMatchesInlineImages(node.initializer.expression);
      }
    }
    if (!found) ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return found;
}

// 落位规则、去重与预览记账只能有一份实现:`rehype-citation-images` 自己,加上渲染它
// 产出的插槽的共享组件 `inline-citation-images`。谁再写一份 `answer-inline-images`
// 的 JSX,两个面的 alt/aria-label 就会开始漂移(那正是二期抽共享件的理由)。
//
// 这条守卫认的不是「只有一份实现」——`c/[token]/page.tsx`(公开会话分享页)本来
// 就有第二处 `className="answer-inline-images"`:公开页没有鉴权 fetch,不能复用
// `inline-citation-images.tsx` 里那个基于 `AuthedImage` 的实现,只能自己再写一份
// 用裸 `<img>` 的等价 JSX(见该文件的 `PublicMarkdownAside` 与旧分享兼容分支)。
// 这是**登记过的**第二个合法 owner,不是遗漏——守卫要认的是「只有这两个」这条
// 允许清单,任何第三处出现就是真的分叉。
test("正文图片区块的 JSX 只有两个登记在案的 owner(共享实现 + 公开页的免鉴权副本)", async () => {
  const owners = [];
  for (const { path, module } of await appSourceModules()) {
    if (hasInlineImagesClassName(module)) owners.push(path);
  }
  assert.deepEqual(owners.sort(), ["c/[token]/page.tsx", "inline-citation-images.tsx"]);
});
