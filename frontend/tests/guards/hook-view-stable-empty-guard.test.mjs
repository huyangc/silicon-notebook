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
// — or, when the fallback can't be shared module-wide (a mutable `Set`/
// `Map` can't be frozen shut against a later `.add`/`.delete` call, so
// sharing one instance across every hook instance/actor would widen a stray
// write's blast radius to the whole module), a per-hook-instance
// `useRef(new Set())` — and reference it from the ternary's hidden-state
// branch instead of the bare literal. See use-ask-session.ts's `NO_TURNS`
// (module constant) and use-report-workspace.ts's `hiddenSelectedIdsRef`
// (per-instance ref).
//
// Judgement is semantic (AST) — no line numbers, matching this repo's
// static-source-policy guard (`tests/guards/static-source-policy.test.mjs`),
// which independently forbids test code from querying AST node positions at
// all (`.getStart()`/`.getEnd()`/`.getFullStart()`). Offenders are reported
// as `<file> 字段 <dotted field path>` instead. Walk each hook function's
// *own* body — never descending into a nested function/method/class scope,
// so a helper closure's unrelated `return [];` or a locally-scoped ternary
// inside a callback can never be mistaken for part of the hook's own
// returned view — and flag any `ConditionalExpression` whose `whenTrue`/
// `whenFalse` branch, after (a) unwrapping parens/`as`/`satisfies`/
// type-assertion wrappers and (b) — recursively — descending into the right
// operand of a `??`/`||` binary expression (`visible ? (items ?? []) :
// NO_ITEMS`'s *live* branch still hands back a fresh `[]` whenever `items`
// is nullish, so the instability survives being wrapped in a fallback
// operator), is a zero-element `ArrayLiteralExpression`, a zero-property
// `ObjectLiteralExpression`, or a zero-argument `new Set()` / `new Map()`
// (same instability, different spelling), when that conditional sits:
//   (a) directly as a property value inside an object literal reachable
//       from the hook's own `return {...}` — the return object literal
//       itself, a nested object-literal property value
//       (use-kg-workspace.ts nests `graph: { searchHits: … }` etc.), or a
//       top-level local variable whose (unwrapped) initializer is itself an
//       object literal that later flows into the return (the "compute a
//       local object, then `graph: graphView` it into return" shape); or
//   (b) directly as a top-level `VariableDeclaration` initializer anywhere
//       in the hook's own body (the "compute a local, then return it"
//       shape).
//
// A frozen `Object.freeze([] as T[]) as T[]` fallback constant is not itself
// an ArrayLiteralExpression/ObjectLiteralExpression in the ternary branch —
// it is an Identifier reference — so the fix makes this guard pass.
//
// Hook file discovery is a glob over `frontend/app/use-*.ts`, not a fixed
// list — a new hook file that nobody remembered to add to a static roster
// used to scan silently went uncovered. Every discovered file must be
// explicitly accounted for: either scanned (its exported `use...` function
// is found and walked), or named in `NOT_A_VIEW_OWNER` with a reason it
// doesn't own a notebook/actor-scoped view at all. Per scanned hook,
// `scannedFields` counts the `field: value` properties actually visited;
// landing on zero is itself suspicious (either the scan went dark, or the
// hook's own `return {...}` is genuinely all-shorthand and has nothing to
// check) — the latter is tracked explicitly via `EXPECTED_NO_VIEW_FIELDS`
// so a hook can't silently regress to zero scanned fields without this
// guard noticing.
import test from "node:test";
import assert from "node:assert/strict";
import ts from "typescript";

import { appSourceModules, findFunction, parseModule } from "../../test-support/semantic-source.mjs";

// Hook files that intentionally do not own a notebook/actor-scoped "view"
// object at all, and are therefore out of scope for this guard entirely.
// Every entry must carry a reason — this is an explicit acknowledgement,
// not a place to silently opt a hook out of coverage.
const NOT_A_VIEW_OWNER = new Map([
  ["use-floating-window.ts", "拖动窗口几何状态，不持有 notebook/actor 视图"],
]);

// Hooks that DO own a view, but whose own `return {...}` object literal is
// entirely shorthand properties (`{ x, y }`, no `field: value` pairs) — so
// `scannedFields` legitimately lands on zero for this hook. Each entry
// exists so a hook that *should* have scannable fields can't quietly
// regress to zero without this guard noticing (see the per-file assertion
// below); it is not a way to suppress the check.
const EXPECTED_NO_VIEW_FIELDS = new Set(["use-root-modal-coordinator.ts"]);

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

