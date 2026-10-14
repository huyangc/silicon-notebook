// KnowhowPanel 拆分后的结构边界(debt-fix S4-d,设计 C7)。
//
// `knowhow-panel.tsx` 原是一个 6348 行的巨石,S4 把它拆成样式模块、三个自带状态机
// 的弹窗、列表/网格/行详情三层视图,以及表列表/行代码/概念矩阵三个 owner hook,
// 要求零行为变化。拆分期用来证明「零变化」的 Profiler 提交次数与样式文本哈希是
// 重构期金标,S4-d 退役(AGENTS.md「Tests」不保留 refactor-only golden);拆分后
// 仍然承重、又不能靠渲染结果观察到的约定,由本守卫按 AST 钉住:
//
//  1. 搬走的组件/常量/hook 不回流:面板里不再声明它们,也不再声明搬进 hook 的
//     state 与命令;每个名字在自己的模块里恰好声明一次(组件与 hook 为 export)。
//  2. 样式挂载点:面板不含 `<style jsx>`;`<KnowhowPanelStyles />` 恰好一处,且是
//     返回的 `<section>` 的最后一个子元素(样式注入顺序与级联位置不变)。
//  3. 拆出的模块不反向 import knowhow-panel.tsx(依赖方向只能是面板 → 模块)。
//  4. effect 与 owner hook 的调用顺序。effect 按声明顺序执行,同一次提交里写同一
//     批 state 时后写的赢,所以这个顺序是行为:notebook 复位必须先于引用跳转(否则
//     挂载即跳转会被复位清掉),概念矩阵 hook 必须是最后一个 hook 调用(它的「切
//     概念时复位删除确认态」effect 搬家前就是面板最后一个 effect)。按语句顺序与
//     依赖数组文本识别,不用行号。依赖数组文本也是行为:拉表详情 effect 只依赖
//     selectedTableId(S6),加回 loadDetail 会让它在切笔记本时先于复位执行、对新
//     笔记本多发一次注定被丢弃的旧表详情请求。
//  4b. 作废 in-flight 表详情的两次 bump(S6):拉详情 effect 的 cleanup 与 notebook
//     复位 effect 的函数体里都必须有 `detailRequestRef.current += 1`。后者只防两次
//     提交之间落地的旧笔记本响应,act() 下的组件用例观察不到那个窗口,只能在这里钉。
//     另外每处 setSelectedTableId 所在的函数都必须先把同一个值写进
//     selectedTableIdRef(openTable 判断「重开同一张表」与异步命令判断「用户还在不在
//     这张表」都读它);反过来,镜像的每次写入也必须与写同一值的 setSelectedTableId
//     同函数成对出现;setter 只许直接调用,不许当作值传出(prop、参数、别名)。
//  5. 回调身份:搬家没有改变任何回调的 useCallback 状态。loadTables 仍是
//     useCallback([notebookId])、loadRowCode 仍是 useCallback([]);
//     handleCodeSaved/handleCodeDeleted/deleteConcept/deleteBranch 仍是每次渲染
//     新建的普通函数(计划第 184 行:**不要**给前两个加 useCallback);面板写的
//     四个矩阵 setter 与列表的 setHealthFilter 以原名返回 useState 原生 setter。
//     面板挂载点按原样传引用(`onOpenConcept={knowhowConceptMatrix.setOpenConceptValue}`
//     而不是包一层箭头)。这些身份变化大多渲染不出差别,只能在这里钉。
//
// owner hook 返回值的使用方式(各调用一次、只经属性访问、不摊平、不进依赖数组)在
// owner-view-no-reflatten-guard.test.mjs。判据只看 AST 形状,不含行号或源码位置。
import test from "node:test";
import assert from "node:assert/strict";
import ts from "typescript";

import {
  findFunction,
  importsIn,
  jsxElements,
  parseModule,
  parseText,
} from "../../test-support/semantic-source.mjs";

const PANEL_MODULE = "knowhow-panel.tsx";

// 拆出的模块 → 在该模块顶层声明的名字(export 与否)。
const SPLIT_MODULES = new Map([
  ["knowhow-panel-styles.tsx", [["KnowhowPanelStyles", true]]],
  ["knowhow-row-completion-modal.tsx", [["KnowhowRowCompletionModal", true]]],
  ["knowhow-row-optimize-modal.tsx", [["KnowhowRowOptimizeModal", true]]],
  ["knowhow-reformat-batch-modal.tsx", [["KnowhowReformatBatchModal", true]]],
  ["knowhow-table-list.tsx", [["KnowhowTableList", true], ["KNOWHOW_TABLE_PAGE_SIZE", false]]],
  ["knowhow-table-grid.tsx", [["KnowhowTableGrid", true], ["ColumnNameCell", false]]],
  ["knowhow-badges.tsx", [["RoleBadge", true], ["ProjectionStatusBadge", true]]],
  ["knowhow-row-drawer.tsx", [["KnowhowRowDrawer", true]]],
  ["use-knowhow-table-list.ts", [["useKnowhowTableList", true]]],
  ["use-knowhow-row-code.ts", [["useKnowhowRowCode", true]]],
  ["use-knowhow-concept-matrix.ts", [["useKnowhowConceptMatrix", true]]],
]);

