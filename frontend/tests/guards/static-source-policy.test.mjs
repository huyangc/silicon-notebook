import test from "node:test";
import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

import { STATIC_SOURCE_CONTRACTS } from "../../test-support/static-source-contracts.mjs";


const FRONTEND_DIR = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../..",
);
const APP_DIR = path.join(FRONTEND_DIR, "app");
const POLICY_ROOTS = [
  APP_DIR,
  path.join(FRONTEND_DIR, "tests"),
  path.join(FRONTEND_DIR, "test-support"),
];
const DIRECT_READ_ALLOWLIST = new Set([
  // Reads committed cross-language golden data, never production source text.
  "tests/unit/knowhow-normalize.test.mjs",
  // Reads the committed backend/frontend UI-contribution parity fixture; the
  // production registry itself is imported as a module, not scanned as text.
  "tests/guards/extension-ui-parity.test.mjs",
  // Central semantic AST helper: callers consume nodes, never text positions.
  "test-support/semantic-source.mjs",
  // Synthetic module names used by this guard's mutation tests.
  "test/semantic-source.mjs",
  // Contains this policy's mutation strings, not production source inspection.
  "tests/guards/static-source-policy.test.mjs",
  // Reads package/config metadata only.
  "tests/guards/test-runner-config.test.mjs",
  "tests/guards/test-location-guard.test.mjs",
  // Reads globals.css to compare selector specificity. Not production TS source and
  // not a source-position query — a stylesheet cascade contract has no AST to consume,
  // and jsdom's getComputedStyle ignores specificity, so text is the only honest input.
  "tests/guards/effort-picker-style-guard.test.mjs",
  // 同上,只读 globals.css:断言提问导航锚在对话区那一行(grid-row),而不是对整个
  // 面板绝对居中。样式表没有可消费的 AST,jsdom 又不做 grid 布局(量出来的 rect
  // 恒为 0),文本是唯一诚实的输入。对 page.tsx 的断言仍走语义解析。
  "tests/unit/chat-turn-nav-anchor.test.mjs",
  // 同上,只读 globals.css:断言按节合成的答案标题字阶收窄在 `.chat-answer` 容器链下,
  // 不泄漏进共用同一个 `.answer-markdown` 渲染管线类的深度报告。样式表没有可消费的
  // AST,jsdom 又不实现特异性级联,文本是唯一诚实的输入。同文件对 report-view.tsx 的
  // 断言仍走 semantic-source 的语义解析。
  "tests/guards/answer-heading-scope-guard.test.mjs",
  // 同上,只读 globals.css:断言 `.source-agent-badge` 与 `.source-kg-badge` 共用同一个
  // 选择器组(同一套中性 pill),且该声明块内不含 danger/warning 警示 token、也不含
  // `.source-kg-badge` 独有的 `pointer-events: none`(否则会重新压住徽标的 title 悬浮
  // 提示)。样式表没有可消费的 AST,文本是唯一诚实的输入;文件里对 page.tsx 门控条件
  // 与徽标元素的断言仍走 semantic-source 的语义解析,不读裸文本。
  "tests/guards/source-agent-badge-guard.test.mjs",
  // 同上,只读 globals.css:断言群组界面的行(`.group-row`)自己是 flex —— 它此前是块级的
  // `.checklist-row` 加内联 alignItems/gap,那两个属性在块级盒子上静默无效,整行会叠成
  // 一条没有间距的文字。样式表没有可消费的 AST,jsdom 也不做布局(量出来的 rect 恒为 0),
  // 文本是唯一诚实的输入;同文件对两个 tsx 的断言仍走 semantic-source 的语义解析。
  "tests/guards/group-layout-guard.test.mjs",
  // 同上,只读 globals.css:独立群组页 groups-page.tsx 上出现的每一个 class 名都必须
  // 在样式表里真的有规则。这条守卫存在的理由就是一个真实缺陷——页面挂了 7 处
  // `.eyebrow`,而 globals.css 里从来没有这条规则,那些小标题以继承来的正文字号裸奔。
  // 样式表没有可消费的 AST,`tsc` 不检查 className 字符串,testing-library 只看文本;
  // 文本是唯一诚实的输入。同文件对 groups-page.tsx 的 className 采集仍走 TS 语义解析。
  "tests/guards/group-page-style-guard.test.mjs",
  // 同上,只读 globals.css:断言只读/群组共享库的顶栏身份行 `.reader-badge-row` 恒不换行、
  // 标题可压缩并省略。它此前用会换行的 `.tag-row`,而 `.workspace-header` 是固定 72px 单行,
  // 于是标题被推到可视区之上——群组共享库点进去整份库名一个像素都看不见。样式表没有可消费的
  // AST,jsdom 又不做布局(量出来的 rect 恒为 0),文本是唯一诚实的输入;组件那侧的结构前提
  // 由 tests/component/notebook-reader-actions.component.test.tsx 真渲染断言。
  "tests/guards/reader-badge-layout-guard.test.mjs",
  // 同上，只读 globals.css：首个真实 workspace side-panel contribution 需要条件
  // 第三列、来源栏收起态与移动端单列。jsdom 不执行 grid 布局，样式表文本是唯一
  // 可验证输入；组件测试另行证明无可见 contribution 时 host 返回 exact null。
  "tests/guards/extension-ui-layout-guard.test.mjs",
  // 构建期装载仓库外 UI 插件的同步脚本：在临时目录里造插件包夹具、跑真同步、
  // 再读回自己刚生成的产物。读的全是测试自己写出来的临时文件，加上后端契约
  // 生成器里那一行排序键声明——没有一处是对生产源码的位置/顺序查询。
  "tests/unit/sync-ui-plugins.test.mjs",
  // 只读 package.json 的生命周期钩子、根 .gitignore 的两条忽略规则，以及 features/ 下
  // 复制进来的插件包的**目录清单**（readdir，判「扁平且只含 TS/manifest/出处标记」）。
  // 生成物按定义不入库，所以「它总在」只能由钩子保证，而这几份都是配置元数据或文件
  // 系统形状、不是生产源码；包内模块的 import 面仍走 semantic-source 的语义解析。
  "tests/guards/extension-plugin-package-guard.test.mjs",
  // node --test 对 `.tsx` 报 Unknown file extension，而 node 泳道真的 import 生产
  // registry。这条守卫遍历模块图证明那条闭包全是 `.ts`，因此必须自己 resolve 并
  // 打开被 import 到的每个文件——它只消费 import 说明符与文件扩展名，不做任何
  // 源码位置/顺序查询。
  "tests/guards/extension-module-graph-guard.test.mjs",
]);
const STRICT_TEXT_READER_ALLOWLIST = new Set([
  // This helper owns production source text and must expose only AST semantics.
  "test-support/semantic-source.mjs",
  "test/semantic-source.mjs",
  // The test-location guard only reads repository layout and runner metadata.
  "tests/guards/test-location-guard.test.mjs",
]);
const SOURCE_INPUT_NAME = (
  /^(?:source|sourceText|productionSource|content|text|payload)$/i
);
const AST_NODE_INPUT_NAME = (
  /^(?:node|sourceFile|declaration|statement|expression)$/i
);
const TEXT_POSITION_METHODS = new Set([
  "at",
  "charAt",
  "charCodeAt",
  "codePointAt",
  "indexOf",
  "slice",
  "split",
  "substring",
]);
const AST_COLLECTIONS = new Set([
  "members",
  "properties",
  "statements",
]);
const AST_POSITION_METHODS = new Set([
  "getEnd",
  "getFullStart",
  "getStart",
]);
const AST_NODE_FACTORIES = new Set([
  "createSourceFile",
  "findFunction",
  "findFunctionIn",
  "parseModule",
  "parseText",
]);


function isSourceModule(relative) {
  return (
    relative.endsWith(".mjs")
    || relative.endsWith(".ts")
    || relative.endsWith(".tsx")
  );
}