// A ternary branch is unstable if it *is* a bare empty literal once
// unwrapped, or — recursively — if it's a `??`/`||` fallback whose right
// operand is. `items ?? []`/`items || []` is stable exactly when `items`
// itself is stable; the moment `items` is nullish/falsy, the expression
// evaluates to a fresh `[]` every render, so the branch is unstable
// whenever its *fallback* side is a bare empty literal — regardless of how
// stable the left side happens to be.
function isUnstableEmptyBranch(node) {
  const expression = unwrapToExpression(node);
  if (isUnstableEmptyLiteral(expression)) return true;
  if (
    ts.isBinaryExpression(expression)
    && (
      expression.operatorToken.kind === ts.SyntaxKind.QuestionQuestionToken
      || expression.operatorToken.kind === ts.SyntaxKind.BarBarToken
    )
  ) {
    return isUnstableEmptyBranch(expression.right);
  }
  return false;
}

// Either branch being unstable is unstable — we don't assume which side of
// the ternary is "the hidden-state fallback" vs. "the live data"; both
// existing incidents put the bare literal on the `whenFalse` side, but
// judging structurally (not by branch position) is what makes this a
// semantic guard rather than a re-encoding of today's line list.
function conditionalHasUnstableEmptyBranch(node) {
  return isUnstableEmptyBranch(node.whenTrue) || isUnstableEmptyBranch(node.whenFalse);
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

// Recurse into an object literal (a hook's `return {...}`, a nested
// object-literal property value such as `graph: {...}`, or a top-level
// local variable's object-literal initializer that later flows into the
// return), flagging any property whose value is an unstable
// ConditionalExpression and counting every `field: value` property visited
// along the way (`stats.scannedFields`) regardless of whether it turns out
// to be a violation — that count is what makes a scan that silently sees
// nothing detectable (see the per-file assertion in the test body).
function scanObjectLiteralForUnstableEmpties(
  objectLiteral,
  sourceFile,
  fieldPath,
  violations,
  stats,
  shapeLabel,
) {
  for (const property of objectLiteral.properties) {
    if (!ts.isPropertyAssignment(property)) continue; // shorthand/spread/method: not the `field: value` shape we judge
    stats.scannedFields += 1;
    const name = property.name.getText(sourceFile);
    const path = fieldPath ? `${fieldPath}.${name}` : name;
    const value = unwrapToExpression(property.initializer);
    if (ts.isConditionalExpression(value)) {
      if (conditionalHasUnstableEmptyBranch(value)) {
        violations.push({
          field: path,
          shape: shapeLabel,
        });
      }
    } else if (ts.isObjectLiteralExpression(value)) {
      scanObjectLiteralForUnstableEmpties(value, sourceFile, path, violations, stats, shapeLabel);
    }
  }
}

async function scanHook(fileName, functionName) {
  const sourceFile = await parseModule(fileName);
  const hookFunctionNode = findFunction(sourceFile, functionName);

  const violations = [];
  const stats = { scannedFields: 0, localDeclarations: 0 };
  let returnObjectLiteralCount = 0;

  for (const returnStatement of topLevelReturnStatements(hookFunctionNode)) {
    const expression = returnStatement.expression && unwrapToExpression(returnStatement.expression);
    if (!expression || !ts.isObjectLiteralExpression(expression)) continue;
    returnObjectLiteralCount += 1;
    scanObjectLiteralForUnstableEmpties(
      expression,
      sourceFile,
      "",
      violations,
      stats,
      "return 对象字面量字段",
    );
  }

  for (const declaration of topLevelVariableDeclarations(hookFunctionNode)) {
    if (!declaration.initializer) continue;
    stats.localDeclarations += 1;
    const initializer = unwrapToExpression(declaration.initializer);
    const name = declaration.name.getText(sourceFile);
    if (ts.isConditionalExpression(initializer)) {
      if (conditionalHasUnstableEmptyBranch(initializer)) {
        violations.push({
          field: name,
          shape: "局部变量声明（先算成变量再放进 return）",
        });
      }
    } else if (ts.isObjectLiteralExpression(initializer)) {
      // "先算成局部对象再 `graph: graphView` 返回" shape: the local
      // variable itself is an object literal (not a ternary) whose
      // *properties* may still hide an unstable ternary.
      scanObjectLiteralForUnstableEmpties(
        initializer,
        sourceFile,
        name,
        violations,
        stats,
        "局部变量声明中的对象字面量字段（先算成局部对象再放进 return）",
      );
    }
  }

  return { violations, returnObjectLiteralCount, ...stats };
}

// A top-level `FunctionDeclaration` named `use...` and exported — the shape
// every hook file in `frontend/app/` uses (`export function useFoo(...) {`).
function exportedHookFunctionName(sourceFile) {
  for (const statement of sourceFile.statements) {
    if (
      ts.isFunctionDeclaration(statement)
      && statement.name
      && /^use[A-Z]/.test(statement.name.text)
      && (statement.modifiers ?? []).some(
        (modifier) => modifier.kind === ts.SyntaxKind.ExportKeyword,
      )
    ) {
      return statement.name.text;
    }
  }
  return undefined;
}

// Glob `frontend/app/use-*.ts(x)` — deliberately not a static roster, so a
// newly added hook file is automatically swept into scanning (or, if it
// can't be classified at all, fails loudly instead of being silently
// skipped). `appSourceModules()` already excludes `.test.`/`.d.ts` files and
// walks both `app/` (no path prefix) and `features/` (prefixed); filtering
// for `use-*.ts(x)` with no `/` in the path keeps this to top-level
// `frontend/app/` hook files only, matching every hook file that exists
// today (`find app -iname 'use-*.ts*'` turns up none nested or under
// `features/`).
async function discoverHookFunctions() {
  const modules = await appSourceModules();
  const hookModules = modules.filter((entry) => /^use-[^/]+\.tsx?$/.test(entry.path));
  const discoveredFiles = new Set(hookModules.map((entry) => entry.path));

  for (const excludedFile of NOT_A_VIEW_OWNER.keys()) {
    if (!discoveredFiles.has(excludedFile)) {
      throw new Error(
        `NOT_A_VIEW_OWNER 登记了 frontend/app/${excludedFile}，但该文件已不存在 —— 清理这条陈旧的排除项。`,
      );
    }
  }

  const hooks = [];
  for (const { path: file, module: sourceFile } of hookModules) {
    if (NOT_A_VIEW_OWNER.has(file)) continue; // explicitly registered non-view-owner, see the map above
    const name = exportedHookFunctionName(sourceFile);
    if (!name) {
      // Response to an unclassifiable new hook file must be loud, not a
      // silent gap in coverage: either it exports a `use...` function (and
      // gets scanned automatically) or it must be explicitly registered in
      // NOT_A_VIEW_OWNER with a reason.
      throw new Error(
        `frontend/app/${file}: 未找到顶层 \`export function use...\` —— 新发现的 hook 文件必须能`
        + "被本守卫识别出它导出的 hook 函数名，否则须显式登记进 NOT_A_VIEW_OWNER 并写明理由。",
      );
    }
    hooks.push({ file, name });
  }
  return hooks;
}

test("workspace hooks never hand back a bare [] / {} literal for the owner-hidden view", async () => {
  const hookFunctions = await discoverHookFunctions();
  const offenders = [];
  let hookFunctionsScanned = 0;
  let totalReturnObjectLiterals = 0;
  let totalLocalDeclarations = 0;

  for (const { file, name } of hookFunctions) {
    const { violations, returnObjectLiteralCount, scannedFields, localDeclarations } =
      await scanHook(file, name);
    hookFunctionsScanned += 1;
    totalReturnObjectLiterals += returnObjectLiteralCount;
    totalLocalDeclarations += localDeclarations;

    if (EXPECTED_NO_VIEW_FIELDS.has(file)) {
      // Registered exception (see the map's comment): this hook's own
      // `return {...}` is entirely shorthand properties, so zero scanned
      // fields is expected here, not evidence the scan went dark.
    } else {
      assert.ok(
        scannedFields > 0,
        `frontend/app/${file}: scannedFields 为 0 —— 要么本守卫对这个 hook 的扫描已经失效`
        + "（没有真正看到任何 return 字段），要么这个 hook 的 return 确实全是 shorthand 属性、"
        + "没有可判断的 `field: value` 字段，此时须显式登记进 EXPECTED_NO_VIEW_FIELDS 并写明理由。",
      );
    }

    for (const violation of violations) {
      offenders.push(
        `frontend/app/${file} 字段 ${violation.field} —— ${violation.shape}中把不稳定的 `
        + "空字面量 [] / {} 当作 owner-hidden 回退值，每次渲染都是新引用，会让依赖它的 "
        + "effect/useMemo 每帧重跑（可能触发 setState-in-effect 死循环）。改用模块级 "
        + "Object.freeze(...) 常量，或（当回退值不可冻结、例如 Set/Map 时）per-instance "
        + "useRef。",
      );
    }
  }

  // Non-vacuous guard: if any of these counts collapse to zero, the scan
  // itself has gone dark (discovery finding nothing, wrong function names,
  // parseModule failures swallowed, etc.) and the assertions above would
  // pass for the wrong reason.
  assert.ok(hookFunctionsScanned > 0, "发现零个 hook 文件可扫描 —— 文件发现逻辑本身失效了");
  assert.ok(
    totalReturnObjectLiterals > 0,
    "scanned zero return-object-literals across all hooks — the guard is not actually looking at anything",
  );
  assert.ok(
    totalLocalDeclarations > 0,
    "scanned zero top-level local variable declarations across all hooks — the "
    + "local-declaration scan path is not actually looking at anything",
  );

  assert.deepEqual(offenders, []);
});
