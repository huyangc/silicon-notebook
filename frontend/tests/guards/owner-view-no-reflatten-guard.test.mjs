// page.tsx 曾经在 `Home` 顶部把三个 owner hook 的返回值重新摊平成一批扁平局部
// 变量(逐字段 `const uGraph = kgWorkspace.graph.graph;` 这种),纯粹是为了让 JSX
// 不用改。那份 shim(kgWorkspace/rootModals/notebookCollection 三段)已经删掉,
// 使用点全部改成直接访问 hook 命名空间。这条守卫钉住「不再回潮」:`Home` 函数体内
// 任何 `const X = <hook>.<ns>.<field>` / `const X = <hook>.<field>` 形态(初始值是
// 以七个 owner hook 之一、或下面反查出的命名空间别名之一为根的
// PropertyAccessExpression/ElementAccessExpression/CallExpression 混合链,且不是
// 解构、最终赋给一个局部 `const`)一律违规,不论链上是否夹着方法调用
// (`rootModals.view("x")`、`rootModals.view("x").open` 都算——链底是不是调用不影响
// 判据,唯一免检的是显式 allowlist)。唯一放行的新写法是**命名空间级**解构——
// `const { knowledge, schema, graph } = kgWorkspace;` 这种把 kgWorkspace 拆成三个
// 子命名空间对象、而不是逐字段摊平。
//
// 判据边界(本次加固的三处):
// (a) 命名空间别名(如 kgKnowledge/kgSchema/kgGraph)会被自动反查进检测用的根集合
//     ——见下面 `collectNamespaceAliases`——而不是手抄三个字符串常量:它们从
//     `ALLOWED_NAMESPACE_KEYS_BY_HOOK` 描述的合法命名空间级解构里反查绑定名,新增
//     一个命名空间键会自动生效,不需要同步改这份反查逻辑本身。这样
//     `const uGraphProbe = kgGraph.merged;` 才会被识别成「对命名空间别名的再摊平」
//     ,与直接对 `kgWorkspace.graph.merged` 摊平同罪。
// (b) 检测不再要求链的最外层不是 CallExpression:`rootModals.view("x")`(整个初始值
//     就是一次调用)与 `rootModals.view("x").open`(调用嵌在链中间)都按同一套「链
//     底追溯到 hook/别名根」的逻辑识别,唯一豁免路径是按整条声明规范化文本比对的
//     `PRE_EXISTING_SINGLE_USE_ALLOWLIST`(结构豁免而非"调用就放过")。跑过一遍现有
//     `Home` 源码后没有发现被误杀的既有调用形态,因此本次没有新增豁免条目。
// (c) 不下钻进嵌套函数体(FunctionExpression/ArrowFunction/FunctionDeclaration,
//     `home` 自身除外)。原来的 shim 问题是"在 `Home` 顶层作用域把 hook 字段一次性
//     摊平、供全函数到处复用";一个只在某个事件回调/effect 闭包内部就地读一次
//     hook 命名空间字段做类型收窄的局部 `const`(例如
//     `onEdit={() => { const target = notebookCollection.menu.notebook; ... }}`)
//     结构上不是同一种反模式——它的作用域就是那个回调,不会被别处引用,也不会像
//     顶层摊平那样把 hook 命名空间的边界模糊掉。因此检测只看 `Home` 自己的语句树,
//     碰到嵌套函数体就整体跳过,不下钻。
// (d) `ALLOWED_NAMESPACE_KEYS_BY_HOOK` 按 hook 分表,而不是用一个全局命名空间键
//     集合套所有 hook——后者会让"任何 hook 解构出一个恰好叫 knowledge/schema/graph
//     的键"都被误判成合法,即便那个键实际上并不存在于该 hook 上。分表后每个 hook
//     的合法命名空间键各自独立声明,判据是"这个键在该 hook 上映射的值本身还是一层
//     命名空间对象(要再 `.field` 才能拿到叶子数据)",不是任意字符串巧合相等。
// (e) `destructureRootHookName` 现在同时认根为 hook 名或 `namespaceAliasRoots`
//     里的命名空间别名——`const { merged } = kgGraph;` 因此与 `const { graph:
//     { merged } } = kgWorkspace;` 同罪,不再因为根标识符不在 `HOOK_ROOTS` 里就
//     被整条豁免。
// (f) 命名空间级解构的每个绑定元素必须是简单标识符:`const { knowledge: { items }
//     } = kgWorkspace;` 这种嵌套 binding pattern,`propertyName` 仍是合法的
//     "knowledge",但 `element.name` 又是一层解构模式——那是把 "knowledge" 命名
//     空间继续摊平到字段级,判据必须同时要求 `ts.isIdentifier(element.name)`,只
//     比对 propertyName 会漏判。
//
// ⚠ `askSession`/`sourceLibrary` 各自已有一段**先于本任务存在**的大段逐字段解构
// (`const { question, turns, ... } = askSession;`、`const { sources, ... } =
// sourceLibrary;`)。它们与本任务要删的三段 shim 是同一种「反模式」,但改掉它们是
// 完全独立、影响面大得多的另一件事(数十个字段、遍布全函数的读写点),不在本次
// 「删掉 kgWorkspace/rootModals/notebookCollection 三段 shim」的范围内。本守卫按
// hook 名分别处理这两类:kgWorkspace 的解构必须落在它自己的命名空间白名单内,
// askSession/sourceLibrary 的既有解构原样放行(结构性豁免,不按文本比对,免得字段
// 增删就要跟着改守卫),其余四个 hook(rootModals/notebookCollection/
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