function isTestEntrypoint(relative) {
  return (
    relative.endsWith(".test.mjs")
    || relative.endsWith(".test.ts")
    || relative.endsWith(".test.tsx")
  );
}


function scriptKind(relative) {
  if (relative.endsWith(".tsx")) return ts.ScriptKind.TSX;
  if (relative.endsWith(".ts")) return ts.ScriptKind.TS;
  return ts.ScriptKind.JS;
}


function unwrapExpression(expression) {
  let current = expression;
  while (
    ts.isParenthesizedExpression(current)
    || ts.isAsExpression(current)
    || ts.isSatisfiesExpression(current)
    || ts.isTypeAssertionExpression(current)
    || ts.isNonNullExpression(current)
    || ts.isAwaitExpression(current)
  ) {
    current = current.expression;
  }
  return current;
}


function staticStringValue(
  expression,
  resolveBinding = undefined,
  seen = new Set(),
) {
  const current = unwrapExpression(expression);
  if (
    ts.isStringLiteralLike(current)
    || ts.isNoSubstitutionTemplateLiteral(current)
  ) {
    return current.text;
  }
  if (
    ts.isIdentifier(current)
    && resolveBinding
  ) {
    const binding = resolveBinding(current);
    if (!binding || seen.has(binding.declaration)) return undefined;
    return staticStringValue(
      binding.initializer,
      resolveBinding,
      new Set(seen).add(binding.declaration),
    );
  }
  if (
    ts.isBinaryExpression(current)
    && current.operatorToken.kind === ts.SyntaxKind.PlusToken
  ) {
    const left = staticStringValue(current.left, resolveBinding, seen);
    const right = staticStringValue(current.right, resolveBinding, seen);
    return left === undefined || right === undefined
      ? undefined
      : left + right;
  }
  return undefined;
}


function bindingNameContains(bindingName, name) {
  if (ts.isIdentifier(bindingName)) return bindingName.text === name;
  if (ts.isObjectBindingPattern(bindingName)) {
    return bindingName.elements.some(
      (element) => bindingNameContains(element.name, name),
    );
  }
  return bindingName.elements.some(
    (element) => (
      !ts.isOmittedExpression(element)
      && bindingNameContains(element.name, name)
    ),
  );
}


function declarationListBinding(declarationList, name) {
  for (const declaration of declarationList.declarations) {
    if (!bindingNameContains(declaration.name, name)) continue;
    const isStaticConst = (
      declarationList.flags & ts.NodeFlags.Const
      && ts.isIdentifier(declaration.name)
      && declaration.initializer
    );
    return {
      declaration,
      initializer: isStaticConst ? declaration.initializer : undefined,
    };
  }
  return undefined;
}


function importBindsName(statement, name) {
  if (ts.isImportEqualsDeclaration(statement)) {
    return statement.name.text === name;
  }
  if (!ts.isImportDeclaration(statement) || !statement.importClause) {
    return false;
  }
  if (statement.importClause.name?.text === name) return true;
  const bindings = statement.importClause.namedBindings;
  if (bindings && ts.isNamespaceImport(bindings)) {
    return bindings.name.text === name;
  }
  return Boolean(
    bindings
    && ts.isNamedImports(bindings)
    && bindings.elements.some((element) => element.name.text === name),
  );
}


function directLexicalBinding(statements, name) {
  for (const statement of statements) {
    if (ts.isVariableStatement(statement)) {
      const binding = declarationListBinding(
        statement.declarationList,
        name,
      );
      if (binding) return binding;
    }
    if (
      (
        ts.isFunctionDeclaration(statement)
        || ts.isClassDeclaration(statement)
        || ts.isEnumDeclaration(statement)
        || ts.isModuleDeclaration(statement)
      )
      && statement.name?.text === name
    ) {
      return { declaration: statement, initializer: undefined };
    }
    if (importBindsName(statement, name)) {
      return { declaration: statement, initializer: undefined };
    }
  }
  return undefined;
}


function lexicalConstBinding(identifier) {
  const name = identifier.text;
  for (
    let current = identifier.parent;
    current;
    current = current.parent
  ) {
    if (
      ts.isBlock(current)
      || ts.isSourceFile(current)
      || ts.isModuleBlock(current)
    ) {
      const binding = directLexicalBinding(current.statements, name);
      if (binding) {
        return binding.initializer
          ? binding
          : undefined;
      }
    }
    if (ts.isCaseBlock(current)) {
      const binding = directLexicalBinding(
        current.clauses.flatMap((clause) => [...clause.statements]),
        name,
      );
      if (binding) {
        return binding.initializer ? binding : undefined;
      }
    }
    if (
      (
        ts.isForStatement(current)
        || ts.isForInStatement(current)
        || ts.isForOfStatement(current)
      )
      && current.initializer
      && ts.isVariableDeclarationList(current.initializer)
    ) {
      const binding = declarationListBinding(current.initializer, name);
      if (binding) {
        return binding.initializer ? binding : undefined;
      }
    }
    if (
      ts.isFunctionLike(current)
      && current.parameters.some(
        (parameter) => bindingNameContains(parameter.name, name),
      )
    ) {
      return undefined;
    }
    if (
      (ts.isClassDeclaration(current) || ts.isClassExpression(current))
      && current.name?.text === name
    ) {
      return undefined;
    }
    if (
      ts.isModuleDeclaration(current)
      && ts.isIdentifier(current.name)
      && current.name.text === name
    ) {
      return undefined;
    }
    if (
      ts.isFunctionLike(current)
      && current.name
      && ts.isIdentifier(current.name)
      && current.name.text === name
    ) {
      return undefined;
    }
    if (
      ts.isCatchClause(current)
      && current.variableDeclaration
      && bindingNameContains(current.variableDeclaration.name, name)
    ) {
      return undefined;
    }
  }
  return undefined;
}


function staticPropertyName(expression, resolveBinding = undefined) {
  if (ts.isPropertyAccessExpression(expression)) {
    return expression.name.text;
  }
  if (
    ts.isElementAccessExpression(expression)
    && expression.argumentExpression
  ) {
    return staticStringValue(
      expression.argumentExpression,
      resolveBinding,
    );
  }
  return undefined;
}


function staticModuleCallSpecifier(node, resolveBinding = undefined) {
  if (!ts.isCallExpression(node) || node.arguments.length === 0) {
    return undefined;
  }
  const first = staticStringValue(node.arguments[0], resolveBinding);
  if (first === undefined) return undefined;
  const callee = unwrapExpression(node.expression);
  if (callee.kind === ts.SyntaxKind.ImportKeyword) {
    return first;
  }
  if (
    ts.isIdentifier(callee)
    && callee.text === "require"
  ) {
    return first;
  }
  if (
    (
      ts.isPropertyAccessExpression(callee)
      || ts.isElementAccessExpression(callee)
    )
    && staticPropertyName(callee, resolveBinding) === "require"
    && ts.isIdentifier(unwrapExpression(callee.expression))
    && unwrapExpression(callee.expression).text === "module"
  ) {
    return first;
  }
  return undefined;
}


async function sourceModules(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const modules = [];
  for (const entry of entries) {
    const absolute = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      modules.push(...await sourceModules(absolute));
    } else if (isSourceModule(
      path.relative(FRONTEND_DIR, absolute).replaceAll(path.sep, "/"),
    )) {
      modules.push(absolute);
    }
  }
  return modules.sort();
}