// 搬进三个 hook 的 state、派生值与命令:面板里任何作用域都不得再声明这些名字。
const MOVED_INTO_HOOKS = [
  "tables", "tablesLoading", "tablesError", "healthFilter", "visibleTables", "loadTables",
  "rowCodeByColumn", "rowCodeLoaded", "rowCodeError", "rowCodeRequestRef", "loadRowCode",
  "handleCodeSaved", "handleCodeDeleted",
  "openConceptValue", "highlightRowId", "confirmDeleteConcept", "deletingConcept",
  "conceptDrawerError", "deletingBranchRowId", "openConceptGroup", "deleteConcept", "deleteBranch",
];

// KnowhowPanel 函数体顶层的 effect 与 owner hook 调用顺序(effect 以依赖数组标识)。
const PANEL_HOOK_ORDER = [
  "useKnowhowTableList",
  "useEffect[]", // panelMountedRef
  "useEffect[selectedTableId]", // 拉表详情(只跟选中表走,不随 loadDetail 身份重跑,S6)
  "useKnowhowRowCode", // 原「按 openRowId 拉行代码」effect 的位置
  "useEffect[notebookId]", // notebook 复位(必须先于引用跳转)
  "useEffect[initialTableId, initialRowId]", // 引用跳转:openTable
  "useEffect[initialRowId, detail]", // detail 到达后分流
  "useEffect[detail, detailError, initialRowId, initialTableId, selectedTableId]", // 陈旧引用提示
  "useKnowhowConceptMatrix", // 最后:矩阵确认态复位 effect 仍最后执行
];

// 三个 hook 的回调身份规格。
const HOOK_CALLBACKS = new Map([
  ["use-knowhow-table-list.ts", {
    hook: "useKnowhowTableList",
    useCallbackDeps: { loadTables: "[notebookId]" },
    plainFunctions: [],
    nativeSetters: ["setHealthFilter"],
  }],
  ["use-knowhow-row-code.ts", {
    hook: "useKnowhowRowCode",
    useCallbackDeps: { loadRowCode: "[]" },
    plainFunctions: ["handleCodeSaved", "handleCodeDeleted"],
    nativeSetters: [],
  }],
  ["use-knowhow-concept-matrix.ts", {
    hook: "useKnowhowConceptMatrix",
    useCallbackDeps: {},
    plainFunctions: ["deleteConcept", "deleteBranch"],
    nativeSetters: ["setOpenConceptValue", "setHighlightRowId", "setConfirmDeleteConcept", "setConceptDrawerError"],
  }],
]);

// 面板挂载点:这些 prop 原样传 hook 上的引用(身份与搬家前相同)。
const PANEL_BINDINGS = [
  ["KnowhowTableList", "onHealthFilterChange", "knowhowTableList.setHealthFilter"],
  ["KnowhowTableList", "onRetry", "knowhowTableList.loadTables"],
  ["KnowhowTableGrid", "onOpenConcept", "knowhowConceptMatrix.setOpenConceptValue"],
  ["KnowhowMatrixDrawer", "onConfirmDeleteConcept", "knowhowConceptMatrix.deleteConcept"],
  ["KnowhowMatrixDrawer", "onDeleteBranch", "knowhowConceptMatrix.deleteBranch"],
  ["KnowhowCodeModal", "onSaved", "knowhowRowCode.handleCodeSaved"],
  ["KnowhowCodeModal", "onDeleted", "knowhowRowCode.handleCodeDeleted"],
];

function flatText(node, sourceFile) {
  return node.getText(sourceFile).replace(/\s+/g, " ").trim();
}

function isExported(node) {
  return (ts.getCombinedModifierFlags(node) & ts.ModifierFlags.Export) !== 0;
}

function boundNames(name, into) {
  if (ts.isIdentifier(name)) {
    into.push(name.text);
  } else {
    for (const element of name.elements) {
      if (!ts.isOmittedExpression(element)) boundNames(element.name, into);
    }
  }
  return into;
}