// 每个 hook 各自的合法命名空间级解构键——键的值本身是一层命名空间状态对象
// (还要再 `.field` 才能拿到叶子数据),不是任意叶子字段。kgWorkspace 的三段已经
// 在下面第二个测试里断言存在;notebookCollection 的三段目前**尚未**这样解构
// (本 PR 不做,是独立一件事)——放进这张表只是让守卫在未来出现同构写法时不会把它
// 错判成摊平违规,不代表这次已经批准了那次解构。
const ALLOWED_NAMESPACE_KEYS_BY_HOOK = new Map([
  ["kgWorkspace", new Set(["knowledge", "schema", "graph"])],
  ["notebookCollection", new Set(["editor", "deletion", "menu"])],
]);

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

function isFunctionLike(node) {
  return (
    ts.isFunctionExpression(node)
    || ts.isArrowFunction(node)
    || ts.isFunctionDeclaration(node)
  );
}

// 链底追溯:同时解包 PropertyAccessExpression/ElementAccessExpression/
// CallExpression(三者都用 `.expression` 继续往里走),返回根标识符名(找不到就是
// null)与"链上是否夹着至少一次调用"。必须先看到至少一环 Property/Element/Call
// 才进入追溯——裸标识符(`const x = rootModals;`,整个 hook 被原样重新绑定,不是
// 摊平字段)不算违规形状。
function chainRoot(expr) {
  let current = unwrapTransparent(expr);
  if (
    !ts.isPropertyAccessExpression(current)
    && !ts.isElementAccessExpression(current)
    && !ts.isCallExpression(current)
  ) {
    return { root: null, hasCall: false };
  }
  let hasCall = false;
  while (
    ts.isPropertyAccessExpression(current)
    || ts.isElementAccessExpression(current)
    || ts.isCallExpression(current)
  ) {
    if (ts.isCallExpression(current)) hasCall = true;
    current = unwrapTransparent(current.expression);
  }
  return { root: ts.isIdentifier(current) ? current.text : null, hasCall };
}

function isSimpleLiteralDefault(expr) {
  if (expr.kind === ts.SyntaxKind.NullKeyword) return true;
  if (ts.isNumericLiteral(expr)) return true;
  if (ts.isStringLiteralLike(expr)) return true;
  if (ts.isArrayLiteralExpression(expr) && expr.elements.length === 0) return true;
  return false;
}

// 解构初始值的「根 hook 名」——这里刻意不下钻 CallExpression
// (`const { open } = rootModals.view("x");` 因此不算 hook 根解构,调用结果解构不是
// 本守卫要拦的形状;这与上面 chainRoot 对纯标识符赋值放开调用是两回事,分别对应
// 两种不同的既有写法)。
//
// 根既可以是七个 owner hook 之一,也可以是下面反查出的命名空间别名之一
// (`namespaceAliasRoots`,例如 `kgGraph`)——命名空间别名本身就是一层命名空间级
// 视图,`const { merged } = kgGraph;` 与直接 `const { graph: { merged } } =
// kgWorkspace;` 是同一种再摊平,必须同罪认定。函数定义时 `namespaceAliasRoots`
// 尚未声明,但本函数只在它声明之后才会被调用(见下方 `collectNamespaceAliases`/
// `visit` 的调用时机),按引用捕获、无 TDZ 风险。
function destructureRootHookName(initializer) {
  let current = unwrapTransparent(initializer);
  while (ts.isPropertyAccessExpression(current) || ts.isElementAccessExpression(current)) {
    current = unwrapTransparent(current.expression);
  }
  if (!ts.isIdentifier(current)) return null;
  if (HOOK_ROOTS.has(current.text) || namespaceAliasRoots.has(current.text)) {
    return current.text;
  }
  return null;
}

