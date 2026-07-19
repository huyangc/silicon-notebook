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
  // Central semantic AST helper: callers consume nodes, never text positions.
  "test/semantic-source.mjs",
  // Contains this policy's mutation strings, not production source inspection.
  "test/static-source-policy.test.mjs",
  // Reads package/config metadata only.
  "test-runner-config.test.mjs",
]);
const POSITION_QUERY_ALLOWLIST = new Set([
  "test/static-source-policy.test.mjs",
]);
const STRICT_TEXT_READER_ALLOWLIST = new Set([
  // This helper owns production source text and must expose only AST semantics.
  "test/semantic-source.mjs",
]);
const SOURCE_INPUT_NAME = (
  /^(?:source|sourceText|productionSource|content|text|payload)$/i
);
const TEXT_POSITION_METHODS = new Set([
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
      ts.isCallExpression(node)
      && node.expression.kind === ts.SyntaxKind.ImportKeyword
      && node.arguments.length === 1
      && ts.isStringLiteralLike(node.arguments[0])
      && isFsSpecifier(node.arguments[0].text)
    ) {
      readsFiles = true;
      return;
    }
    if (
      ts.isCallExpression(node)
      && ts.isIdentifier(node.expression)
      && node.expression.text === "require"
      && node.arguments.length === 1
      && ts.isStringLiteralLike(node.arguments[0])
      && isFsSpecifier(node.arguments[0].text)
    ) {
      readsFiles = true;
      return;
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return readsFiles;
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
  const readsFiles = moduleReadsFiles(sourceFile);
  const registeredReader = (
    readsFiles && STRICT_TEXT_READER_ALLOWLIST.has(relative)
  );
  const arrayBindings = new Set();
  let found = false;

  function recordArrayBinding(node) {
    if (
      (
        ts.isParameter(node)
        || ts.isVariableDeclaration(node)
      )
      && ts.isIdentifier(node.name)
      && (
        isDefinitelyArrayType(node.type)
        || (
          ts.isVariableDeclaration(node)
          && node.initializer
          && ts.isArrayLiteralExpression(node.initializer)
        )
      )
    ) {
      arrayBindings.add(node.name.text);
    }
    ts.forEachChild(node, recordArrayBinding);
  }
  recordArrayBinding(sourceFile);

  function visit(node) {
    if (found) return;
    if (
      ts.isPropertyAccessExpression(node)
      && ["pos", "end"].includes(node.name.text)
    ) {
      found = true;
      return;
    }
    if (
      ts.isPropertyAccessExpression(node)
      && node.name.text === "length"
      && ts.isIdentifier(node.expression)
      && SOURCE_INPUT_NAME.test(node.expression.text)
      && !arrayBindings.has(node.expression.text)
    ) {
      found = true;
      return;
    }
    if (
      ts.isElementAccessExpression(node)
      && ts.isIdentifier(node.expression)
      && SOURCE_INPUT_NAME.test(node.expression.text)
      && !arrayBindings.has(node.expression.text)
    ) {
      found = true;
      return;
    }
    if (
      ts.isElementAccessExpression(node)
      && ts.isPropertyAccessExpression(node.expression)
      && AST_COLLECTIONS.has(node.expression.name.text)
    ) {
      found = true;
      return;
    }
    if (
      ts.isVariableDeclaration(node)
      && ts.isArrayBindingPattern(node.name)
      && node.initializer
      && ts.isPropertyAccessExpression(node.initializer)
      && AST_COLLECTIONS.has(node.initializer.name.text)
    ) {
      found = true;
      return;
    }
    if (
      ts.isBinaryExpression(node)
      && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
      && ts.isArrayLiteralExpression(node.left)
      && ts.isPropertyAccessExpression(node.right)
      && AST_COLLECTIONS.has(node.right.name.text)
    ) {
      found = true;
      return;
    }
    if (
      ts.isCallExpression(node)
      && ts.isPropertyAccessExpression(node.expression)
    ) {
      const method = node.expression.name.text;
      const receiver = node.expression.expression;
      if (["getStart", "getFullStart", "getEnd"].includes(method)) {
        found = true;
        return;
      }
      if (
        method === "at"
        && ts.isPropertyAccessExpression(receiver)
        && AST_COLLECTIONS.has(receiver.name.text)
      ) {
        found = true;
        return;
      }
      if (
        TEXT_POSITION_METHODS.has(method)
        && (
          registeredReader
          || (
            ts.isIdentifier(receiver)
            && SOURCE_INPUT_NAME.test(receiver.text)
            && !arrayBindings.has(receiver.text)
          )
        )
      ) {
        found = true;
        return;
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
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
  for (const source of [
    "import fs from 'fs'; void fs;",
    "const fs = require('node:fs'); void fs;",
    "const fs = await import('fs/promises'); void fs;",
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
        + "const values = [];\nvalues.slice();",
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
});


test("source policy leaves ordinary array operations alone", () => {
  for (const source of [
    "const values = [1]; values.slice().sort();",
    "function clone(values: string[]) { return values.slice(); }",
    "function clone(payload: readonly string[]) { return payload.slice(); }",
    "function clone(payload: Uint8Array) { return payload.slice(); }",
    "function trimPrefix(value: string) { return value.slice(1); }",
  ]) {
    assert.deepEqual(
      modulePolicyOffenders("example.test.ts", source),
      [],
      source,
    );
  }
});


test("source policy stays bounded for repeated callable syntax", () => {
  const repeatedCalls = Array.from(
    { length: 1_000 },
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