// 模块里任何作用域声明的名字(函数、变量与解构绑定)。
function declaredNamesAnywhere(sourceFile) {
  const names = [];
  function visit(node) {
    if (ts.isFunctionDeclaration(node) && node.name) names.push(node.name.text);
    if (ts.isVariableDeclaration(node)) boundNames(node.name, names);
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return names;
}

// 模块顶层声明:名字 → { count, exported }。
function topLevelDeclarations(sourceFile) {
  const found = new Map();
  const record = (name, exported) => {
    const entry = found.get(name) ?? { count: 0, exported: false };
    found.set(name, { count: entry.count + 1, exported: entry.exported || exported });
  };
  for (const statement of sourceFile.statements) {
    if (ts.isFunctionDeclaration(statement) && statement.name) {
      record(statement.name.text, isExported(statement));
    } else if (ts.isVariableStatement(statement)) {
      for (const declaration of statement.declarationList.declarations) {
        for (const name of boundNames(declaration.name, [])) record(name, isExported(statement));
      }
    }
  }
  return found;
}

// ── 1. 搬走的名字不回流 ───────────────────────────────────────────────────────

function panelRedeclarationViolations(sourceFile) {
  const forbidden = new Set([
    ...[...SPLIT_MODULES.values()].flat().map(([name]) => name),
    ...MOVED_INTO_HOOKS,
  ]);
  return [...new Set(declaredNamesAnywhere(sourceFile).filter((name) => forbidden.has(name)))]
    .map((name) => `面板又声明了已搬走的 ${name}`);
}

function ownerModuleViolations(sourceFile, expected) {
  const found = topLevelDeclarations(sourceFile);
  const violations = [];
  for (const [name, mustExport] of expected) {
    const entry = found.get(name);
    if (!entry || entry.count !== 1) {
      violations.push(`${name} 在本模块顶层应声明恰好一次,实际 ${entry?.count ?? 0} 次`);
    } else if (mustExport && !entry.exported) {
      violations.push(`${name} 必须 export`);
    }
  }
  return violations;
}

// ── 2. 样式挂载点 ───────────────────────────────────────────────────────────

function returnedJsxRoot(owner) {
  for (const statement of owner.body.statements) {
    if (!ts.isReturnStatement(statement) || !statement.expression) continue;
    let expression = statement.expression;
    while (ts.isParenthesizedExpression(expression)) expression = expression.expression;
    if (ts.isJsxElement(expression)) return expression;
  }
  return null;
}

function styleMountViolations(sourceFile, ownerName) {
  const violations = [];
  function visit(node) {
    if (
      (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node))
      && node.tagName.getText(sourceFile) === "style"
    ) {
      violations.push("面板里出现了 <style> 元素(样式只在 knowhow-panel-styles.tsx)");
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  const mounts = jsxElements(sourceFile, "KnowhowPanelStyles");
  if (mounts.length !== 1) violations.push(`<KnowhowPanelStyles /> 应恰好一处,实际 ${mounts.length} 处`);
  const root = returnedJsxRoot(findFunction(sourceFile, ownerName));
  const children = root
    ? root.children.filter((child) => !(ts.isJsxText(child) && child.containsOnlyTriviaWhiteSpaces))
    : [];
  const last = children[children.length - 1];
  const lastIsStyles = Boolean(
    root
    && root.openingElement.tagName.getText(sourceFile) === "section"
    && last
    && ts.isJsxSelfClosingElement(last)
    && last.tagName.getText(sourceFile) === "KnowhowPanelStyles",
  );
  if (!lastIsStyles) violations.push("<KnowhowPanelStyles /> 必须是返回的 <section> 的最后一个子元素");
  return violations;
}

// ── 3. 不反向 import 面板 ────────────────────────────────────────────────────

function reverseImportViolations(sourceFile) {
  return importsIn(sourceFile)
    .filter(({ module }) => /(^|\/)knowhow-panel(\.tsx)?$/.test(module))
    .map(({ module, imported }) => `反向 import ${module} 的 ${imported}`);
}

// ── 4. effect / owner hook 调用顺序 ─────────────────────────────────────────

function hookCallLabel(call, sourceFile) {
  if (!ts.isCallExpression(call) || !ts.isIdentifier(call.expression)) return null;
  const name = call.expression.text;
  if (name === "useEffect" || name === "useLayoutEffect") {
    const deps = call.arguments[1];
    return `${name}${deps ? flatText(deps, sourceFile) : "<无依赖数组>"}`;
  }
  return /^useKnowhow[A-Z]/.test(name) ? name : null;
}

function statementHookCall(statement) {
  if (ts.isExpressionStatement(statement)) return statement.expression;
  if (ts.isVariableStatement(statement)) {
    const [declaration] = statement.declarationList.declarations;
    return declaration?.initializer ?? null;
  }
  return null;
}

function callsAnyHook(node) {
  let found = false;
  function visit(child) {
    if (found) return;
    if (
      ts.isCallExpression(child)
      && ts.isIdentifier(child.expression)
      && /^use[A-Z]/.test(child.expression.text)
    ) {
      found = true;
      return;
    }
    ts.forEachChild(child, visit);
  }
  visit(node);
  return found;
}

function panelOrderViolations(sourceFile, ownerName, expected) {
  const owner = findFunction(sourceFile, ownerName);
  const labels = [];
  let afterLastOwnerHook = false;
  const lateHookCalls = [];
  const lastOwnerHook = expected[expected.length - 1];
  for (const statement of owner.body.statements) {
    const call = statementHookCall(statement);
    const label = call ? hookCallLabel(call, sourceFile) : null;
    if (afterLastOwnerHook && callsAnyHook(statement)) lateHookCalls.push(flatText(statement, sourceFile));
    if (label) labels.push(label);
    if (label === lastOwnerHook) afterLastOwnerHook = true;
  }
  const violations = [];
  if (JSON.stringify(labels) !== JSON.stringify(expected)) {
    violations.push(`effect/owner hook 顺序变了:${JSON.stringify(labels)}`);
  }
  for (const text of lateHookCalls) violations.push(`${lastOwnerHook} 之后还有 hook 调用:${text}`);
  return violations;
}


// ── 4b. 详情请求作废点与选中表镜像 ────────────────────────────────────────────

const DETAIL_BUMP = "detailRequestRef.current += 1";

function topLevelEffect(owner, sourceFile, depsText) {
  for (const statement of owner.body.statements) {
    const call = statementHookCall(statement);
    if (
      call && ts.isCallExpression(call) && ts.isIdentifier(call.expression)
      && call.expression.text === "useEffect" && call.arguments[1]
      && flatText(call.arguments[1], sourceFile) === depsText
    ) return call.arguments[0];
  }
  return null;
}

function bodyStatements(fn) {
  return fn && fn.body && ts.isBlock(fn.body) ? fn.body.statements : [];
}

function hasBump(statements, sourceFile) {
  return statements.some((statement) => ts.isExpressionStatement(statement) && flatText(statement.expression, sourceFile) === DETAIL_BUMP);
}

function detailInvalidationViolations(sourceFile, ownerName) {
  const owner = findFunction(sourceFile, ownerName);
  const violations = [];
  const reset = topLevelEffect(owner, sourceFile, "[notebookId]");
  if (!reset || !hasBump(bodyStatements(reset), sourceFile)) {
    violations.push(`notebook 复位 effect 的函数体里必须有 ${DETAIL_BUMP}`);
  }
  const load = topLevelEffect(owner, sourceFile, "[selectedTableId]");
  const cleanup = bodyStatements(load)
    .filter((statement) => ts.isReturnStatement(statement) && statement.expression)
    .map((statement) => statement.expression)
    .find((expression) => ts.isArrowFunction(expression) || ts.isFunctionExpression(expression));
  if (!cleanup || !hasBump(bodyStatements(cleanup), sourceFile)) {
    violations.push(`拉表详情 effect 的 cleanup 里必须有 ${DETAIL_BUMP}`);
  }
  return violations;
}

function enclosingFunction(node) {
  let current = node.parent;
  while (current && !ts.isFunctionDeclaration(current) && !ts.isArrowFunction(current) && !ts.isFunctionExpression(current)) {
    current = current.parent;
  }
  return current ?? null;
}

function statementTexts(fn, sourceFile) {
  return bodyStatements(fn)
    .filter((statement) => ts.isExpressionStatement(statement))
    .map((statement) => flatText(statement.expression, sourceFile));
}

function selectionMirrorViolations(sourceFile, ownerName) {
  const owner = findFunction(sourceFile, ownerName);
  const violations = [];
  function visit(node) {
    // setter 只许直接调用:当作值传出去(prop、参数、赋给别的名字)就绕过了镜像。
    if (
      ts.isIdentifier(node) && node.text === "setSelectedTableId"
      && !(ts.isCallExpression(node.parent) && node.parent.expression === node)
      && !ts.isBindingElement(node.parent)
    ) {
      violations.push(`setSelectedTableId 只能直接调用,不能当作值传出:${flatText(node.parent, sourceFile)}`);
    }
    // 镜像只许跟着选中表一起写:同一函数里必须有写同一个值的 setSelectedTableId。
    if (
      ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
      && flatText(node.left, sourceFile) === "selectedTableIdRef.current"
    ) {
      const value = flatText(node.right, sourceFile);
      if (!statementTexts(enclosingFunction(node), sourceFile).includes(`setSelectedTableId(${value})`)) {
        violations.push(`selectedTableIdRef.current = ${value} 所在函数没有 setSelectedTableId(${value})`);
      }
    }
    if (
      ts.isCallExpression(node) && ts.isIdentifier(node.expression)
      && node.expression.text === "setSelectedTableId" && node.arguments.length === 1
    ) {
      const value = flatText(node.arguments[0], sourceFile);
      const mirrored = statementTexts(enclosingFunction(node), sourceFile).includes(`selectedTableIdRef.current = ${value}`);
      if (!mirrored) violations.push(`setSelectedTableId(${value}) 所在函数没有同步写 selectedTableIdRef.current = ${value}`);
    }
    ts.forEachChild(node, visit);
  }
  visit(owner.body);
  return violations;
}

// ── 5. 回调身份 ─────────────────────────────────────────────────────────────

function hookCallbackViolations(sourceFile, spec) {
  const hook = findFunction(sourceFile, spec.hook);
  const violations = [];
  const plainFunctions = new Set();
  const useStateSetters = new Set();
  const variables = new Map();
  for (const statement of hook.body.statements) {
    if (ts.isFunctionDeclaration(statement) && statement.name) plainFunctions.add(statement.name.text);
    if (!ts.isVariableStatement(statement)) continue;
    for (const declaration of statement.declarationList.declarations) {
      const initializer = declaration.initializer;
      if (ts.isIdentifier(declaration.name)) variables.set(declaration.name.text, initializer);
      if (
        ts.isArrayBindingPattern(declaration.name)
        && initializer
        && ts.isCallExpression(initializer)
        && ts.isIdentifier(initializer.expression)
        && initializer.expression.text === "useState"
      ) {
        const setter = declaration.name.elements[1];
        if (setter && !ts.isOmittedExpression(setter) && ts.isIdentifier(setter.name)) {
          useStateSetters.add(setter.name.text);
        }
      }
    }
  }
  const shorthandReturns = new Set();
  for (const statement of hook.body.statements) {
    if (!ts.isReturnStatement(statement) || !statement.expression) continue;
    if (!ts.isObjectLiteralExpression(statement.expression)) continue;
    for (const property of statement.expression.properties) {
      if (ts.isShorthandPropertyAssignment(property)) shorthandReturns.add(property.name.text);
    }
  }
  for (const [name, deps] of Object.entries(spec.useCallbackDeps)) {
    const initializer = variables.get(name);
    const ok = initializer
      && ts.isCallExpression(initializer)
      && ts.isIdentifier(initializer.expression)
      && initializer.expression.text === "useCallback"
      && initializer.arguments[1]
      && flatText(initializer.arguments[1], sourceFile) === deps;
    if (!ok) violations.push(`${name} 必须仍是 useCallback(…, ${deps})`);
  }
  for (const name of spec.plainFunctions) {
    if (!plainFunctions.has(name) || variables.has(name)) {
      violations.push(`${name} 必须仍是 hook 体内的普通函数声明(不包 useCallback)`);
    }
  }
  for (const name of spec.nativeSetters) {
    if (!useStateSetters.has(name)) violations.push(`${name} 必须是 useState 的原生 setter`);
  }
  for (const name of [...Object.keys(spec.useCallbackDeps), ...spec.plainFunctions, ...spec.nativeSetters]) {
    if (!shorthandReturns.has(name)) violations.push(`${name} 必须以原名原样返回(shorthand)`);
  }
  return violations;
}

function panelBindingViolations(sourceFile) {
  const violations = [];
  for (const [element, prop, expected] of PANEL_BINDINGS) {
    const mounts = jsxElements(sourceFile, element);
    const actual = mounts.map((mount) => mount.bindings?.[prop]);
    if (actual.length !== 1 || actual[0] !== expected) {
      violations.push(`<${element} ${prop}> 应原样传 ${expected},实际 ${JSON.stringify(actual)}`);
    }
  }
  return violations;
}

// ── 真实源码 ───────────────────────────────────────────────────────────────

const panel = await parseModule(PANEL_MODULE);

test("搬走的组件、常量、hook 与 hook 内 state/命令不回流到面板", () => {
  assert.deepEqual(panelRedeclarationViolations(panel), []);
});

test("每个搬走的名字在自己的模块里恰好声明一次,组件与 hook 为 export", async () => {
  for (const [module, expected] of SPLIT_MODULES) {
    assert.deepEqual(ownerModuleViolations(await parseModule(module), expected), [], module);
  }
});

test("面板不含 <style jsx>,<KnowhowPanelStyles /> 恰好一处且是 section 的最后一个子元素", () => {
  assert.deepEqual(styleMountViolations(panel, "KnowhowPanel"), []);
});

test("拆出的模块不反向 import knowhow-panel.tsx", async () => {
  for (const module of SPLIT_MODULES.keys()) {
    assert.deepEqual(reverseImportViolations(await parseModule(module)), [], module);
  }
});

test("KnowhowPanel 的 effect 与 owner hook 调用顺序不变,概念矩阵 hook 最后调用", () => {
  assert.deepEqual(panelOrderViolations(panel, "KnowhowPanel", PANEL_HOOK_ORDER), []);
});

test("表详情请求的两处作废点都在,选中表镜像与每处 setSelectedTableId 同步", () => {
  assert.deepEqual(detailInvalidationViolations(panel, "KnowhowPanel"), []);
  assert.deepEqual(selectionMirrorViolations(panel, "KnowhowPanel"), []);
});

test("三个 owner hook 的回调身份与搬家前相同", async () => {
  for (const [module, spec] of HOOK_CALLBACKS) {
    assert.deepEqual(hookCallbackViolations(await parseModule(module), spec), [], module);
  }
});

test("面板挂载点原样传 hook 上的回调引用", () => {
  assert.deepEqual(panelBindingViolations(panel), []);
});

// ── 自测:每条判据在合成源码上正例零违规、反例被抓住 ───────────────────────────

function mutate(base, from, to) {
  assert.ok(base.includes(from), `锚点不存在:${from}`);
  assert.equal(base.replaceAll(from, to), base.replace(from, to), `锚点不唯一:${from}`);
  return base.replace(from, to);
}

const SELF_PANEL = `
import { KnowhowTableList } from "./knowhow-table-list.tsx";
function Panel({ notebookId }) {
  const knowhowTableList = useKnowhowTableList({ notebookId });
  useEffect(() => { mounted.current = true; }, []);
  useEffect(() => { load(); return () => { detailRequestRef.current += 1; }; }, [selectedTableId]);
  const knowhowRowCode = useKnowhowRowCode(openRowId);
  useEffect(() => { detailRequestRef.current += 1; selectedTableIdRef.current = null; setSelectedTableId(null); }, [notebookId]);
  useEffect(() => { jump(); }, [initialTableId, initialRowId]);
  useEffect(() => { route(); }, [initialRowId, detail]);
  useEffect(() => { stale(); }, [detail, detailError, initialRowId, initialTableId, selectedTableId]);
  function openTable(tableId) { selectedTableIdRef.current = tableId; setSelectedTableId(tableId); }
  const openRow = rows.find((row) => row.id === openRowId);
  const knowhowConceptMatrix = useKnowhowConceptMatrix({ notebookId });
  return (
    <section className="knowhow-view">
      <KnowhowTableList onHealthFilterChange={knowhowTableList.setHealthFilter} onRetry={knowhowTableList.loadTables} />
      <KnowhowTableGrid onOpenConcept={knowhowConceptMatrix.setOpenConceptValue} />
      <KnowhowMatrixDrawer onConfirmDeleteConcept={knowhowConceptMatrix.deleteConcept} onDeleteBranch={knowhowConceptMatrix.deleteBranch} />
      <KnowhowCodeModal onSaved={knowhowRowCode.handleCodeSaved} onDeleted={knowhowRowCode.handleCodeDeleted} />
      <KnowhowPanelStyles />
    </section>
  );
}
`;

const SELF_ROW_CODE_HOOK = `
export function useKnowhowRowCode(openRowId) {
  const [rowCodeByColumn, setRowCodeByColumn] = useState({});
  const loadRowCode = useCallback((rowId) => { fetchIt(rowId); }, []);
  function handleCodeSaved(columnId, entry) { setRowCodeByColumn((prev) => ({ ...prev, [columnId]: entry })); }
  function handleCodeDeleted(columnId) { setRowCodeByColumn((prev) => prev); }
  return { view: { rowCodeByColumn }, loadRowCode, handleCodeSaved, handleCodeDeleted };
}
`;

const SELF_TABLE_LIST_HOOK = `
export function useKnowhowTableList({ notebookId }) {
  const [healthFilter, setHealthFilter] = useState("all");
  const loadTables = useCallback(() => { fetchIt(notebookId); }, [notebookId]);
  return { view: { healthFilter }, setHealthFilter, loadTables };
}
`;

const ROW_CODE_SPEC = HOOK_CALLBACKS.get("use-knowhow-row-code.ts");
const TABLE_LIST_SPEC = HOOK_CALLBACKS.get("use-knowhow-table-list.ts");

function selfPanelViolations(body) {
  const sourceFile = parseText(body, "self-test.tsx");
  return [
    ...panelRedeclarationViolations(sourceFile),
    ...styleMountViolations(sourceFile, "Panel"),
    ...reverseImportViolations(sourceFile),
    ...panelOrderViolations(sourceFile, "Panel", PANEL_HOOK_ORDER),
    ...panelBindingViolations(sourceFile),
    ...detailInvalidationViolations(sourceFile, "Panel"),
    ...selectionMirrorViolations(sourceFile, "Panel"),
  ];
}

test("自测:面板判据对合规写法零违规", () => {
  assert.deepEqual(selfPanelViolations(SELF_PANEL), []);
  assert.deepEqual(hookCallbackViolations(parseText(SELF_ROW_CODE_HOOK, "self-row.ts"), ROW_CODE_SPEC), []);
  assert.deepEqual(hookCallbackViolations(parseText(SELF_TABLE_LIST_HOOK, "self-list.ts"), TABLE_LIST_SPEC), []);
});

const PANEL_MUTANTS = [
  {
    label: "面板重新定义搬走的组件",
    from: "function Panel(",
    to: "function RoleBadge() { return null; }\nfunction Panel(",
    expect: /面板又声明了已搬走的 RoleBadge/,
  },
  {
    label: "面板重新持有搬进 hook 的 state",
    from: "  const openRow =",
    to: "  const [openConceptValue, setOpenConceptValue] = useState(null);\n  const openRow =",
    expect: /面板又声明了已搬走的 openConceptValue/,
  },
  {
    label: "面板里写回 <style jsx global>",
    from: "      <KnowhowPanelStyles />",
    to: "      <style jsx global>{``}</style>\n      <KnowhowPanelStyles />",
    expect: /面板里出现了 <style> 元素/,
  },
  {
    label: "样式不再是 section 的最后一个子元素",
    from: "      <KnowhowPanelStyles />\n",
    to: "      <KnowhowPanelStyles />\n      <Footer />\n",
    expect: /必须是返回的 <section> 的最后一个子元素/,
  },
  {
    label: "样式挂两次",
    from: "      <KnowhowTableGrid",
    to: "      <KnowhowPanelStyles />\n      <KnowhowTableGrid",
    expect: /应恰好一处,实际 2 处/,
  },
  {
    label: "反向 import 面板",
    from: 'import { KnowhowTableList } from "./knowhow-table-list.tsx";',
    to: 'import { KnowhowPanel } from "./knowhow-panel.tsx";',
    expect: /反向 import \.\/knowhow-panel\.tsx 的 KnowhowPanel/,
  },
  {
    label: "行代码 hook 提前到拉表详情 effect 之前",
    from: "  useEffect(() => { load(); return () => { detailRequestRef.current += 1; }; }, [selectedTableId]);\n  const knowhowRowCode = useKnowhowRowCode(openRowId);",
    to: "  const knowhowRowCode = useKnowhowRowCode(openRowId);\n  useEffect(() => { load(); return () => { detailRequestRef.current += 1; }; }, [selectedTableId]);",
    expect: /effect\/owner hook 顺序变了/,
  },
  {
    label: "notebook 复位与引用跳转对调",
    from: "  useEffect(() => { detailRequestRef.current += 1; selectedTableIdRef.current = null; setSelectedTableId(null); }, [notebookId]);\n  useEffect(() => { jump(); }, [initialTableId, initialRowId]);",
    to: "  useEffect(() => { jump(); }, [initialTableId, initialRowId]);\n  useEffect(() => { detailRequestRef.current += 1; selectedTableIdRef.current = null; setSelectedTableId(null); }, [notebookId]);",
    expect: /effect\/owner hook 顺序变了/,
  },
  {
    label: "概念矩阵 hook 提前调用",
    from: "  const knowhowConceptMatrix = useKnowhowConceptMatrix({ notebookId });\n",
    to: "",
    expect: /effect\/owner hook 顺序变了/,
    extra: ["  const knowhowRowCode = useKnowhowRowCode(openRowId);", "  const knowhowRowCode = useKnowhowRowCode(openRowId);\n  const knowhowConceptMatrix = useKnowhowConceptMatrix({ notebookId });"],
  },
  {
    label: "概念矩阵 hook 之后又调用 hook",
    from: "  const knowhowConceptMatrix = useKnowhowConceptMatrix({ notebookId });\n",
    to: "  const knowhowConceptMatrix = useKnowhowConceptMatrix({ notebookId });\n  const late = useMemo(() => 1, []);\n",
    expect: /useKnowhowConceptMatrix 之后还有 hook 调用/,
  },
  {
    label: "notebook 复位不再作废 in-flight 表详情",
    from: "  useEffect(() => { detailRequestRef.current += 1; selectedTableIdRef.current = null;",
    to: "  useEffect(() => { selectedTableIdRef.current = null;",
    expect: /notebook 复位 effect 的函数体里必须有/,
  },
  {
    label: "拉表详情 effect 的 cleanup 不再作废",
    from: "load(); return () => { detailRequestRef.current += 1; }; }, [selectedTableId]);",
    to: "load(); return () => { cancelled = true; }; }, [selectedTableId]);",
    expect: /拉表详情 effect 的 cleanup 里必须有/,
  },
  {
    label: "openTable 写选中表却不写镜像",
    from: "function openTable(tableId) { selectedTableIdRef.current = tableId; setSelectedTableId(tableId); }",
    to: "function openTable(tableId) { setSelectedTableId(tableId); }",
    expect: /setSelectedTableId\(tableId\) 所在函数没有同步写 selectedTableIdRef\.current = tableId/,
  },
  {
    label: "镜像写的值与选中表不一致",
    from: "selectedTableIdRef.current = null; setSelectedTableId(null); }, [notebookId]);",
    to: "selectedTableIdRef.current = undefined; setSelectedTableId(null); }, [notebookId]);",
    expect: /setSelectedTableId\(null\) 所在函数没有同步写/,
  },
  {
    label: "把 setSelectedTableId 当作 prop 传出",
    from: "      <KnowhowTableList onHealthFilterChange",
    to: "      <KnowhowTableList onOpen={setSelectedTableId} onHealthFilterChange",
    expect: /setSelectedTableId 只能直接调用,不能当作值传出/,
  },
  {
    label: "只写镜像、不写选中表",
    from: "  const openRow = rows.find",
    to: "  function peek(tableId) { selectedTableIdRef.current = tableId; }\n  const openRow = rows.find",
    expect: /selectedTableIdRef\.current = tableId 所在函数没有 setSelectedTableId\(tableId\)/,
  },
  {
    label: "setter 包一层箭头再传",
    from: "onOpenConcept={knowhowConceptMatrix.setOpenConceptValue}",
    to: "onOpenConcept={(value) => knowhowConceptMatrix.setOpenConceptValue(value)}",
    expect: /<KnowhowTableGrid onOpenConcept> 应原样传/,
  },
];

for (const mutant of PANEL_MUTANTS) {
  test(`自测:面板判据抓住「${mutant.label}」`, () => {
    let body = mutate(SELF_PANEL, mutant.from, mutant.to);
    if (mutant.extra) body = mutate(body, mutant.extra[0], mutant.extra[1]);
    const violations = selfPanelViolations(body);
    assert.ok(violations.some((violation) => mutant.expect.test(violation)), `没有抓到:${JSON.stringify(violations)}`);
  });
}

const HOOK_MUTANTS = [
  {
    label: "给 handleCodeSaved 加 useCallback",
    base: SELF_ROW_CODE_HOOK,
    spec: ROW_CODE_SPEC,
    from: "  function handleCodeSaved(columnId, entry) { setRowCodeByColumn((prev) => ({ ...prev, [columnId]: entry })); }",
    to: "  const handleCodeSaved = useCallback((columnId, entry) => { setRowCodeByColumn((prev) => ({ ...prev, [columnId]: entry })); }, []);",
    expect: /handleCodeSaved 必须仍是 hook 体内的普通函数声明/,
  },
  {
    label: "loadRowCode 的依赖数组变了",
    base: SELF_ROW_CODE_HOOK,
    spec: ROW_CODE_SPEC,
    from: "}, []);\n  function handleCodeSaved",
    to: "}, [openRowId]);\n  function handleCodeSaved",
    expect: /loadRowCode 必须仍是 useCallback\(…, \[\]\)/,
  },
  {
    label: "loadTables 去掉 useCallback",
    base: SELF_TABLE_LIST_HOOK,
    spec: TABLE_LIST_SPEC,
    from: "const loadTables = useCallback(() => { fetchIt(notebookId); }, [notebookId]);",
    to: "const loadTables = () => { fetchIt(notebookId); };",
    expect: /loadTables 必须仍是 useCallback\(…, \[notebookId\]\)/,
  },
  {
    label: "原生 setter 换成包装函数返回",
    base: SELF_TABLE_LIST_HOOK,
    spec: TABLE_LIST_SPEC,
    from: "setHealthFilter, loadTables };",
    to: "setHealthFilter: (next) => setHealthFilter(next), loadTables };",
    expect: /setHealthFilter 必须以原名原样返回/,
  },
];

for (const mutant of HOOK_MUTANTS) {
  test(`自测:回调身份判据抓住「${mutant.label}」`, () => {
    const violations = hookCallbackViolations(parseText(mutate(mutant.base, mutant.from, mutant.to), "self-hook.ts"), mutant.spec);
    assert.ok(violations.some((violation) => mutant.expect.test(violation)), `没有抓到:${JSON.stringify(violations)}`);
  });
}
