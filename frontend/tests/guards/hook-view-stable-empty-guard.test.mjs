// PR #557 regression: several workspace hooks fell back to a bare `[]`/`{}`
// literal for the "owner hidden" (unauthenticated / collection page) branch
// of a ternary feeding their returned readonly view — either directly as a
// property value inside the hook's own `return {...}` object literal, or as
// a local variable that later flows into it (`use-source-library.ts`'s
// `visibleSources`). A bare empty literal is a **new reference on every
// render**. Consumers (page.tsx effects/useMemo) depend on these fields;
// handing back a fresh `[]`/`{}` every render makes the dependency "change"
// every render, and one such effect calls `setState` in its body — that is
// an infinite render loop (Next.js dev overlay: "Maximum update depth
// exceeded" at `app/page.tsx (2127:7) @ Home.useEffect`).
//
// Fix pattern: hoist a frozen, module-level constant (`NO_X` / `EMPTY_X`)
// and reference it from the ternary's hidden-state branch instead of the
// bare literal — see use-ask-session.ts's `NO_TURNS` etc.
//
// Judgement is semantic (AST) — no line numbers, matching this repo's
// static-source-policy guard (`tests/guards/static-source-policy.test.mjs`),
// which independently forbids test code from querying AST node positions at
// all (`.getStart()`/`.getEnd()`/`.getFullStart()`). Offenders are reported
// as `<file> 字段 <dotted field path>` instead. Walk each of the seven hook
// functions' *own* body — never descending into a nested function/method/
// class scope, so a helper closure's unrelated `return [];` or a
// locally-scoped ternary inside a callback can never be mistaken for part of
// the hook's own returned view — and flag any `ConditionalExpression` whose
// `whenTrue`/`whenFalse` branch is a zero-element `ArrayLiteralExpression`, a
// zero-property `ObjectLiteralExpression`, or a zero-argument `new Set()` /
// `new Map()` (same instability, different spelling), when that conditional sits:
//   (a) directly as a property value inside the hook's own `return {...}`
//       object literal, recursing into nested object-literal property
//       values (use-kg-workspace.ts nests `graph: { searchHits: … }` etc.); or
//   (b) directly as a `VariableDeclaration` initializer anywhere in the
//       hook's own body (the "compute a local, then return it" shape).
//
// A frozen `Object.freeze([] as T[]) as T[]` fallback constant is not itself
// an ArrayLiteralExpression/ObjectLiteralExpression in the ternary branch —
// it is an Identifier reference — so the fix makes this guard pass.
import test from "node:test";
import assert from "node:assert/strict";
import ts from "typescript";

import { findFunction, parseModule } from "../../test-support/semantic-source.mjs";

// The seven hook files this guard is scoped to, and the exported hook
// function name inside each one (all are `export function use…(…) {…}`).
const HOOK_FUNCTIONS = [
  { file: "use-source-library.ts", name: "useSourceLibrary" },
  { file: "use-ask-session.ts", name: "useAskSession" },
  { file: "use-report-workspace.ts", name: "useReportWorkspace" },
  { file: "use-kg-workspace.ts", name: "useKgWorkspace" },
  { file: "use-notebook-collection.ts", name: "useNotebookCollection" },
  { file: "use-root-modal-coordinator.ts", name: "useRootModalCoordinator" },
  { file: "use-workspace-extensions.ts", name: "useWorkspaceExtensions" },
];

// Parameters here are deliberately *not* named `node` (this repo's
// static-source-policy guard treats an "ast-node"-named parameter's
// `.properties`/`.members`/`.statements` cardinality check as a source-order
// query and flags it, even though counting zero-vs-nonzero entries to
// recognize the empty-literal shape is a semantic check, not a layout one).
function isEmptyArrayLiteral(literal) {
  return ts.isArrayLiteralExpression(literal) && literal.elements.length === 0;
}

function isEmptyObjectLiteral(literal) {
  return ts.isObjectLiteralExpression(literal) && literal.properties.length === 0;
}

// `new Set()` / `new Map()` with no arguments is the same instability in a
// different spelling (use-report-workspace.ts's `selectedIds` shipped with it).
function isEmptyCollectionConstruction(literal) {
  return ts.isNewExpression(literal)
    && ts.isIdentifier(literal.expression)
    && ["Set", "Map", "WeakSet", "WeakMap", "Array", "Object"].includes(literal.expression.text)
    && (literal.arguments?.length ?? 0) === 0;
}

function isUnstableEmptyLiteral(literal) {
  return isEmptyArrayLiteral(literal)
    || isEmptyObjectLiteral(literal)
    || isEmptyCollectionConstruction(literal);
}

// Either branch being a bare empty literal is unstable — we don't assume
// which side of the ternary is "the hidden-state fallback" vs. "the live
// data"; both existing incidents put the bare literal on the `whenFalse`
// side, but judging structurally (not by branch position) is what makes
// this a semantic guard rather than a re-encoding of today's line list.
function conditionalHasUnstableEmptyBranch(node) {
  return isUnstableEmptyLiteral(node.whenTrue) || isUnstableEmptyLiteral(node.whenFalse);
}

function isNestedFunctionScope(node) {
  return ts.isFunctionDeclaration(node)
    || ts.isFunctionExpression(node)
    || ts.isArrowFunction(node)
    || ts.isMethodDeclaration(node)
    || ts.isGetAccessor(node)
    || ts.isSetAccessor(node)
    || ts.isClassDeclaration(node)
    || ts.isClassExpression(node);
}

