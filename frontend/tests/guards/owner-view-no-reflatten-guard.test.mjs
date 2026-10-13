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
//
// ── 第二个目标:KnowhowPanel(debt-fix S4-d) ──────────────────────────────────
// `knowhow-panel.tsx` 拆分后,面板通过三个 owner hook 持有表列表、行代码、概念矩阵
// 三块领域状态(`useKnowhowTableList` / `useKnowhowRowCode` /
// `useKnowhowConceptMatrix`)。同一套「不再摊平」判据参数化后也作用在
// `KnowhowPanel` 上,且比 `Home` 更严,因为面板从零开始、没有历史包袱:
//  · 三个 hook 各在面板自己的函数体里调用恰好一次,结果绑定到一个简单标识符
//    (`const knowhowTableList = useKnowhowTableList(...)`),不解构;
//  · 顶层不逐字段摊平(与 `Home` 同一套链式判据;没有命名空间白名单、没有豁免);
//  · 绑定标识符的每一处引用都必须是属性访问的根(`knowhowTableList.view.x`、
//    `knowhowRowCode.loadRowCode(...)`)。裸引用一律违规——放进依赖数组(返回对象
//    每次渲染都是新的,effect/memo 会每帧重跑)、整体作为 JSX prop 或展开、塞进
//    对象字面量、再绑给别的名字,都会在这里被抓住。这一条覆盖嵌套函数体,因为在
//    回调里把整个返回对象传出去与在顶层传出去同样会泄漏「每帧新对象」。
// 面板的结构性约束(搬走的组件不回流、样式挂载点、hook 调用顺序、回调身份)在
// `knowhow-panel-boundary-guard.test.mjs`。
import test from "node:test";
import assert from "node:assert/strict";
import ts from "typescript";

import { findFunction, parseModule, parseText } from "../../test-support/semantic-source.mjs";

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

// KnowhowPanel 的三个 owner hook(函数名)。绑定名由调用点反查,不手抄。
const KNOWHOW_OWNER_HOOKS = ["useKnowhowTableList", "useKnowhowRowCode", "useKnowhowConceptMatrix"];

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

