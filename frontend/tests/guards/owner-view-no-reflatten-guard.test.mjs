// page.tsx 曾经在 `Home` 顶部把三个 owner hook 的返回值重新摊平成一批扁平局部
// 变量(逐字段 `const uGraph = kgWorkspace.graph.graph;` 这种),纯粹是为了让 JSX
// 不用改。那份 shim(kgWorkspace/rootModals/notebookCollection 三段)已经删掉,
// 使用点全部改成直接访问 hook 命名空间。这条守卫钉住「不再回潮」:`Home` 函数体内
// 任何 `const X = <hook>.<ns>.<field>` / `const X = <hook>.<field>` 形态(初始值是
// 以七个 owner hook 之一为根的 PropertyAccessExpression/ElementAccessExpression
// 链,且不是调用、不是解构)一律违规。唯一放行的新写法是**命名空间级**解构——
// `const { knowledge, schema, graph } = kgWorkspace;` 这种把 kgWorkspace 拆成三个
// 子命名空间对象、而不是逐字段摊平。
//
// ⚠ `askSession`/`sourceLibrary` 各自已有一段**先于本任务存在**的大段逐字段解构
// (`const { question, turns, ... } = askSession;`、`const { sources, ... } =
// sourceLibrary;`)。它们与本任务要删的三段 shim 是同一种「反模式」,但改掉它们是
// 完全独立、影响面大得多的另一件事(数十个字段、遍布全函数的读写点),不在本次
// 「删掉 kgWorkspace/rootModals/notebookCollection 三段 shim」的范围内。本守卫按
// hook 名分别处理这两类:kgWorkspace 的解构必须落在 {knowledge,schema,graph} 白
// 名单内,askSession/sourceLibrary 的既有解构原样放行(结构性豁免,不按文本比对,
// 免得字段增删就要跟着改守卫),其余四个 hook(rootModals/notebookCollection/
// reportWorkspace/workspaceExtensions)不解构——出现即违规。
//
// 判据按 AST 形状,不含行号(本文件禁止 `.getStart()`/`.getEnd()`/`.getFullStart()`,
// 见 static-source-policy 守卫)。
import test from "node:test";
import assert from "node:assert/strict";
import ts from "typescript";

import { findFunction, parseModule } from "../../test-support/semantic-source.mjs";

const page = await parseModule("page.tsx");
const home = findFunction(page, "Home");

const HOOK_ROOTS = new Set([
  "kgWorkspace",
  "rootModals",
  "notebookCollection",
  "askSession",
  "reportWorkspace",
  "sourceLibrary",
  "workspaceExtensions",
]);

// kgWorkspace 的三个子命名空间是唯一允许保留的解构键——用来把
// `kgWorkspace.graph.searchHits` 这类深路径缩成 `kgGraph.searchHits`,而不是把每个
// 叶子字段各摊平成一个局部变量。
const ALLOWED_NAMESPACE_KEYS = new Set(["knowledge", "schema", "graph"]);

// 结构性豁免:这两个 hook 各自已有一段先于本任务存在的整体解构,改掉它们是独立
// 任务,见文件顶部说明。
const GRANDFATHERED_DESTRUCTURE_HOOKS = new Set(["askSession", "sourceLibrary"]);

// 两条**先于**本任务、与被删的 kgWorkspace/rootModals/notebookCollection 三段 shim
// 无关的单次属性提取。它们不是「把整个 hook 摊平成一批扁平别名供全函数复用」,只是
// 各自唯一使用点附近的一次性属性读取,不在本任务改动范围内。按整条声明的规范化
// 文本(不含 `const`/`let`/分号,`node.getText()` 的原生形状)精确匹配,防止同名但
// 换了链路的新声明蒙混过关。
const PRE_EXISTING_SINGLE_USE_ALLOWLIST = new Set([
  "workspaceExtensionProjection = workspaceExtensions.projection",
  "loadSourceElementPage = sourceLibrary.loadSourceElementPage",
]);

function unwrapTransparent(expr) {
  let current = expr;
  while (ts.isParenthesizedExpression(current) || ts.isNonNullExpression(current)) {
    current = current.expression;
  }
  return current;
}

