import test from "node:test";
import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

import { STATIC_SOURCE_CONTRACTS } from "./static-source-contracts.mjs";


const APP_DIR = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const DIRECT_READ_ALLOWLIST = new Set([
  // Reads committed cross-language golden data, never production source text.
  "knowhow-normalize.test.mjs",
  "test/semantic-source.mjs",
  "test/static-source-policy.test.mjs",
  "test-runner-config.test.mjs",
]);
const POSITION_QUERY_ALLOWLIST = new Set([
  // Contains mutation examples for this policy; it does not inspect production.
  "test/static-source-policy.test.mjs",
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


async function sourceModules(directory = APP_DIR) {
  const entries = await readdir(directory, { withFileTypes: true });
  const modules = [];
  for (const entry of entries) {
    const absolute = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      modules.push(...await sourceModules(absolute));
    } else if (isSourceModule(
      path.relative(APP_DIR, absolute).replaceAll(path.sep, "/"),
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
      ts.isCallExpression(node)
      && node.expression.kind === ts.SyntaxKind.ImportKeyword
      && node.arguments.length === 1
      && ts.isStringLiteralLike(node.arguments[0])
    ) {
      const resolved = resolve(node.arguments[0].text);
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
      || relative.startsWith("test/")
      || (
        testReachable.has(relative)
        && !productionReachable.has(relative)
      )
    )),
  );
}


async function policyModules() {
  const sources = new Map();
  for (const absolute of await sourceModules()) {
    const relative = path.relative(APP_DIR, absolute).replaceAll(path.sep, "/");
    sources.set(relative, await readFile(absolute, "utf8"));
  }
  return [...policyRelativeModules(sources)]
    .sort()
    .map((relative) => path.join(APP_DIR, relative));
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


function hasPositionOrOrderQuery(relative, source) {
  if (POSITION_QUERY_ALLOWLIST.has(relative)) return false;
  const sourceFile = ts.createSourceFile(
    relative,
    source,
    ts.ScriptTarget.Latest,
    true,
    scriptKind(relative),
  );
  let found = false;
  const SOURCE_INPUT_NAME = /^(?:source|sourceText|productionSource|content|text|payload)$/i;
  const FILE_READ_EXPORTS = new Set(["readFile", "readFileSync"]);
  const fileReadCallees = new Set(FILE_READ_EXPORTS);
  let nextBindingId = 1;

  for (const statement of sourceFile.statements) {
    if (
      !ts.isImportDeclaration(statement)
      || !ts.isStringLiteral(statement.moduleSpecifier)
      || !/^node:fs(?:\/promises)?$/.test(statement.moduleSpecifier.text)
      || !statement.importClause
      || !statement.importClause.namedBindings
      || !ts.isNamedImports(statement.importClause.namedBindings)
    ) {
      continue;
    }
    for (const element of statement.importClause.namedBindings.elements) {
      const imported = element.propertyName?.text ?? element.name.text;
      if (FILE_READ_EXPORTS.has(imported)) {
        fileReadCallees.add(element.name.text);
      }
    }
  }

  function newScope(parent = undefined, kind = "block") {
    return { parent, kind, bindings: new Map() };
  }

  function newBinding(value) {
    return { id: nextBindingId++, value };
  }

  function resolvedBinding(scope, name) {
    for (let current = scope; current; current = current.parent) {
      if (current.bindings.has(name)) {
        return current.bindings.get(name);
      }
    }
    return undefined;
  }

  function defineBinding(scope, name, value) {
    const existing = scope.bindings.get(name);
    if (existing) {
      existing.value = value;
      return existing;
    }
    const created = newBinding(value);
    scope.bindings.set(name, created);
    return created;
  }

  function assignBinding(scope, name, value) {
    const existing = resolvedBinding(scope, name);
    if (existing) {
      existing.value = value;
    } else {
      defineBinding(scope, name, value);
    }
  }

  function nearestFunctionScope(scope) {
    let current = scope;
    while (current.parent && current.kind !== "function") {
      current = current.parent;
    }
    return current;
  }

  function cloneScope(scope) {
    const chain = [];
    for (let current = scope; current; current = current.parent) {
      chain.push(current);
    }
    let clonedParent;
    for (const original of chain.reverse()) {
      const cloned = newScope(clonedParent, original.kind);
      for (const [name, entry] of original.bindings) {
        cloned.bindings.set(
          name,
          { id: entry.id, value: entry.value },
        );
      }
      clonedParent = cloned;
    }
    return clonedParent;
  }

  function bindingStates(scope) {
    const states = new Map();
    for (let current = scope; current; current = current.parent) {
      for (const entry of current.bindings.values()) {
        if (!states.has(entry.id)) states.set(entry.id, entry.value);
      }
    }
    return states;
  }

  function joinScopes(target, branches) {
    const branchStates = branches.map(bindingStates);
    for (let current = target; current; current = current.parent) {
      for (const entry of current.bindings.values()) {
        entry.value = branchStates.some(
          (states) => states.get(entry.id) ?? entry.value,
        );
      }
    }
  }

  function collectFunctionVarNames(node) {
    const names = new Set();
    function collect(current, root = false) {
      if (!root && ts.isFunctionLike(current)) return;
      if (
        ts.isVariableDeclarationList(current)
        && !(current.flags & ts.NodeFlags.BlockScoped)
      ) {
        for (const declaration of current.declarations) {
          if (ts.isIdentifier(declaration.name)) {
            names.add(declaration.name.text);
          }
        }
      }
      ts.forEachChild(current, (child) => collect(child));
    }
    collect(node, true);
    return names;
  }

  function predeclareFunctionVars(node, scope) {
    for (const name of collectFunctionVarNames(node)) {
      if (!scope.bindings.has(name)) {
        scope.bindings.set(name, newBinding(false));
      }
    }
  }

  function unwrapExpression(expression) {
    let current = expression;
    while (
      ts.isAwaitExpression(current)
      || ts.isParenthesizedExpression(current)
      || ts.isAsExpression(current)
      || ts.isTypeAssertionExpression(current)
      || ts.isNonNullExpression(current)
    ) {
      current = current.expression;
    }
    return current;
  }

  function isFileRead(expression) {
    const current = unwrapExpression(expression);
    if (!ts.isCallExpression(current)) return false;
    if (
      ts.isIdentifier(current.expression)
      && fileReadCallees.has(current.expression.text)
    ) {
      return true;
    }
    return (
      ts.isPropertyAccessExpression(current.expression)
      && ["readFile", "readFileSync"].includes(current.expression.name.text)
    );
  }

  function isTextFlow(expression, scope) {
    const current = unwrapExpression(expression);
    if (ts.isIdentifier(current)) {
      const resolved = resolvedBinding(scope, current.text);
      return resolved === undefined
        ? SOURCE_INPUT_NAME.test(current.text)
        : resolved.value;
    }
    if (isFileRead(current)) return true;
    if (ts.isConditionalExpression(current)) {
      return (
        isTextFlow(current.whenTrue, scope)
        || isTextFlow(current.whenFalse, scope)
      );
    }
    return (
      ts.isBinaryExpression(current)
      && [
        ts.SyntaxKind.PlusToken,
        ts.SyntaxKind.AmpersandAmpersandToken,
        ts.SyntaxKind.BarBarToken,
        ts.SyntaxKind.QuestionQuestionToken,
      ].includes(current.operatorToken.kind)
      && (
        isTextFlow(current.left, scope)
        || isTextFlow(current.right, scope)
      )
    );
  }

  function parameterIsTextFlow(parameter) {
    if (
      (
        parameter.initializer
        && ts.isArrayLiteralExpression(parameter.initializer)
      )
      || isDefinitelyArrayType(parameter.type)
    ) {
      return false;
    }
    return (
      ts.isIdentifier(parameter.name)
      && SOURCE_INPUT_NAME.test(parameter.name.text)
    );
  }

  function visit(node, scope) {
    if (found) return;
    if (ts.isFunctionLike(node)) {
      const functionScope = newScope(scope, "function");
      for (const parameter of node.parameters) {
        if (ts.isIdentifier(parameter.name)) {
          defineBinding(
            functionScope,
            parameter.name.text,
            parameterIsTextFlow(parameter),
          );
        }
        if (parameter.initializer) {
          visit(parameter.initializer, scope);
        }
      }
      if (node.body) predeclareFunctionVars(node.body, functionScope);
      if (node.body) visit(node.body, functionScope);
      return;
    }
    if (ts.isBlock(node)) {
      const blockScope = newScope(scope);
      for (const statement of node.statements) {
        visit(statement, blockScope);
        if (found) return;
      }
      return;
    }
    if (ts.isCatchClause(node)) {
      const catchScope = newScope(scope);
      if (
        node.variableDeclaration
        && ts.isIdentifier(node.variableDeclaration.name)
      ) {
        defineBinding(
          catchScope,
          node.variableDeclaration.name.text,
          false,
        );
      }
      visit(node.block, catchScope);
      return;
    }
    if (ts.isIfStatement(node)) {
      visit(node.expression, scope);
      const thenScope = cloneScope(scope);
      visit(node.thenStatement, thenScope);
      const elseScope = cloneScope(scope);
      if (node.elseStatement) visit(node.elseStatement, elseScope);
      joinScopes(scope, [thenScope, elseScope]);
      return;
    }
    if (ts.isTryStatement(node)) {
      const tryScope = cloneScope(scope);
      visit(node.tryBlock, tryScope);
      const branches = [tryScope];
      if (node.catchClause) {
        const catchScope = cloneScope(scope);
        visit(node.catchClause, catchScope);
        branches.push(catchScope);
      }
      joinScopes(scope, branches);
      if (node.finallyBlock) visit(node.finallyBlock, scope);
      return;
    }
    if (
      ts.isWhileStatement(node)
      || ts.isDoStatement(node)
    ) {
      visit(node.expression, scope);
      const baseline = cloneScope(scope);
      const iteration = cloneScope(scope);
      visit(node.statement, iteration);
      joinScopes(scope, [baseline, iteration]);
      return;
    }
    if (ts.isForStatement(node)) {
      const loopScope = newScope(scope);
      if (node.initializer) visit(node.initializer, loopScope);
      if (node.condition) visit(node.condition, loopScope);
      const baseline = cloneScope(loopScope);
      const iteration = cloneScope(loopScope);
      visit(node.statement, iteration);
      if (node.incrementor) visit(node.incrementor, iteration);
      joinScopes(loopScope, [baseline, iteration]);
      return;
    }
    if (
      ts.isForInStatement(node)
      || ts.isForOfStatement(node)
    ) {
      const loopScope = newScope(scope);
      visit(node.expression, loopScope);
      visit(node.initializer, loopScope);
      const baseline = cloneScope(loopScope);
      const iteration = cloneScope(loopScope);
      visit(node.statement, iteration);
      joinScopes(loopScope, [baseline, iteration]);
      return;
    }
    if (ts.isSwitchStatement(node)) {
      visit(node.expression, scope);
      const branches = [cloneScope(scope)];
      for (const clause of node.caseBlock.clauses) {
        const branch = cloneScope(scope);
        if (ts.isCaseClause(clause)) visit(clause.expression, branch);
        for (const statement of clause.statements) {
          visit(statement, branch);
        }
        branches.push(branch);
      }
      joinScopes(scope, branches);
      return;
    }
    if (
      ts.isVariableDeclaration(node)
      && ts.isIdentifier(node.name)
    ) {
      const value = node.initializer
        ? isTextFlow(node.initializer, scope)
        : false;
      if (node.initializer) visit(node.initializer, scope);
      const declarationList = node.parent;
      const declarationScope = (
        ts.isVariableDeclarationList(declarationList)
        && !(declarationList.flags & ts.NodeFlags.BlockScoped)
      )
        ? nearestFunctionScope(scope)
        : scope;
      if (
        ts.isVariableDeclarationList(declarationList)
        && !(declarationList.flags & ts.NodeFlags.BlockScoped)
      ) {
        if (node.initializer) {
          assignBinding(declarationScope, node.name.text, value);
        }
      } else {
        defineBinding(declarationScope, node.name.text, value);
      }
      return;
    }
    if (
      ts.isBinaryExpression(node)
      && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
      && ts.isIdentifier(node.left)
    ) {
      const value = isTextFlow(node.right, scope);
      visit(node.right, scope);
      assignBinding(scope, node.left.text, value);
      return;
    }
    if (
      ts.isPropertyAccessExpression(node)
      && ["pos", "end"].includes(node.name.text)
    ) {
      found = true;
      return;
    }
    if (
      ts.isElementAccessExpression(node)
      && ts.isPropertyAccessExpression(node.expression)
      && ["statements", "members", "properties"].includes(
        node.expression.name.text,
      )
    ) {
      found = true;
      return;
    }
    if (
      (
        ts.isVariableDeclaration(node)
        && ts.isArrayBindingPattern(node.name)
        && node.initializer
        && ts.isPropertyAccessExpression(node.initializer)
        && ["statements", "members", "properties"].includes(
          node.initializer.name.text,
        )
      )
      || (
        ts.isBinaryExpression(node)
        && ts.isArrayLiteralExpression(node.left)
        && ts.isPropertyAccessExpression(node.right)
        && ["statements", "members", "properties"].includes(
          node.right.name.text,
        )
      )
    ) {
      found = true;
      return;
    }
    if (
      ts.isCallExpression(node)
      && ts.isPropertyAccessExpression(node.expression)
    ) {
      const method = node.expression.name.text;
      if (["getStart", "getFullStart", "getEnd"].includes(method)) {
        found = true;
        return;
      }
      if (
        method === "at"
        && ts.isPropertyAccessExpression(node.expression.expression)
        && ["statements", "members", "properties"].includes(
          node.expression.expression.name.text,
        )
      ) {
        found = true;
        return;
      }
      if (
        ["indexOf", "slice", "substring"].includes(method)
        && isTextFlow(node.expression.expression, scope)
      ) {
        found = true;
        return;
      }
    }
    ts.forEachChild(node, (child) => visit(child, scope));
  }

  const rootScope = newScope(undefined, "function");
  predeclareFunctionVars(sourceFile, rootScope);
  visit(sourceFile, rootScope);
  return found;
}


function modulePolicyOffenders(relative, source) {
  const offenders = [];
  const readsFiles = (
    /from\s+["']node:fs(?:\/promises)?["']/.test(source)
    || /import\(\s*["']node:fs(?:\/promises)?["']\s*\)/.test(source)
  );
  if (readsFiles && !DIRECT_READ_ALLOWLIST.has(relative)) {
    offenders.push(`${relative}: direct production-source read`);
  }
  if (
    hasPositionOrOrderQuery(relative, source)
  ) {
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
    const relative = path.relative(APP_DIR, absolute).replaceAll(path.sep, "/");
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


test("source policy rejects position identities without banning ordinary arrays", () => {
  assert.deepEqual(
    modulePolicyOffenders(
      "example.test.mjs",
      "const start = node.getStart();",
    ),
    ["example.test.mjs: source-position or source-order query"],
  );
  for (const mutation of [
    "const start = node.getFullStart();",
    "const finish = node.end;",
    "const first = sourceFile.statements[0];",
    "const first = sourceFile.members.at(0);",
    "const [first] = sourceFile.statements;",
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
      "test/source-helper.mjs",
      "export function fragment(payload) { return payload.slice(1); }",
    ),
    ["test/source-helper.mjs: source-position or source-order query"],
  );
  assert.deepEqual(
    modulePolicyOffenders(
      "test/array-helper.ts",
      "export function clone(values: string[]) { return values.slice(); }",
    ),
    [],
  );
  assert.deepEqual(
    modulePolicyOffenders(
      "test/local-array-helper.mjs",
      "export function clone() { const values = [1]; return values.slice(); }",
    ),
    [],
  );
  assert.deepEqual(
    modulePolicyOffenders(
      "test/readonly-array-helper.ts",
      "export function clone(values: readonly string[]) {"
        + " return values.slice(); }",
    ),
    [],
  );
  assert.deepEqual(
    modulePolicyOffenders(
      "test/scoped-array-helper.ts",
      "function fragment(payload: string) { return payload.slice(1); }\n"
        + "function clone(payload: string[]) { return payload.slice(); }",
    ),
    ["test/scoped-array-helper.ts: source-position or source-order query"],
  );
  assert.deepEqual(
    modulePolicyOffenders(
      "test/unpoisoned-array-helper.ts",
      "function inspect(payload: string) { return payload.length; }\n"
        + "function clone(payload: string[]) { return payload.slice(); }",
    ),
    [],
  );
  assert.deepEqual(
    modulePolicyOffenders(
      "test/ordinary-string-helper.ts",
      "function trimPrefix(value: string) { return value.slice(1); }",
    ),
    [],
  );
  assert.deepEqual(
    modulePolicyOffenders(
      "test/typed-array-helper.ts",
      "function clone(payload: Uint8Array) { return payload.slice(); }",
    ),
    [],
  );
  assert.deepEqual(
    modulePolicyOffenders(
      "example.test.mjs",
      "const copy = values.slice().sort();",
    ),
    [],
  );
  assert.deepEqual(
    modulePolicyOffenders(
      "feature.test.mjs",
      "function fragment(payload) { return payload.slice(1); }",
    ),
    ["feature.test.mjs: source-position or source-order query"],
  );
  assert.deepEqual(
    modulePolicyOffenders(
      "test/semantic-source.mjs",
      "import { readFile } from 'node:fs/promises';\n"
        + "const fragment = (await readFile(path, 'utf8')).slice(1);",
    ),
    ["test/semantic-source.mjs: source-position or source-order query"],
  );
  assert.deepEqual(
    modulePolicyOffenders(
      "test/semantic-source.mjs",
      "import fs from 'node:fs';\n"
        + "let payload;\npayload = fs.readFileSync(path, 'utf8');\n"
        + "payload.substring(1);",
    ),
    ["test/semantic-source.mjs: source-position or source-order query"],
  );
  for (const mutation of [
    "import { readFileSync } from 'node:fs';\n"
      + "let data = readFileSync(path, 'utf8');\n"
      + "if (flag) { data = []; }\ndata.slice(1);",
    "import { readFileSync } from 'node:fs';\n"
      + "let data;\nif (flag) { data = readFileSync(path, 'utf8'); }"
      + " else { data = []; }\ndata.slice(1);",
    "import { readFileSync } from 'node:fs';\n"
      + "let data = [];\nwhile (flag) {"
      + " data = readFileSync(path, 'utf8'); }\ndata.slice(1);",
    "import { readFileSync } from 'node:fs';\n"
      + "let data;\ntry { data = readFileSync(path, 'utf8'); }"
      + " catch { data = []; }\ndata.slice(1);",
    "import { readFileSync } from 'node:fs';\n"
      + "if (flag) { var data = readFileSync(path, 'utf8'); }\n"
      + "data.slice(1);",
    "import { readFileSync } from 'node:fs';\n"
      + "let data = readFileSync(path, 'utf8');\n"
      + "try {} catch (data) {}\ndata.slice(1);",
    "import { readFile as load } from 'node:fs/promises';\n"
      + "const data = await load(path, 'utf8');\ndata.slice(1);",
  ]) {
    assert.deepEqual(
      modulePolicyOffenders("test/semantic-source.mjs", mutation),
      ["test/semantic-source.mjs: source-position or source-order query"],
      mutation,
    );
  }
  assert.deepEqual(
    modulePolicyOffenders(
      "test/semantic-source.mjs",
      "import { readFileSync } from 'node:fs';\n"
        + "let data = readFileSync(path, 'utf8');\n"
        + "data = [];\ndata.slice(1);",
    ),
    [],
  );
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
});


test("source policy includes semantic helper modules, not only test entrypoints", async () => {
  const relative = (await policyModules()).map(
    (absolute) => path.relative(APP_DIR, absolute).replaceAll(path.sep, "/"),
  );
  assert.ok(relative.includes("test/semantic-source.mjs"));
  assert.ok(relative.includes("test/setup.ts"));
});


test("frontend tests do not inspect production source layout directly", async () => {
  assert.deepEqual(await policyOffenders(), []);
});