// 摊平检测,按 owner 函数参数化(原来只针对 `Home`,判据逐字不变)。返回违规声明的
// 规范化文本列表与反查出的命名空间别名集合。
//  · hookRoots:hook 返回值在 owner 里的绑定名集合;
//  · allowedNamespaceKeysByHook / grandfatheredDestructureHooks /
//    preExistingSingleUseAllowlist:`Home` 的既有放行项,KnowhowPanel 三者皆空。
function scanOwnerFlattening({
  sourceFile,
  owner,
  hookRoots,
  allowedNamespaceKeysByHook = new Map(),
  grandfatheredDestructureHooks = new Set(),
  preExistingSingleUseAllowlist = new Set(),
}) {
  // 解构初始值的「根 hook 名」——这里刻意不下钻 CallExpression
  // (`const { open } = rootModals.view("x");` 因此不算 hook 根解构,调用结果解构不是
  // 本守卫要拦的形状;这与 chainRoot 对纯标识符赋值放开调用是两回事,分别对应
  // 两种不同的既有写法)。
  //
  // 根既可以是 owner hook 之一,也可以是下面反查出的命名空间别名之一
  // (`namespaceAliasRoots`,例如 `kgGraph`)——命名空间别名本身就是一层命名空间级
  // 视图,`const { merged } = kgGraph;` 与直接 `const { graph: { merged } } =
  // kgWorkspace;` 是同一种再摊平,必须同罪认定。
  const namespaceAliasRoots = new Set();

  function destructureRootHookName(initializer) {
    let current = unwrapTransparent(initializer);
    while (ts.isPropertyAccessExpression(current) || ts.isElementAccessExpression(current)) {
      current = unwrapTransparent(current.expression);
    }
    if (!ts.isIdentifier(current)) return null;
    if (hookRoots.has(current.text) || namespaceAliasRoots.has(current.text)) {
      return current.text;
    }
    return null;
  }

  function declarationText(node) {
    return node.getText(sourceFile).replace(/\s+/g, " ").trim();
  }

  // (a) 命名空间别名反查:只认「根为某个 hook、且解构键全部落在该 hook 自己的
  // allowedNamespaceKeysByHook 白名单内、且没有其它绑定形态(rest/非标识符)」这
  // 一种合法命名空间级解构,把它绑定的本地别名并入根集合——之后
  // `kgGraph.merged` 这类深路径被再摊平成局部变量时(`const uGraphProbe =
  // kgGraph.merged;`)才能被同一套链式检测抓到。与主检测同样不下钻嵌套函数体。
  function collectNamespaceAliases(node) {
    if (node !== owner && isFunctionLike(node)) return;
    if (
      ts.isVariableDeclaration(node)
      && node.initializer
      && ts.isObjectBindingPattern(node.name)
    ) {
      const hookName = destructureRootHookName(node.initializer);
      const allowedKeys = hookName ? allowedNamespaceKeysByHook.get(hookName) : undefined;
      const bareRoot = unwrapTransparent(node.initializer);
      const isBareHookIdentifier = ts.isIdentifier(bareRoot) && bareRoot.text === hookName;
      if (allowedKeys && isBareHookIdentifier) {
        for (const element of node.name.elements) {
          if (element.dotDotDotToken || !ts.isIdentifier(element.name)) continue;
          const propertyName = (element.propertyName ?? element.name).getText(sourceFile);
          if (allowedKeys.has(propertyName)) namespaceAliasRoots.add(element.name.text);
        }
      }
    }
    ts.forEachChild(node, collectNamespaceAliases);
  }
  collectNamespaceAliases(owner);

  const flattenRoots = new Set([...hookRoots, ...namespaceAliasRoots]);

  function isHookRootedChain(expr) {
    const { root } = chainRoot(expr);
    return Boolean(root && flattenRoots.has(root));
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

  // (c) 不下钻进嵌套函数体——owner 自身除外。见文件头部说明。
  function visit(node) {
    if (node !== owner && isFunctionLike(node)) return;
    if (ts.isVariableDeclaration(node) && node.initializer) {
      const { name, initializer } = node;
      if (ts.isIdentifier(name)) {
        if (isFlattenedFieldInitializer(initializer)) {
          const text = declarationText(node);
          if (!preExistingSingleUseAllowlist.has(text)) violations.push(text);
        }
      } else if (ts.isObjectBindingPattern(name) || ts.isArrayBindingPattern(name)) {
        const hookName = destructureRootHookName(initializer);
        if (hookName) {
          if (grandfatheredDestructureHooks.has(hookName)) {
            // 先于本任务存在,改动范围之外——见文件顶部说明。
          } else if (ts.isArrayBindingPattern(name)) {
            // 没有任何生产用例这样写;一旦出现就保守地当违规处理。
            violations.push(declarationText(node));
          } else {
            const bareRoot = unwrapTransparent(initializer);
            const isBareHookIdentifier = ts.isIdentifier(bareRoot) && bareRoot.text === hookName;
            const allowedKeys = allowedNamespaceKeysByHook.get(hookName) ?? new Set();
            const boundElements = name.elements.filter((element) => !element.dotDotDotToken);
            // 命名空间级解构的每个绑定元素必须是简单标识符——`knowledge: { items }`
            // 这种嵌套 binding pattern 表面上 propertyName 仍是合法的 "knowledge",
            // 但 `element.name` 本身又是一层 ObjectBindingPattern/ArrayBindingPattern,
            // 等于在同一条声明里把 "knowledge" 命名空间再摊平到字段级,与顶层
            // `const uKnowledge = kgWorkspace.knowledge.items;` 同罪,必须一律违规。
            const allSimpleIdentifiers = boundElements.every((element) => ts.isIdentifier(element.name));
            const boundNames = boundElements
              .map((element) => (element.propertyName ?? element.name).getText(sourceFile));
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

  visit(owner);
  return { violations, namespaceAliasRoots };
}

// KnowhowPanel:每个 owner hook 的调用点。只认 owner 自己函数体内的调用(嵌套函数里
// 调 hook 本身就违反 hooks 规则,这里也一并记为违规);合规形状只有一种——调用是
// `const <标识符> = useX(...)` 这条声明的完整初始值。
function ownerHookBindings(sourceFile, owner, hookNames) {
  const calls = new Map(hookNames.map((hookName) => [hookName, []]));
  const violations = [];
  function visit(node, nested) {
    const enteringNested = nested || (node !== owner && isFunctionLike(node));
    if (
      ts.isCallExpression(node)
      && ts.isIdentifier(node.expression)
      && calls.has(node.expression.text)
    ) {
      const hookName = node.expression.text;
      const parent = node.parent;
      const bound = !nested
        && ts.isVariableDeclaration(parent)
        && parent.initializer === node
        && ts.isIdentifier(parent.name)
        && (parent.parent.flags & ts.NodeFlags.Const) !== 0;
      if (bound) {
        calls.get(hookName).push(parent.name.text);
      } else {
        calls.get(hookName).push(null);
        violations.push(`${hookName}(...) 没有作为 \`const <标识符> = ${hookName}(...)\` 在函数体内调用:`
          + ` ${parent.getText(sourceFile).replace(/\s+/g, " ").trim()}`);
      }
    }
    ts.forEachChild(node, (child) => visit(child, enteringNested));
  }
  visit(owner, false);
  for (const [hookName, names] of calls) {
    if (names.length !== 1) violations.push(`${hookName} 必须恰好调用一次,实际 ${names.length} 次`);
  }
  const roots = new Set([...calls.values()].flat().filter(Boolean));
  return { roots, violations };
}

function enclosingHookCall(node) {
  // 依赖数组:`useX(fn, [ ... ])` 第二个参数里的元素。
  const array = node.parent;
  if (!ts.isArrayLiteralExpression(array)) return null;
  const call = array.parent;
  if (
    ts.isCallExpression(call)
    && ts.isIdentifier(call.expression)
    && /^use[A-Z]/.test(call.expression.text)
    && call.arguments[1] === array
  ) {
    return call.expression.text;
  }
  return null;
}

// 绑定标识符的每一处引用都必须是属性访问的根。覆盖整个 owner(含嵌套函数体)。
function bareOwnerReferences(sourceFile, owner, roots) {
  const violations = [];
  function visit(node) {
    if (ts.isIdentifier(node) && roots.has(node.text)) {
      const parent = node.parent;
      const isDeclarationName = ts.isVariableDeclaration(parent) && parent.name === node;
      const isAccessRoot = ts.isPropertyAccessExpression(parent) && parent.expression === node;
      if (!isDeclarationName && !isAccessRoot) {
        const depsOf = enclosingHookCall(node);
        let where = "整体引用";
        if (depsOf) where = `放进 ${depsOf} 的依赖数组`;
        else if (ts.isJsxExpression(parent) || ts.isJsxSpreadAttribute(parent)) where = "整体作为 JSX prop 下传";
        else if (ts.isShorthandPropertyAssignment(parent) || ts.isSpreadAssignment(parent)) where = "整体塞进对象字面量";
        violations.push(`${node.text}:${where}(${parent.getText(sourceFile).replace(/\s+/g, " ").trim()})`);
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(owner);
  return violations;
}

// KnowhowPanel 的完整判据,供真实源码与下方自测共用。
function knowhowOwnerViolations(sourceFile, ownerName) {
  const owner = findFunction(sourceFile, ownerName);
  const { roots, violations: bindingViolations } = ownerHookBindings(sourceFile, owner, KNOWHOW_OWNER_HOOKS);
  const { violations: flattenViolations } = scanOwnerFlattening({ sourceFile, owner, hookRoots: roots });
  return {
    roots,
    violations: [
      ...bindingViolations,
      ...flattenViolations.map((text) => `逐字段摊平:${text}`),
      ...bareOwnerReferences(sourceFile, owner, roots),
    ],
  };
}

const homeScan = scanOwnerFlattening({
  sourceFile: page,
  owner: home,
  hookRoots: HOOK_ROOTS,
  allowedNamespaceKeysByHook: ALLOWED_NAMESPACE_KEYS_BY_HOOK,
  grandfatheredDestructureHooks: GRANDFATHERED_DESTRUCTURE_HOOKS,
  preExistingSingleUseAllowlist: PRE_EXISTING_SINGLE_USE_ALLOWLIST,
});

test("Home 函数体不再把 owner hook 的字段重新摊平成扁平局部变量", () => {
  assert.deepEqual(
    homeScan.violations,
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
  // 反查出的别名确实进了根集合(否则 `kgGraph.merged` 的再摊平会漏判)。
  assert.deepEqual([...homeScan.namespaceAliasRoots].sort(), ["kgGraph", "kgKnowledge", "kgSchema"]);
});

test("KnowhowPanel 的三个 owner hook 各调用一次、只经属性访问使用、不摊平", async () => {
  const panel = await parseModule("knowhow-panel.tsx");
  const { roots, violations } = knowhowOwnerViolations(panel, "KnowhowPanel");
  assert.deepEqual(violations, []);
  assert.deepEqual([...roots].sort(), ["knowhowConceptMatrix", "knowhowRowCode", "knowhowTableList"]);
});

// ── 自测:同一套 KnowhowPanel 判据跑在合成源码上,正例零违规、每个反例都被抓住 ──

const CLEAN_PANEL = `
function Panel({ notebookId }) {
  const knowhowTableList = useKnowhowTableList({ notebookId });
  const knowhowRowCode = useKnowhowRowCode(null);
  useEffect(() => { knowhowTableList.loadTables(); }, [notebookId]);
  function onSaved() { knowhowRowCode.handleCodeSaved("c", {}); }
  const knowhowConceptMatrix = useKnowhowConceptMatrix({ effects: { loadTables: knowhowTableList.loadTables } });
  return (
    <Grid
      onOpenConcept={knowhowConceptMatrix.setOpenConceptValue}
      group={knowhowConceptMatrix.view.openConceptGroup!}
      rows={knowhowRowCode.view.rowCodeByColumn}
      onSaved={onSaved}
    />
  );
}
`;

function selfTestViolations(body) {
  return knowhowOwnerViolations(parseText(body, "self-test.tsx"), "Panel").violations;
}

test("自测:KnowhowPanel 判据对合规写法零违规", () => {
  assert.deepEqual(selfTestViolations(CLEAN_PANEL), []);
});

const KNOWHOW_MUTANTS = [
  {
    label: "解构 hook 返回值",
    from: "const knowhowRowCode = useKnowhowRowCode(null);",
    to: "const { view, loadRowCode } = useKnowhowRowCode(null);",
    expect: /useKnowhowRowCode\(\.\.\.\) 没有作为/,
  },
  {
    label: "调用两次",
    from: "const knowhowRowCode = useKnowhowRowCode(null);",
    to: "const knowhowRowCode = useKnowhowRowCode(null);\n  const again = useKnowhowRowCode(null);",
    expect: /useKnowhowRowCode 必须恰好调用一次,实际 2 次/,
  },
  {
    label: "漏调用",
    from: "const knowhowRowCode = useKnowhowRowCode(null);",
    to: "",
    expect: /useKnowhowRowCode 必须恰好调用一次,实际 0 次/,
  },
  {
    label: "顶层逐字段摊平",
    from: "  function onSaved()",
    to: "  const tables = knowhowTableList.view.tables;\n  function onSaved()",
    expect: /逐字段摊平:tables = knowhowTableList\.view\.tables/,
  },
  {
    label: "摊平成带字面量默认值的局部变量",
    from: "  function onSaved()",
    to: "  const group = knowhowConceptMatrix.view.openConceptGroup ?? null;\n  function onSaved()",
    expect: /逐字段摊平:group = knowhowConceptMatrix\.view\.openConceptGroup \?\? null/,
  },
  {
    label: "整个返回对象放进依赖数组",
    from: "}, [notebookId]);",
    to: "}, [notebookId, knowhowTableList]);",
    expect: /knowhowTableList:放进 useEffect 的依赖数组/,
  },
  {
    label: "整体作为 JSX prop 下传",
    from: "onSaved={onSaved}",
    to: "onSaved={onSaved}\n      rowCode={knowhowRowCode}",
    expect: /knowhowRowCode:整体作为 JSX prop 下传/,
  },
  {
    label: "整体展开成 JSX props",
    from: "onSaved={onSaved}",
    to: "onSaved={onSaved}\n      {...knowhowConceptMatrix}",
    expect: /knowhowConceptMatrix:整体作为 JSX prop 下传/,
  },
  {
    label: "在回调里整体塞进对象字面量",
    from: 'knowhowRowCode.handleCodeSaved("c", {});',
    to: 'report({ knowhowRowCode });',
    expect: /knowhowRowCode:整体塞进对象字面量/,
  },
  {
    label: "在嵌套函数里调用 hook",
    from: 'function onSaved() { knowhowRowCode.handleCodeSaved("c", {}); }',
    to: 'function onSaved() { const late = useKnowhowTableList({}); late.loadTables(); }',
    expect: /useKnowhowTableList\(\.\.\.\) 没有作为/,
  },
];

for (const mutant of KNOWHOW_MUTANTS) {
  test(`自测:KnowhowPanel 判据抓住「${mutant.label}」`, () => {
    assert.ok(CLEAN_PANEL.includes(mutant.from), `锚点不存在:${mutant.from}`);
    assert.equal(CLEAN_PANEL.replaceAll(mutant.from, mutant.to), CLEAN_PANEL.replace(mutant.from, mutant.to), `锚点不唯一:${mutant.from}`);
    const violations = selfTestViolations(CLEAN_PANEL.replace(mutant.from, mutant.to));
    assert.ok(
      violations.some((violation) => mutant.expect.test(violation)),
      `没有抓到:${JSON.stringify(violations)}`,
    );
  });
}