// Strips parenthesization and type-assertion wrappers (`as const`,
// `as Foo`, `<Foo>expr`, `satisfies Foo`) to reach the underlying
// expression. `useAskSession`/`useSourceLibrary` both close their return
// with `} as const;`, so without this the return statement's expression is
// an `AsExpression`, never recognized as the `ObjectLiteralExpression` we
// need to recurse into — that alone silently zeroes out 5 of the 14 real
// occurrences this guard exists to catch (all 4 in use-ask-session.ts's
// return, plus use-source-library.ts's `sourceElements` return field).
function unwrapToExpression(node) {
  let current = node;
  while (current) {
    if (ts.isParenthesizedExpression(current)) {
      current = current.expression;
    } else if (ts.isAsExpression(current) || ts.isSatisfiesExpression(current)) {
      current = current.expression;
    } else if (ts.isTypeAssertionExpression(current)) {
      current = current.expression;
    } else {
      break;
    }
  }
  return current;
}

// Every top-level `return` statement inside the hook's own body — i.e. not
// inside a nested function/method/class scope declared within the hook
// (helper closures, `useMemo`/`useEffect` callbacks, `async function` local
// helpers). Those have their own, unrelated returns and must never be
// conflated with the hook's own returned view.
function topLevelReturnStatements(hookFunctionNode) {
  const returns = [];
  function visit(node) {
    if (isNestedFunctionScope(node)) return;
    if (ts.isReturnStatement(node)) returns.push(node);
    ts.forEachChild(node, visit);
  }
  ts.forEachChild(hookFunctionNode.body, visit);
  return returns;
}

// Every top-level `VariableDeclaration` inside the hook's own body, same
// nested-scope exclusion as above.
function topLevelVariableDeclarations(hookFunctionNode) {
  const declarations = [];
  function visit(node) {
    if (isNestedFunctionScope(node)) return;
    if (ts.isVariableDeclaration(node)) declarations.push(node);
    ts.forEachChild(node, visit);
  }
  ts.forEachChild(hookFunctionNode.body, visit);
  return declarations;
}

// Recurse into an object literal (a hook's `return {...}`, or a nested
// object-literal property value such as `graph: {...}`), flagging any
// property whose value is an unstable ConditionalExpression.
function scanObjectLiteralForUnstableEmpties(objectLiteral, sourceFile, fieldPath, violations) {
  for (const property of objectLiteral.properties) {
    if (!ts.isPropertyAssignment(property)) continue; // shorthand/spread/method: not the `field: value` shape we judge
    const name = property.name.getText(sourceFile);
    const path = fieldPath ? `${fieldPath}.${name}` : name;
    const value = unwrapToExpression(property.initializer);
    if (ts.isConditionalExpression(value)) {
      if (conditionalHasUnstableEmptyBranch(value)) {
        violations.push({
          field: path,
          shape: "return 对象字面量字段",
        });
      }
    } else if (ts.isObjectLiteralExpression(value)) {
      scanObjectLiteralForUnstableEmpties(value, sourceFile, path, violations);
    }
  }
}

async function scanHook(fileName, functionName) {
  const sourceFile = await parseModule(fileName);
  const hookFunctionNode = findFunction(sourceFile, functionName);

  const violations = [];
  let returnObjectLiteralCount = 0;

  for (const returnStatement of topLevelReturnStatements(hookFunctionNode)) {
    const expression = returnStatement.expression && unwrapToExpression(returnStatement.expression);
    if (!expression || !ts.isObjectLiteralExpression(expression)) continue;
    returnObjectLiteralCount += 1;
    scanObjectLiteralForUnstableEmpties(expression, sourceFile, "", violations);
  }

  for (const declaration of topLevelVariableDeclarations(hookFunctionNode)) {
    if (!declaration.initializer) continue;
    const initializer = unwrapToExpression(declaration.initializer);
    if (ts.isConditionalExpression(initializer) && conditionalHasUnstableEmptyBranch(initializer)) {
      violations.push({
        field: declaration.name.getText(sourceFile),
        shape: "局部变量声明（先算成变量再放进 return）",
      });
    }
  }

  return { violations, returnObjectLiteralCount };
}

test("workspace hooks never hand back a bare [] / {} literal for the owner-hidden view", async () => {
  const offenders = [];
  let hookFunctionsScanned = 0;
  let totalReturnObjectLiterals = 0;

  for (const { file, name } of HOOK_FUNCTIONS) {
    const { violations, returnObjectLiteralCount } = await scanHook(file, name);
    hookFunctionsScanned += 1;
    totalReturnObjectLiterals += returnObjectLiteralCount;
    for (const violation of violations) {
      offenders.push(
        `frontend/app/${file} 字段 ${violation.field} —— ${violation.shape}中把不稳定的 `
        + "空字面量 [] / {} 当作 owner-hidden 回退值，每次渲染都是新引用，会让依赖它的 "
        + "effect/useMemo 每帧重跑（可能触发 setState-in-effect 死循环）。改用模块级 "
        + "Object.freeze(...) 常量。",
      );
    }
  }

  // Non-vacuous guard: if either count collapses to zero, the scan itself
  // has gone dark (wrong function names, parseModule failures swallowed,
  // etc.) and the assertion below would pass for the wrong reason.
  assert.equal(hookFunctionsScanned, HOOK_FUNCTIONS.length);
  assert.ok(
    totalReturnObjectLiterals > 0,
    "scanned zero return-object-literals across all seven hooks — the guard is not actually looking at anything",
  );

  assert.deepEqual(offenders, []);
});