// 只在最外层就是属性/元素访问时才算「链」——调用表达式(`rootModals.view("x")`)
// 一律不算,即便它的 callee 是属性访问。
function isHookRootedPropertyChain(expr) {
  let current = unwrapTransparent(expr);
  if (!ts.isPropertyAccessExpression(current) && !ts.isElementAccessExpression(current)) {
    return false;
  }
  while (ts.isPropertyAccessExpression(current) || ts.isElementAccessExpression(current)) {
    current = unwrapTransparent(current.expression);
  }
  return ts.isIdentifier(current) && HOOK_ROOTS.has(current.text);
}

function isSimpleLiteralDefault(expr) {
  if (expr.kind === ts.SyntaxKind.NullKeyword) return true;
  if (ts.isNumericLiteral(expr)) return true;
  if (ts.isStringLiteralLike(expr)) return true;
  if (ts.isArrayLiteralExpression(expr) && expr.elements.length === 0) return true;
  return false;
}

// 逐字段摊平的两种形状:纯属性链,或者「属性链 ?? 一个字面量默认值」
// (`notebookCollection.editor?.mountable ?? []` 这种)。
function isFlattenedFieldInitializer(initializer) {
  if (isHookRootedPropertyChain(initializer)) return true;
  return (
    ts.isBinaryExpression(initializer)
    && initializer.operatorToken.kind === ts.SyntaxKind.QuestionQuestionToken
    && isHookRootedPropertyChain(initializer.left)
    && isSimpleLiteralDefault(initializer.right)
  );
}

// 解构初始值的「根 hook 名」——不像上面那个只在最外层拒绝调用,这里同样不下钻进
// CallExpression(`const { open } = rootModals.view("x");` 因此不算 hook 根解构,
// 调用结果解构不是本守卫要拦的形状)。
function destructureRootHookName(initializer) {
  let current = unwrapTransparent(initializer);
  while (ts.isPropertyAccessExpression(current) || ts.isElementAccessExpression(current)) {
    current = unwrapTransparent(current.expression);
  }
  return ts.isIdentifier(current) && HOOK_ROOTS.has(current.text) ? current.text : null;
}

function declarationText(node) {
  return node.getText(page).replace(/\s+/g, " ").trim();
}

const violations = [];

function visit(node) {
  if (ts.isVariableDeclaration(node) && node.initializer) {
    const { name, initializer } = node;
    if (ts.isIdentifier(name)) {
      if (!ts.isCallExpression(initializer) && isFlattenedFieldInitializer(initializer)) {
        const text = declarationText(node);
        if (!PRE_EXISTING_SINGLE_USE_ALLOWLIST.has(text)) violations.push(text);
      }
    } else if (ts.isObjectBindingPattern(name) || ts.isArrayBindingPattern(name)) {
      const hookName = destructureRootHookName(initializer);
      if (hookName) {
        if (GRANDFATHERED_DESTRUCTURE_HOOKS.has(hookName)) {
          // 先于本任务存在,改动范围之外——见文件顶部说明。
        } else if (ts.isArrayBindingPattern(name)) {
          // 没有任何生产用例这样写;一旦出现就保守地当违规处理。
          violations.push(declarationText(node));
        } else {
          const bareRoot = unwrapTransparent(initializer);
          const isBareHookIdentifier = ts.isIdentifier(bareRoot) && bareRoot.text === hookName;
          const boundNames = name.elements
            .filter((element) => !element.dotDotDotToken)
            .map((element) => (element.propertyName ?? element.name).getText(page));
          const onlyNamespaceKeys = boundNames.every((boundName) => ALLOWED_NAMESPACE_KEYS.has(boundName));
          if (!isBareHookIdentifier || !onlyNamespaceKeys) {
            violations.push(declarationText(node));
          }
        }
      }
    }
  }
  ts.forEachChild(node, visit);
}

visit(home);

test("Home 函数体不再把 owner hook 的字段重新摊平成扁平局部变量", () => {
  assert.deepEqual(
    violations,
    [],
    "发现逐字段的 hook 摊平声明——改成在使用点直接访问 hook 命名空间,"
      + "或者(仅限 kgWorkspace)命名空间级解构 { knowledge, schema, graph }",
  );
});

test("命名空间级解构本身不会被误伤", () => {
  assert.match(
    home.getText(page),
    /const \{ knowledge: kgKnowledge, schema: kgSchema, graph: kgGraph \} = kgWorkspace;/,
  );
});