function declarationText(node) {
  return node.getText(page).replace(/\s+/g, " ").trim();
}

// (a) 命名空间别名反查:只认「根为某个 hook、且解构键全部落在该 hook 自己的
// ALLOWED_NAMESPACE_KEYS_BY_HOOK 白名单内、且没有其它绑定形态(rest/非标识符)」这
// 一种合法命名空间级解构,把它绑定的本地别名并入下面的根集合——之后
// `kgGraph.merged` 这类深路径被再摊平成局部变量时(`const uGraphProbe =
// kgGraph.merged;`)才能被同一套链式检测抓到。与主检测同样不下钻嵌套函数体。
const namespaceAliasRoots = new Set();

function collectNamespaceAliases(node) {
  if (node !== home && isFunctionLike(node)) return;
  if (
    ts.isVariableDeclaration(node)
    && node.initializer
    && ts.isObjectBindingPattern(node.name)
  ) {
    const hookName = destructureRootHookName(node.initializer);
    const allowedKeys = hookName ? ALLOWED_NAMESPACE_KEYS_BY_HOOK.get(hookName) : undefined;
    const bareRoot = unwrapTransparent(node.initializer);
    const isBareHookIdentifier = ts.isIdentifier(bareRoot) && bareRoot.text === hookName;
    if (allowedKeys && isBareHookIdentifier) {
      for (const element of node.name.elements) {
        if (element.dotDotDotToken || !ts.isIdentifier(element.name)) continue;
        const propertyName = (element.propertyName ?? element.name).getText(page);
        if (allowedKeys.has(propertyName)) namespaceAliasRoots.add(element.name.text);
      }
    }
  }
  ts.forEachChild(node, collectNamespaceAliases);
}
collectNamespaceAliases(home);

const FLATTEN_ROOTS = new Set([...HOOK_ROOTS, ...namespaceAliasRoots]);

function isHookRootedChain(expr) {
  const { root } = chainRoot(expr);
  return Boolean(root && FLATTEN_ROOTS.has(root));
}

// 逐字段摊平的两种形状:纯链(可能夹着调用),或者「链 ?? 一个字面量默认值」
// (`notebookCollection.editor?.mountable ?? []` 这种)。
function isFlattenedFieldInitializer(initializer) {
  if (isHookRootedChain(initializer)) return true;
  return (
    ts.isBinaryExpression(initializer)
    && initializer.operatorToken.kind === ts.SyntaxKind.QuestionQuestionToken
    && isHookRootedChain(initializer.left)
    && isSimpleLiteralDefault(initializer.right)
  );
}

const violations = [];

// (c) 不下钻进嵌套函数体——`home` 自身除外。见文件头部说明。
function visit(node) {
  if (node !== home && isFunctionLike(node)) return;
  if (ts.isVariableDeclaration(node) && node.initializer) {
    const { name, initializer } = node;
    if (ts.isIdentifier(name)) {
      if (isFlattenedFieldInitializer(initializer)) {
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
          const allowedKeys = ALLOWED_NAMESPACE_KEYS_BY_HOOK.get(hookName) ?? new Set();
          const boundElements = name.elements.filter((element) => !element.dotDotDotToken);
          // 命名空间级解构的每个绑定元素必须是简单标识符——`knowledge: { items }`
          // 这种嵌套 binding pattern 表面上 propertyName 仍是合法的 "knowledge",
          // 但 `element.name` 本身又是一层 ObjectBindingPattern/ArrayBindingPattern,
          // 等于在同一条声明里把 "knowledge" 命名空间再摊平到字段级,与顶层
          // `const uKnowledge = kgWorkspace.knowledge.items;` 同罪,必须一律违规。
          const allSimpleIdentifiers = boundElements.every((element) => ts.isIdentifier(element.name));
          const boundNames = boundElements
            .map((element) => (element.propertyName ?? element.name).getText(page));
          const onlyNamespaceKeys = allSimpleIdentifiers
            && boundNames.length > 0
            && boundNames.every((boundName) => allowedKeys.has(boundName));
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
      + "或者(仅限白名单内的 hook)命名空间级解构 { knowledge, schema, graph }",
  );
});

test("命名空间级解构本身不会被误伤", () => {
  assert.match(
    home.getText(page),
    /const \{ knowledge: kgKnowledge, schema: kgSchema, graph: kgGraph \} = kgWorkspace;/,
  );
});
