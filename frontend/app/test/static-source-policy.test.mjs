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
    && ["Array", "ReadonlyArray"].includes(type.typeName.text)
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

  function newScope(parent = undefined) {
    return { parent, bindings: new Map() };
  }

  function binding(scope, name) {
    for (let current = scope; current; current = current.parent) {
      if (current.bindings.has(name)) {
        return current.bindings.get(name);
      }
    }
    return undefined;
  }

  function assignBinding(scope, name, value) {
    for (let current = scope; current; current = current.parent) {
      if (current.bindings.has(name)) {
        current.bindings.set(name, value);
        return;
      }
    }
    scope.bindings.set(name, value);
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
      && ["readFile", "readFileSync"].includes(current.expression.text)
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
      const resolved = binding(scope, current.text);
      return resolved === undefined
        ? SOURCE_INPUT_NAME.test(current.text)
        : resolved;
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
      && current.operatorToken.kind === ts.SyntaxKind.PlusToken
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
      const functionScope = newScope(scope);
      for (const parameter of node.parameters) {
        if (ts.isIdentifier(parameter.name)) {
          functionScope.bindings.set(
            parameter.name.text,
            parameterIsTextFlow(parameter),
          );
        }
        if (parameter.initializer) {
          visit(parameter.initializer, scope);
        }
      }
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
    if (
      ts.isVariableDeclaration(node)
      && ts.isIdentifier(node.name)
    ) {
      const value = node.initializer
        ? isTextFlow(node.initializer, scope)
        : false;
      if (node.initializer) visit(node.initializer, scope);
      scope.bindings.set(node.name.text, value);
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

  visit(sourceFile, newScope());
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