function relativeImports(relative, source, moduleNames) {
  const sourceFile = ts.createSourceFile(
    relative,
    source,
    ts.ScriptTarget.Latest,
    true,
    scriptKind(relative),
  );
  const imports = [];
  function resolve(specifier) {
    if (!specifier.startsWith(".")) return undefined;
    const base = path.posix.normalize(
      path.posix.join(path.posix.dirname(relative), specifier),
    );
    for (const candidate of [
      base,
      `${base}.ts`,
      `${base}.tsx`,
      `${base}.mjs`,
      `${base}/index.ts`,
      `${base}/index.tsx`,
      `${base}/index.mjs`,
    ]) {
      if (moduleNames.has(candidate)) return candidate;
    }
    return undefined;
  }
  function visit(node) {
    if (
      (
        ts.isImportDeclaration(node)
        || ts.isExportDeclaration(node)
      )
      && node.moduleSpecifier
      && ts.isStringLiteral(node.moduleSpecifier)
    ) {
      const resolved = resolve(node.moduleSpecifier.text);
      if (resolved) imports.push(resolved);
    }
    if (
      ts.isImportEqualsDeclaration(node)
      && ts.isExternalModuleReference(node.moduleReference)
      && node.moduleReference.expression
      && ts.isStringLiteralLike(node.moduleReference.expression)
    ) {
      const resolved = resolve(node.moduleReference.expression.text);
      if (resolved) imports.push(resolved);
    }
    const callSpecifier = staticModuleCallSpecifier(
      node,
      lexicalConstBinding,
    );
    if (callSpecifier !== undefined) {
      const resolved = resolve(callSpecifier);
      if (resolved) imports.push(resolved);
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return imports;
}


function reachableModules(roots, importsByModule) {
  const reached = new Set();
  const pending = [...roots];
  while (pending.length > 0) {
    const relative = pending.pop();
    if (reached.has(relative)) continue;
    reached.add(relative);
    pending.push(...(importsByModule.get(relative) ?? []));
  }
  return reached;
}


function policyRelativeModules(moduleSources) {
  const moduleNames = new Set(moduleSources.keys());
  const importsByModule = new Map(
    [...moduleSources].map(([relative, source]) => [
      relative,
      relativeImports(relative, source, moduleNames),
    ]),
  );
  const testEntrypoints = [...moduleNames].filter(isTestEntrypoint);
  const productionEntrypoints = [...moduleNames].filter(
    (relative) => /(?:^|\/)(?:page|layout|route)\.tsx?$/.test(relative),
  );
  const testReachable = reachableModules(testEntrypoints, importsByModule);
  const productionReachable = reachableModules(
    productionEntrypoints,
    importsByModule,
  );
  return new Set(
    [...moduleNames].filter((relative) => (
      isTestEntrypoint(relative)
      || relative.startsWith("test-support/")
      || (
        testReachable.has(relative)
        && !productionReachable.has(relative)
      )
    )),
  );
}


async function policyModules() {
  const sources = new Map();
  for (const root of POLICY_ROOTS) {
    for (const absolute of await sourceModules(root)) {
      const relative = path.relative(FRONTEND_DIR, absolute).replaceAll(path.sep, "/");
      sources.set(relative, await readFile(absolute, "utf8"));
    }
  }
  return [...policyRelativeModules(sources)]
    .sort()
    .map((relative) => path.join(FRONTEND_DIR, relative));
}


function isDefinitelyArrayType(type) {
  if (!type) return false;
  if (ts.isArrayTypeNode(type) || ts.isTupleTypeNode(type)) return true;
  if (ts.isParenthesizedTypeNode(type)) {
    return isDefinitelyArrayType(type.type);
  }
  if (
    ts.isTypeOperatorNode(type)
    && type.operator === ts.SyntaxKind.ReadonlyKeyword
  ) {
    return isDefinitelyArrayType(type.type);
  }
  if (ts.isUnionTypeNode(type)) {
    return type.types.every(isDefinitelyArrayType);
  }
  return (
    ts.isTypeReferenceNode(type)
    && ts.isIdentifier(type.typeName)
    && [
      "Array",
      "ReadonlyArray",
      "ArrayBuffer",
      "SharedArrayBuffer",
      "Buffer",
      "Int8Array",
      "Uint8Array",
      "Uint8ClampedArray",
      "Int16Array",
      "Uint16Array",
      "Int32Array",
      "Uint32Array",
      "Float32Array",
      "Float64Array",
      "BigInt64Array",
      "BigUint64Array",
    ].includes(type.typeName.text)
  );
}


function moduleReadsFiles(sourceFile) {
  let readsFiles = false;
  const isFsSpecifier = (value) => (
    /^(?:node:)?fs(?:\/promises)?$/.test(value)
  );
  function visit(node) {
    if (
      (
        ts.isImportDeclaration(node)
        || ts.isExportDeclaration(node)
      )
      && node.moduleSpecifier
      && ts.isStringLiteral(node.moduleSpecifier)
      && isFsSpecifier(node.moduleSpecifier.text)
    ) {
      readsFiles = true;
      return;
    }
    if (
      ts.isImportEqualsDeclaration(node)
      && ts.isExternalModuleReference(node.moduleReference)
      && node.moduleReference.expression
      && ts.isStringLiteralLike(node.moduleReference.expression)
      && isFsSpecifier(node.moduleReference.expression.text)
    ) {
      readsFiles = true;
      return;
    }
    const callSpecifier = staticModuleCallSpecifier(
      node,
      lexicalConstBinding,
    );
    if (callSpecifier !== undefined && isFsSpecifier(callSpecifier)) {
      readsFiles = true;
      return;
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return readsFiles;
}


function hasPositionOrOrderQuery(relative, source) {
  const sourceFile = ts.createSourceFile(
    relative,
    source,
    ts.ScriptTarget.Latest,
    true,
    scriptKind(relative),
  );
  const readsFiles = moduleReadsFiles(sourceFile);
  const registeredReader = (
    readsFiles && STRICT_TEXT_READER_ALLOWLIST.has(relative)
  );
  const typeDeclarations = new Map();
  (function recordTypeDeclarations(node) {
    if (
      (ts.isInterfaceDeclaration(node) || ts.isTypeAliasDeclaration(node))
      && node.name
    ) {
      typeDeclarations.set(node.name.text, node);
    }
    ts.forEachChild(node, recordTypeDeclarations);
  }(sourceFile));
  let found = false;

  function newScope(parent = undefined) {
    return { parent, bindings: new Map() };
  }

  function defaultKind(name) {
    if (SOURCE_INPUT_NAME.test(name)) return "source";
    if (AST_NODE_INPUT_NAME.test(name)) return "ast-node";
    return "unknown";
  }

  function bindingKind(scope, name) {
    for (let current = scope; current; current = current.parent) {
      if (current.bindings.has(name)) {
        return current.bindings.get(name);
      }
    }
    return defaultKind(name);
  }

  function joinedKind(...kinds) {
    for (const kind of [
      "source",
      "ast-position-method",
      "ast-collection",
      "raw-reader",
      "fs-namespace",
      "ast-node",
    ]) {
      if (kinds.includes(kind)) return kind;
    }
    return kinds.length > 0 && kinds.every((kind) => kind === "array")
      ? "array"
      : "unknown";
  }

  function rightmostExpressionName(expression) {
    let current = expression;
    while (ts.isPropertyAccessExpression(current)) current = current.name;
    return ts.isIdentifier(current) ? current.text : undefined;
  }

  function resolvedDeclarationMembers(declaration, seen) {
    if (ts.isTypeAliasDeclaration(declaration)) {
      return resolvedTypeMembers(declaration.type, seen);
    }
    const inherited = (declaration.heritageClauses ?? []).flatMap(
      (clause) => clause.types.flatMap((heritage) => {
        const name = rightmostExpressionName(heritage.expression);
        if (!name || seen.has(name)) return [];
        if (
          ["Partial", "Readonly", "Required"].includes(name)
          && heritage.typeArguments?.length === 1
        ) {
          return [
            ...(resolvedTypeMembers(
              heritage.typeArguments[0],
              seen,
            ) ?? []),
          ];
        }
        const inheritedDeclaration = typeDeclarations.get(name);
        return inheritedDeclaration
          ? [
            ...(resolvedDeclarationMembers(
              inheritedDeclaration,
              new Set(seen).add(name),
            ) ?? []),
          ]
          : [];
      }),
    );
    return [...declaration.members, ...inherited];
  }

  function resolvedTypeMembers(type, seen = new Set()) {
    if (!type) return undefined;
    if (ts.isParenthesizedTypeNode(type)) {
      return resolvedTypeMembers(type.type, seen);
    }
    if (ts.isTypeLiteralNode(type)) return type.members;
    if (
      ts.isTypeReferenceNode(type)
      && ts.isIdentifier(type.typeName)
      && !seen.has(type.typeName.text)
    ) {
      if (
        ["Partial", "Readonly", "Required"].includes(type.typeName.text)
        && type.typeArguments?.length === 1
      ) {
        return resolvedTypeMembers(type.typeArguments[0], seen);
      }
      const declaration = typeDeclarations.get(type.typeName.text);
      if (!declaration) return undefined;
      const nextSeen = new Set(seen).add(type.typeName.text);
      return resolvedDeclarationMembers(declaration, nextSeen);
    }
    if (ts.isIntersectionTypeNode(type)) {
      return type.types.flatMap(
        (part) => [...(resolvedTypeMembers(part, seen) ?? [])],
      );
    }
    return undefined;
  }

  function objectPropertyType(type, property) {
    for (const member of resolvedTypeMembers(type) ?? []) {
      if (
        ts.isPropertySignature(member)
        && member.name
        && (
          (
            ts.isIdentifier(member.name)
            || ts.isStringLiteralLike(member.name)
          )
          && member.name.text === property
        )
      ) {
        return member.type;
      }
    }
    return undefined;
  }

  function definitelyArrayType(type, seen = new Set()) {
    if (isDefinitelyArrayType(type)) return true;
    if (
      type
      && ts.isTypeReferenceNode(type)
      && ts.isIdentifier(type.typeName)
      && ["Partial", "Readonly", "Required"].includes(type.typeName.text)
      && type.typeArguments?.length === 1
    ) {
      return definitelyArrayType(type.typeArguments[0], seen);
    }
    if (
      type
      && ts.isTypeReferenceNode(type)
      && ts.isIdentifier(type.typeName)
      && !seen.has(type.typeName.text)
    ) {
      const declaration = typeDeclarations.get(type.typeName.text);
      return Boolean(
        declaration
        && ts.isTypeAliasDeclaration(declaration)
        && definitelyArrayType(
          declaration.type,
          new Set(seen).add(type.typeName.text),
        )
      );
    }
    return false;
  }

  function rightmostTypeName(typeName) {
    let current = typeName;
    while (ts.isQualifiedName(current)) current = current.right;
    return ts.isIdentifier(current) ? current.text : undefined;
  }

  const astTypeNames = new Set([
    "Declaration",
    "Expression",
    "Node",
    "SourceFile",
    "Statement",
  ]);

  function interfaceExtendsAst(declaration, seen) {
    return (declaration.heritageClauses ?? []).some(
      (clause) => clause.types.some((heritage) => {
        const name = rightmostExpressionName(heritage.expression);
        if (!name) return false;
        if (astTypeNames.has(name)) return true;
        if (
          ["Partial", "Readonly", "Required"].includes(name)
          && heritage.typeArguments?.length === 1
        ) {
          return definitelyAstNodeType(
            heritage.typeArguments[0],
            seen,
          );
        }
        if (seen.has(name)) return false;
        const inherited = typeDeclarations.get(name);
        return Boolean(
          inherited
          && (
            ts.isInterfaceDeclaration(inherited)
              ? interfaceExtendsAst(
                inherited,
                new Set(seen).add(name),
              )
              : definitelyAstNodeType(
                inherited.type,
                new Set(seen).add(name),
              )
          )
        );
      }),
    );
  }

  function definitelyAstNodeType(type, seen = new Set()) {
    if (!type) return false;
    if (ts.isParenthesizedTypeNode(type)) {
      return definitelyAstNodeType(type.type, seen);
    }
    if (ts.isUnionTypeNode(type)) {
      return type.types.every(
        (part) => definitelyAstNodeType(part, seen),
      );
    }
    if (ts.isIntersectionTypeNode(type)) {
      return type.types.some(
        (part) => definitelyAstNodeType(part, seen),
      );
    }
    if (!ts.isTypeReferenceNode(type)) return false;
    const name = rightmostTypeName(type.typeName);
    if (
      ["Partial", "Readonly", "Required"].includes(name)
      && type.typeArguments?.length === 1
    ) {
      return definitelyAstNodeType(type.typeArguments[0], seen);
    }
    if (astTypeNames.has(name)) {
      return true;
    }
    if (
      ts.isIdentifier(type.typeName)
      && !seen.has(type.typeName.text)
    ) {
      const declaration = typeDeclarations.get(type.typeName.text);
      return Boolean(
        declaration
        && (
          ts.isInterfaceDeclaration(declaration)
            ? interfaceExtendsAst(
              declaration,
              new Set(seen).add(type.typeName.text),
            )
            : definitelyAstNodeType(
              declaration.type,
              new Set(seen).add(type.typeName.text),
            )
        )
      );
    }
    return false;
  }

  function isRawReaderExpression(expression, scope) {
    const current = unwrapExpression(expression);
    if (ts.isIdentifier(current)) {
      return bindingKind(scope, current.text) === "raw-reader";
    }
    if (
      (
        ts.isPropertyAccessExpression(current)
        || ts.isElementAccessExpression(current)
      )
      && ["readFile", "readFileSync"].includes(
        staticPropertyName(current, lexicalConstBinding),
      )
    ) {
      return true;
    }
    return false;
  }

  function isRawReadCall(expression, scope) {
    const current = unwrapExpression(expression);
    return Boolean(
      registeredReader
      && ts.isCallExpression(current)
      && isRawReaderExpression(current.expression, scope)
    );
  }

  function expressionKind(expression, scope) {
    if (!expression) return "unknown";
    const current = unwrapExpression(expression);
    if (ts.isIdentifier(current)) {
      return bindingKind(scope, current.text);
    }
    const moduleSpecifier = staticModuleCallSpecifier(
      current,
      lexicalConstBinding,
    );
    if (
      moduleSpecifier !== undefined
      && /^(?:node:)?fs(?:\/promises)?$/.test(moduleSpecifier)
    ) {
      return "fs-namespace";
    }
    if (ts.isArrayLiteralExpression(current)) {
      const spreadKinds = current.elements
        .filter(ts.isSpreadElement)
        .map((element) => expressionKind(element.expression, scope));
      return spreadKinds.length > 0
        ? joinedKind(...spreadKinds, "array")
        : "array";
    }
    if (isRawReadCall(current, scope)) return "source";
    if (ts.isConditionalExpression(current)) {
      return joinedKind(
        expressionKind(current.whenTrue, scope),
        expressionKind(current.whenFalse, scope),
      );
    }
    if (ts.isCommaListExpression(current)) {
      return expressionKind(current.elements.at(-1), scope);
    }
    if (
      ts.isTemplateExpression(current)
      && current.templateSpans.some(
        (span) => expressionKind(span.expression, scope) === "source",
      )
    ) {
      return "source";
    }
    if (
      ts.isTaggedTemplateExpression(current)
      && ts.isTemplateExpression(current.template)
      && current.template.templateSpans.some(
        (span) => expressionKind(span.expression, scope) === "source",
      )
    ) {
      return "source";
    }
    if (
      ts.isBinaryExpression(current)
      && current.operatorToken.kind === ts.SyntaxKind.CommaToken
    ) {
      return expressionKind(current.right, scope);
    }
    if (
      ts.isBinaryExpression(current)
      && [
        ts.SyntaxKind.PlusToken,
        ts.SyntaxKind.QuestionQuestionToken,
        ts.SyntaxKind.BarBarToken,
        ts.SyntaxKind.AmpersandAmpersandToken,
      ].includes(current.operatorToken.kind)
    ) {
      return joinedKind(
        expressionKind(current.left, scope),
        expressionKind(current.right, scope),
      );
    }
    if (
      ts.isCallExpression(current)
      && ts.isIdentifier(current.expression)
      && current.expression.text === "String"
      && current.arguments.some(
        (argument) => expressionKind(argument, scope) === "source",
      )
    ) {
      return "source";
    }
    if (
      ts.isNewExpression(current)
      && ts.isIdentifier(current.expression)
      && current.expression.text === "String"
      && (current.arguments ?? []).some(
        (argument) => expressionKind(argument, scope) === "source",
      )
    ) {
      return "source";
    }
    if (
      ts.isCallExpression(current)
      && (
        ts.isPropertyAccessExpression(current.expression)
        || ts.isElementAccessExpression(current.expression)
      )
      && staticPropertyName(
        current.expression,
        lexicalConstBinding,
      ) === "concat"
      && (
        expressionKind(current.expression.expression, scope) === "source"
        || current.arguments.some(
          (argument) => expressionKind(argument, scope) === "source",
        )
      )
    ) {
      return "source";
    }
    if (
      ts.isCallExpression(current)
      && (
        ts.isPropertyAccessExpression(current.expression)
        || ts.isElementAccessExpression(current.expression)
      )
      && staticPropertyName(
        current.expression,
        lexicalConstBinding,
      ) === "from"
      && ts.isIdentifier(unwrapExpression(current.expression.expression))
      && unwrapExpression(current.expression.expression).text === "Array"
      && current.arguments.length > 0
    ) {
      const inputKind = expressionKind(current.arguments[0], scope);
      return ["source", "ast-collection"].includes(inputKind)
        ? inputKind
        : "array";
    }
    if (
      ts.isCallExpression(current)
      && AST_NODE_FACTORIES.has(
        staticPropertyName(current.expression, lexicalConstBinding)
        ?? (
          ts.isIdentifier(current.expression)
            ? current.expression.text
            : ""
        ),
      )
    ) {
      return "ast-node";
    }
    if (
      registeredReader
      && (
        ts.isCallExpression(current)
        || ts.isNewExpression(current)
      )
      && (current.arguments ?? []).some(
        (argument) => expressionKind(argument, scope) === "source",
      )
    ) {
      return "source";
    }
    if (
      (
        ts.isPropertyAccessExpression(current)
        || ts.isElementAccessExpression(current)
      )
      && AST_COLLECTIONS.has(
        staticPropertyName(current, lexicalConstBinding),
      )
      && expressionKind(current.expression, scope) === "ast-node"
    ) {
      return "ast-collection";
    }
    if (
      (
        ts.isPropertyAccessExpression(current)
        || ts.isElementAccessExpression(current)
      )
      && AST_POSITION_METHODS.has(
        staticPropertyName(current, lexicalConstBinding),
      )
      && expressionKind(current.expression, scope) === "ast-node"
    ) {
      return "ast-position-method";
    }
    if (
      (
        ts.isPropertyAccessExpression(current)
        || ts.isElementAccessExpression(current)
      )
      && ["readFile", "readFileSync"].includes(
        staticPropertyName(current, lexicalConstBinding),
      )
      && expressionKind(current.expression, scope) === "fs-namespace"
    ) {
      return "raw-reader";
    }
    if (
      ts.isCallExpression(current)
      && (
        ts.isPropertyAccessExpression(current.expression)
        || ts.isElementAccessExpression(current.expression)
      )
    ) {
      return expressionKind(current.expression.expression, scope);
    }
    return "unknown";
  }

  function propertyNameText(name) {
    if (!name) return undefined;
    if (
      ts.isIdentifier(name)
      || ts.isStringLiteralLike(name)
      || ts.isNumericLiteral(name)
    ) {
      return name.text;
    }
    if (ts.isComputedPropertyName(name)) {
      return staticStringValue(name.expression, lexicalConstBinding);
    }
    return undefined;
  }

  function declarePattern(
    name,
    type,
    initializer,
    scope,
    forcedKind = undefined,
  ) {
    if (ts.isIdentifier(name)) {
      const initializerKind = expressionKind(initializer, scope);
      let kind = forcedKind;
      if (kind === undefined) {
        kind = definitelyArrayType(type) || initializerKind === "array"
          ? "array"
          : definitelyAstNodeType(type)
            ? "ast-node"
          : initializerKind !== "unknown"
            ? initializerKind
            : defaultKind(name.text);
      }
      scope.bindings.set(name.text, kind);
      return;
    }
    if (ts.isObjectBindingPattern(name)) {
      const initializerKind = forcedKind
        ?? expressionKind(initializer, scope);
      for (const element of name.elements) {
        if (element.dotDotDotToken) {
          declarePattern(
            element.name,
            undefined,
            undefined,
            scope,
            forcedKind,
          );
          continue;
        }
        const property = element.propertyName
          ? propertyNameText(element.propertyName)
          : ts.isIdentifier(element.name)
            ? element.name.text
            : undefined;
        const propertyType = objectPropertyType(type, property);
        let propertyKind = forcedKind;
        if (propertyKind === undefined && definitelyArrayType(propertyType)) {
          propertyKind = "array";
        } else if (
          propertyKind === undefined
          && initializerKind === "ast-node"
          && AST_COLLECTIONS.has(property)
        ) {
          propertyKind = "ast-collection";
        } else if (
          propertyKind === undefined
          && initializerKind === "ast-node"
          && AST_POSITION_METHODS.has(property)
        ) {
          propertyKind = "ast-position-method";
        } else if (
          propertyKind === undefined
          && initializerKind === "fs-namespace"
          && ["readFile", "readFileSync"].includes(property)
        ) {
          propertyKind = "raw-reader";
        }
        declarePattern(
          element.name,
          propertyType,
          undefined,
          scope,
          propertyKind,
        );
      }
      return;
    }
    for (const element of name.elements) {
      if (!ts.isOmittedExpression(element)) {
        declarePattern(
          element.name,
          undefined,
          undefined,
          scope,
          forcedKind,
        );
      }
    }
  }

  function objectBindingProperty(element) {
    if (element.propertyName) {
      return propertyNameText(element.propertyName);
    }
    return ts.isIdentifier(element.name) ? element.name.text : undefined;
  }

  function patternConsumesOrder(name, initializer, scope) {
    const initializerKind = expressionKind(initializer, scope);
    if (
      ts.isArrayBindingPattern(name)
      && ["source", "ast-collection"].includes(initializerKind)
    ) {
      return true;
    }
    return (
      ts.isObjectBindingPattern(name)
      && initializerKind === "ast-node"
      && name.elements.some(
        (element) => ["pos", "end"].includes(
          objectBindingProperty(element),
        ),
      )
    );
  }

  function predeclareImports(statements, scope) {
    for (const statement of statements) {
      if (
        ts.isImportDeclaration(statement)
        && statement.importClause
        && statement.moduleSpecifier
        && ts.isStringLiteralLike(statement.moduleSpecifier)
        && /^(?:node:)?fs(?:\/promises)?$/.test(
          statement.moduleSpecifier.text,
        )
      ) {
        if (statement.importClause.name) {
          scope.bindings.set(
            statement.importClause.name.text,
            "fs-namespace",
          );
        }
        const bindings = statement.importClause.namedBindings;
        if (bindings && ts.isNamespaceImport(bindings)) {
          scope.bindings.set(bindings.name.text, "fs-namespace");
        } else if (bindings && ts.isNamedImports(bindings)) {
          for (const element of bindings.elements) {
            const imported = (element.propertyName ?? element.name).text;
            scope.bindings.set(
              element.name.text,
              ["readFile", "readFileSync"].includes(imported)
                ? "raw-reader"
                : "unknown",
            );
          }
        }
      }
      if (
        ts.isImportEqualsDeclaration(statement)
        && ts.isExternalModuleReference(statement.moduleReference)
        && statement.moduleReference.expression
        && ts.isStringLiteralLike(statement.moduleReference.expression)
        && /^(?:node:)?fs(?:\/promises)?$/.test(
          statement.moduleReference.expression.text,
        )
      ) {
        scope.bindings.set(statement.name.text, "fs-namespace");
      }
    }
  }

  function predeclareStatements(statements, scope) {
    for (const statement of statements) {
      if (!ts.isVariableStatement(statement)) continue;
      for (const declaration of statement.declarationList.declarations) {
        declarePattern(
          declaration.name,
          declaration.type,
          declaration.initializer,
          scope,
        );
      }
    }
  }

  function textReceiverKind(expression, scope) {
    return expressionKind(expression, scope);
  }

  function assignmentIsConditional(node, logical = false) {
    return logical || Boolean(ts.findAncestor(
      node,
      (ancestor) => (
        ts.isIfStatement(ancestor)
        || ts.isConditionalExpression(ancestor)
        || ts.isSwitchStatement(ancestor)
        || ts.isIterationStatement(ancestor, false)
        || ts.isTryStatement(ancestor)
        || (
          ts.isBinaryExpression(ancestor)
          && [
            ts.SyntaxKind.AmpersandAmpersandToken,
            ts.SyntaxKind.BarBarToken,
            ts.SyntaxKind.QuestionQuestionToken,
          ].includes(ancestor.operatorToken.kind)
        )
      ),
    ));
  }

  function assignBinding(scope, name, kind, conditional = false) {
    if (kind === "unknown") return;
    for (let current = scope; current; current = current.parent) {
      if (current.bindings.has(name)) {
        current.bindings.set(
          name,
          conditional
            ? joinedKind(current.bindings.get(name), kind)
            : kind,
        );
        return;
      }
    }
    scope.bindings.set(name, kind);
  }

  function objectAssignmentTarget(property) {
    if (ts.isShorthandPropertyAssignment(property)) {
      return property.name.text;
    }
    if (ts.isPropertyAssignment(property)) {
      const target = unwrapExpression(property.initializer);
      return ts.isIdentifier(target) ? target.text : undefined;
    }
    return undefined;
  }

  function applyObjectAssignment(node, scope, conditional) {
    const left = unwrapExpression(node.left);
    if (
      !ts.isObjectLiteralExpression(left)
      || expressionKind(node.right, scope) !== "ast-node"
    ) {
      return false;
    }
    for (const property of left.properties) {
      if (
        !ts.isShorthandPropertyAssignment(property)
        && !ts.isPropertyAssignment(property)
      ) {
        continue;
      }
      const propertyName = propertyNameText(property.name);
      if (["pos", "end"].includes(propertyName)) return true;
      const target = objectAssignmentTarget(property);
      if (!target) continue;
      if (AST_COLLECTIONS.has(propertyName)) {
        assignBinding(scope, target, "ast-collection", conditional);
      } else if (AST_POSITION_METHODS.has(propertyName)) {
        assignBinding(scope, target, "ast-position-method", conditional);
      }
    }
    return false;
  }

  function visit(node, scope) {
    if (found) return;
    if (ts.isSourceFile(node)) {
      predeclareImports(node.statements, scope);
      predeclareStatements(node.statements, scope);
      for (const statement of node.statements) visit(statement, scope);
      return;
    }
    if (ts.isFunctionLike(node)) {
      const functionScope = newScope(scope);
      for (const parameter of node.parameters) {
        if (parameter.initializer) {
          visit(parameter.initializer, functionScope);
          if (found) return;
          if (patternConsumesOrder(
            parameter.name,
            parameter.initializer,
            functionScope,
          )) {
            found = true;
            return;
          }
        }
        declarePattern(
          parameter.name,
          parameter.type,
          parameter.initializer,
          functionScope,
        );
      }
      if (node.body) visit(node.body, functionScope);
      return;
    }
    if (ts.isCatchClause(node)) {
      const catchScope = newScope(scope);
      if (node.variableDeclaration) {
        declarePattern(
          node.variableDeclaration.name,
          node.variableDeclaration.type,
          node.variableDeclaration.initializer,
          catchScope,
          "unknown",
        );
      }
      visit(node.block, catchScope);
      return;
    }
    if (ts.isBlock(node)) {
      const blockScope = newScope(scope);
      predeclareStatements(node.statements, blockScope);
      for (const statement of node.statements) visit(statement, blockScope);
      return;
    }
    if (ts.isVariableDeclaration(node)) {
      declarePattern(node.name, node.type, node.initializer, scope);
      if (patternConsumesOrder(node.name, node.initializer, scope)) {
        found = true;
        return;
      }
    }
    if (ts.isBinaryExpression(node)) {
      const logicalAssignment = [
        ts.SyntaxKind.AmpersandAmpersandEqualsToken,
        ts.SyntaxKind.BarBarEqualsToken,
        ts.SyntaxKind.QuestionQuestionEqualsToken,
      ].includes(node.operatorToken.kind);
      const simpleAssignment = (
        node.operatorToken.kind === ts.SyntaxKind.EqualsToken
      );
      const rightKind = expressionKind(node.right, scope);
      const conditional = assignmentIsConditional(
        node,
        logicalAssignment,
      );
      if (
        (simpleAssignment || logicalAssignment)
        && ts.isIdentifier(node.left)
      ) {
        assignBinding(scope, node.left.text, rightKind, conditional);
      }
      if (
        simpleAssignment
        && applyObjectAssignment(node, scope, conditional)
      ) {
        found = true;
        return;
      }
    }
    if (
      (
        ts.isPropertyAccessExpression(node)
        || ts.isElementAccessExpression(node)
      )
      && ["pos", "end"].includes(
        staticPropertyName(node, lexicalConstBinding),
      )
      && expressionKind(node.expression, scope) === "ast-node"
    ) {
      found = true;
      return;
    }
    if (
      (
        ts.isPropertyAccessExpression(node)
        || ts.isElementAccessExpression(node)
      )
      && staticPropertyName(node, lexicalConstBinding) === "length"
      && ["source", "ast-collection"].includes(
        textReceiverKind(node.expression, scope),
      )
    ) {
      found = true;
      return;
    }
    if (
      ts.isElementAccessExpression(node)
      && textReceiverKind(node.expression, scope) === "source"
    ) {
      found = true;
      return;
    }
    if (
      ts.isElementAccessExpression(node)
      && expressionKind(node.expression, scope) === "ast-collection"
    ) {
      found = true;
      return;
    }
    if (
      ts.isVariableDeclaration(node)
      && ts.isArrayBindingPattern(node.name)
      && node.initializer
      && expressionKind(node.initializer, scope) === "ast-collection"
    ) {
      found = true;
      return;
    }
    if (
      ts.isBinaryExpression(node)
      && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
      && ts.isArrayLiteralExpression(node.left)
      && [
        "ast-collection",
        "source",
      ].includes(expressionKind(node.right, scope))
    ) {
      found = true;
      return;
    }
    if (
      ts.isCallExpression(node)
      && (
        ts.isPropertyAccessExpression(node.expression)
        || ts.isElementAccessExpression(node.expression)
      )
    ) {
      const method = staticPropertyName(
        node.expression,
        lexicalConstBinding,
      );
      const receiver = node.expression.expression;
      if (
        AST_POSITION_METHODS.has(method)
        && expressionKind(receiver, scope) === "ast-node"
      ) {
        found = true;
        return;
      }
      if (
        method === "call"
        && expressionKind(receiver, scope) === "ast-position-method"
      ) {
        found = true;
        return;
      }
      if (
        method === "at"
        && expressionKind(receiver, scope) === "ast-collection"
      ) {
        found = true;
        return;
      }
      if (
        TEXT_POSITION_METHODS.has(method)
        && (
          ["source", "ast-collection"].includes(
            textReceiverKind(receiver, scope),
          )
          || (
            registeredReader
            && textReceiverKind(receiver, scope) !== "array"
          )
        )
      ) {
        found = true;
        return;
      }
    }
    if (
      ts.isCallExpression(node)
      && expressionKind(node.expression, scope) === "ast-position-method"
    ) {
      found = true;
      return;
    }
    ts.forEachChild(node, (child) => visit(child, scope));
  }
  visit(sourceFile, newScope());
  return found;
}


function modulePolicyOffenders(relative, source) {
  const sourceFile = ts.createSourceFile(
    relative,
    source,
    ts.ScriptTarget.Latest,
    true,
    scriptKind(relative),
  );
  const offenders = [];
  const readsFiles = moduleReadsFiles(sourceFile);
  if (readsFiles && !DIRECT_READ_ALLOWLIST.has(relative)) {
    offenders.push(`${relative}: direct production-source read`);
  }
  if (hasPositionOrOrderQuery(relative, source)) {
    offenders.push(`${relative}: source-position or source-order query`);
  }
  if (/(?:frontend|backend|scripts)\/[^:'"\n]+:\d+\b/.test(source)) {
    offenders.push(`${relative}: path-line identity`);
  }
  if (
    /\b(?:test|it|describe)\.(?:skip|only|todo)\s*\(/.test(source)
    || /\bskip\s*:\s*(?:true|["'])/.test(source)
  ) {
    offenders.push(`${relative}: disabled or exclusive test`);
  }
  return offenders;
}


async function policyOffenders() {
  const offenders = [];
  for (const absolute of await policyModules()) {
    const relative = path.relative(FRONTEND_DIR, absolute).replaceAll(path.sep, "/");
    const source = await readFile(absolute, "utf8");
    offenders.push(...modulePolicyOffenders(relative, source));
  }
  return offenders.sort();
}


test("static source scans are registered with a category and reason", () => {
  for (const [name, contract] of Object.entries(STATIC_SOURCE_CONTRACTS)) {
    assert.ok(contract.category, name);
    assert.ok(contract.reason, name);
    assert.ok(contract.roots.length > 0, name);
  }
});


test("source policy rejects layout identities with bounded syntax rules", () => {
  for (const mutation of [
    "const start = node.getStart();",
    "const start = node.getFullStart();",
    "const finish = node.end;",
    "const first = sourceFile.statements[0];",
    "const first = sourceFile.members.at(0);",
    "const [first] = sourceFile.statements;",
    "[first] = sourceFile.properties;",
    "const firstLine = source.split('\\n')[0];",
    "const firstCharacter = source[0];",
    "const sourceSize = source.length;",
    "node['pos'];",
    "node['getStart']();",
    "sourceFile['statements'][0];",
    "const items = sourceFile.statements; items[0];",
    "(source).slice(1);",
    "(source as string).slice(1);",
    "(source satisfies string).slice(1);",
    "source.trim().split('\\n');",
    "source.at(0);",
    "const [first] = source;",
    "const body = source; body.slice(1);",
    "let body; body = source; body.slice(1);",
    "String(source).slice(1);",
    "`${source}`.slice(1);",
    "(source + '').slice(1);",
    "function f(x = source.slice(1)) {}",
    "function g([first] = source) {}",
    "(flag ? source : '').slice(1);",
    "(source ?? '').slice(1);",
    "(0, source).slice(1);",
    "new String(source).slice(1);",
    "''.concat(source).slice(1);",
    "String.raw`${source}`.slice(1);",
    "let body = source; if (flag) body = []; body.slice(1);",
    "[...sourceFile.statements][0];",
    "Array.from(sourceFile.statements)[0];",
    "const { statements: items } = sourceFile; items[0];",
    "const start = node.getStart; start.call(node);",
    "const { getStart } = node; getStart();",
    "node['p' + 'os'];",
    "const { pos } = node; void pos;",
    "sourceFile.statements.length;",
    "sourceFile.statements.slice(1);",
    "({ pos } = node);",
    "({ statements: items } = sourceFile); items[0];",
    "function inspect(tree: ts.SourceFile) { return tree.statements[0]; }",
    "const tree = ts.createSourceFile('a.ts', code); tree.statements[0];",
    "const tree = parseText(code, 'a.ts'); tree.statements[0];",
    "findFunction(tree, 'save').getStart();",
    "({ getStart: start } = node); start();",
    "let body = source; try { body = []; } catch {} body.slice(1);",
    "let body = []; body ||= source; body.slice(1);",
    "let body = source; body &&= []; body.slice(1);",
    "let body = source; flag && (body = []); body.slice(1);",
    "let body = source; flag || (body = []); body.slice(1);",
    "let body = source; flag ?? (body = []); body.slice(1);",
    "const key = 'pos'; ({ [key]: p } = node);",
    "const method = 'getStart'; node[method]();",
    "function inspect(tree: Readonly<ts.SourceFile>) {"
      + " return tree.statements[0]; }",
    "type Tree = ts.SourceFile;"
      + " function inspect(tree: Tree) { return tree.statements[0]; }",
    "interface Tree extends ts.SourceFile {}"
      + " function inspect(tree: Tree) { return tree.statements[0]; }",
    "type Tree = ts.SourceFile & { tag: string };"
      + " function inspect(tree: Tree) { return tree.statements[0]; }",
  ]) {
    assert.deepEqual(
      modulePolicyOffenders("example.test.mjs", mutation),
      ["example.test.mjs: source-position or source-order query"],
      mutation,
    );
  }

  assert.deepEqual(
    modulePolicyOffenders(
      "example.test.mjs",
      "import { readFile } from 'node:fs/promises';\n"
        + "const source = await readFile(path, 'utf8');\n"
        + "source.slice(source.indexOf('a'));",
    ),
    [
      "example.test.mjs: direct production-source read",
      "example.test.mjs: source-position or source-order query",
    ],
  );
  assert.deepEqual(
    modulePolicyOffenders(
      "example.test.ts",
      "function bad(source: string) { return source.slice(1); }\n"
        + "function good(source: string[]) { return source.slice(); }",
    ),
    ["example.test.ts: source-position or source-order query"],
  );
  for (const source of [
    "import fs from 'fs'; void fs;",
    "const fs = require('node:fs'); void fs;",
    "const fs = await import('fs/promises'); void fs;",
    "const fs = await import('node:fs', {}); void fs;",
    "const fs = require('fs', undefined); void fs;",
    "const fs = module.require('node:fs'); void fs;",
    "const fs = (module).require('node:fs'); void fs;",
    "const fs = import('node:' + 'fs'); void fs;",
    "const fs = require('f' + 's'); void fs;",
    "const specifier = 'f' + 's'; const fs = import(specifier); void fs;",
    "const specifier = 'node:fs'; import(specifier);"
      + " function shadow() { const specifier = './helper'; return specifier; }",
    "for (const specifier = 'node:fs'; flag;) {"
      + " import(specifier); break; }",
    "const specifier = ('node:fs' satisfies string); import(specifier);",
    "switch (flag) { case 1:"
      + " const specifier = 'node:fs'; import(specifier); break; }",
    "namespace N {"
      + " const specifier = 'node:fs'; import(specifier); }",
    "import fs = require('node:fs'); void fs;",
  ]) {
    assert.deepEqual(
      modulePolicyOffenders("example.test.mjs", source),
      ["example.test.mjs: direct production-source read"],
      source,
    );
  }
  assert.deepEqual(
    modulePolicyOffenders(
      "test/semantic-source.mjs",
      "import { readFile } from 'node:fs/promises';\n"
        + "const body = await readFile(path, 'utf8');\nbody.slice();",
    ),
    ["test/semantic-source.mjs: source-position or source-order query"],
  );
  assert.deepEqual(
    modulePolicyOffenders(
      "test/source-helper.ts",
      "export function fragment(text: string) { return text.slice(1); }",
    ),
    ["test/source-helper.ts: source-position or source-order query"],
  );
  assert.deepEqual(
    modulePolicyOffenders(
      "test/static-source-policy.test.mjs",
      "const source = 'production'; source.slice(1);",
    ),
    ["test/static-source-policy.test.mjs: source-position or source-order query"],
  );
  for (const operation of [
    "const body = await readFile(url, 'utf8'); body[0];",
    "const body = await readFile(url, 'utf8'); body.length;",
    "const body = await load(url, 'utf8'); body[0];",
    "const load = readFile; const body = await load(url, 'utf8'); body.length;",
    "const body = decode(await readFile(url, 'utf8')); body.slice(1);",
    "const body = decode(await readFile(url, 'utf8')); body[0];",
    "const body = decode(await readFile(url, 'utf8')); body.length;",
    "const fs = await import('node:fs/promises');"
      + " const { readFile: load } = fs;"
      + " const body = await load(url, 'utf8'); body[0];",
  ]) {
    const importLine = operation.includes("load(url")
      && !operation.startsWith("const load")
      ? "import { readFile as load } from 'node:fs/promises';\n"
      : "import { readFile } from 'node:fs/promises';\n";
    assert.deepEqual(
      modulePolicyOffenders(
        "test/semantic-source.mjs",
        importLine + operation,
      ),
      ["test/semantic-source.mjs: source-position or source-order query"],
      operation,
    );
  }
});


test("source policy leaves ordinary array operations alone", () => {
  for (const source of [
    "const values = [1]; values.slice().sort();",
    "function clone(values: string[]) { return values.slice(); }",
    "function clone(payload: readonly string[]) { return payload.slice(); }",
    "function clone(payload: Uint8Array) { return payload.slice(); }",
    "function trimPrefix(value: string) { return value.slice(1); }",
    "function good({ source }: { source: string[] }) {"
      + " return source.slice(); }",
    "try {} catch (source) { source.slice(1); }",
    "const response = { end() {} }; response.end();",
    "const model = { properties: [1] }; model.properties[0];",
    "interface P { source: string[] }"
      + " function good({ source }: P) { return source.slice(); }",
    "interface Base { source: string[] } interface Child extends Base {}"
      + " function good({ source }: Child) { return source.slice(); }",
    "interface Base { source: string[] }"
      + " interface Child extends Readonly<Base> {}"
      + " function good({ source }: Child) { return source.slice(); }",
    "let specifier = 'node:fs'; specifier = './helper'; import(specifier);",
    "const specifier = 'node:fs';"
      + " function safe({ specifier }) { import(specifier); }",
    "const specifier = 'node:fs';"
      + " function safe(options) {"
      + " const { specifier } = options; import(specifier); }",
    "const specifier = 'node:fs';"
      + " function safe() { function specifier() {} import(specifier); }",
    "const specifier = 'node:fs';"
      + " const C = class specifier {"
      + " static load() { import(specifier); } };",
    "function good({ source }: Readonly<{ source: string[] }>) {"
      + " return source.slice(); }",
  ]) {
    assert.deepEqual(
      modulePolicyOffenders("example.test.ts", source),
      [],
      source,
    );
  }
  assert.deepEqual(
    modulePolicyOffenders(
      "test/semantic-source.mjs",
      "import { readFile } from 'node:fs/promises';\n"
        + "const items = getItems(); const copy = Array.from(items);"
        + " copy.slice(); copy[0]; copy.length;",
    ),
    [],
  );
});


test("source policy stays bounded for repeated callable syntax", () => {
  const repeatedCalls = Array.from(
    { length: 10_000 },
    () => "f();",
  ).join("\n");
  const source = [
    "const a = () => [];",
    "const b = () => [];",
    "let f = flag ? a : b;",
    repeatedCalls,
  ].join("\n");
  assert.deepEqual(modulePolicyOffenders("example.test.mjs", source), []);
});


test("source policy follows test imports to root TypeScript helper modules", () => {
  const modules = new Map([
    [
      "feature.test.mjs",
      "import { fragment } from './feature-helper.ts'; void fragment;",
    ],
    [
      "feature-helper.ts",
      "export function fragment(text: string) { return text.slice(1); }",
    ],
    ["page.tsx", "export default function Page() { return null; }"],
  ]);
  assert.deepEqual(
    [...policyRelativeModules(modules)].sort(),
    ["feature-helper.ts", "feature.test.mjs"],
  );
  assert.deepEqual(
    modulePolicyOffenders(
      "feature-helper.ts",
      modules.get("feature-helper.ts"),
    ),
    ["feature-helper.ts: source-position or source-order query"],
  );
});


test("source policy follows dynamic imports to test-only helpers", () => {
  const modules = new Map([
    [
      "feature.test.mjs",
      "const helper = await import('./dynamic-helper.ts'); void helper;",
    ],
    [
      "dynamic-helper.ts",
      "export function fragment(payload: string) { return payload.slice(1); }",
    ],
    ["page.tsx", "export default function Page() { return null; }"],
  ]);
  assert.deepEqual(
    [...policyRelativeModules(modules)].sort(),
    ["dynamic-helper.ts", "feature.test.mjs"],
  );

  modules.set(
    "feature.test.mjs",
    "const helper = await import(`./dynamic-helper.ts`); void helper;",
  );
  assert.deepEqual(
    [...policyRelativeModules(modules)].sort(),
    ["dynamic-helper.ts", "feature.test.mjs"],
  );

  for (const entrypoint of [
    "const helper = await import('./dynamic-helper.ts', {}); void helper;",
    "const helper = require('./dynamic-helper.ts'); void helper;",
    "const helper = module.require('./dynamic-helper.ts'); void helper;",
    "const helper = (module).require('./dynamic-helper.ts'); void helper;",
    "const helper = import('./dynamic-' + 'helper.ts'); void helper;",
    "const specifier = ('./dynamic-helper.ts' satisfies string);"
      + " import(specifier);",
    "const specifier = './dynamic-helper.ts';"
      + " const helper = import(specifier); void helper;",
  ]) {
    modules.set("feature.test.mjs", entrypoint);
    assert.deepEqual(
      [...policyRelativeModules(modules)].sort(),
      ["dynamic-helper.ts", "feature.test.mjs"],
      entrypoint,
    );
  }

  const typescriptModules = new Map([
    [
      "feature.test.ts",
      "import helper = require('./dynamic-helper.ts'); void helper;",
    ],
    ["dynamic-helper.ts", "export const helper = true;"],
  ]);
  assert.deepEqual(
    [...policyRelativeModules(typescriptModules)].sort(),
    ["dynamic-helper.ts", "feature.test.ts"],
  );

  modules.set("other.ts", "export const other = true;");
  modules.set(
    "feature.test.mjs",
    "const specifier = './dynamic-helper.ts'; import(specifier);"
      + " function shadow() {"
      + " const specifier = './other.ts'; return specifier; }",
  );
  assert.deepEqual(
    [...policyRelativeModules(modules)].sort(),
    ["dynamic-helper.ts", "feature.test.mjs"],
  );

  modules.set(
    "feature.test.mjs",
    "for (const specifier = './dynamic-helper.ts'; flag;) {"
      + " import(specifier); break; }",
  );
  assert.deepEqual(
    [...policyRelativeModules(modules)].sort(),
    ["dynamic-helper.ts", "feature.test.mjs"],
  );

  for (const entrypoint of [
    "switch (flag) { case 1:"
      + " const specifier = './dynamic-helper.ts';"
      + " import(specifier); break; }",
    "namespace N { const specifier = './dynamic-helper.ts';"
      + " import(specifier); }",
  ]) {
    modules.set("feature.test.ts", entrypoint);
    modules.delete("feature.test.mjs");
    assert.deepEqual(
      [...policyRelativeModules(modules)].sort(),
      ["dynamic-helper.ts", "feature.test.ts"],
      entrypoint,
    );
  }
});


test("source policy includes semantic helper modules, not only test entrypoints", async () => {
  const relative = (await policyModules()).map(
    (absolute) => path.relative(FRONTEND_DIR, absolute).replaceAll(path.sep, "/"),
  );
  assert.ok(relative.includes("test-support/semantic-source.mjs"));
  assert.ok(relative.includes("test-support/setup.ts"));
});


test("frontend tests do not inspect production source layout directly", async () => {
  assert.deepEqual(await policyOffenders(), []);
});
