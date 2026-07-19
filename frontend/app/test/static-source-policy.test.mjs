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
      && ts.isStringLiteral(node.arguments[0])
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


function isDefinitelyArrayInput(parameter) {
  if (
    parameter.initializer
    && ts.isArrayLiteralExpression(parameter.initializer)
  ) {
    return true;
  }
  function isArrayType(type) {
    if (!type) return false;
    if (ts.isArrayTypeNode(type) || ts.isTupleTypeNode(type)) return true;
    if (ts.isParenthesizedTypeNode(type)) return isArrayType(type.type);
    if (ts.isUnionTypeNode(type)) return type.types.every(isArrayType);
    return (
      ts.isTypeReferenceNode(type)
      && ts.isIdentifier(type.typeName)
      && ["Array", "ReadonlyArray"].includes(type.typeName.text)
    );
  }
  return isArrayType(parameter.type);
}


function textFlowReceivers(sourceFile, includeHelperInputs) {
  const receivers = new Set();

  function unwrapAwait(expression) {
    return ts.isAwaitExpression(expression)
      ? expression.expression
      : expression;
  }

  function isTextFlow(expression) {
    const unwrapped = unwrapAwait(expression);
    if (ts.isIdentifier(unwrapped)) return receivers.has(unwrapped.text);
    return (
      ts.isCallExpression(unwrapped)
      && (
        (
          ts.isIdentifier(unwrapped.expression)
          && ["readFile", "readFileSync"].includes(
            unwrapped.expression.text,
          )
        )
        || (
          ts.isPropertyAccessExpression(unwrapped.expression)
          && ["readFile", "readFileSync"].includes(
            unwrapped.expression.name.text,
          )
        )
      )
    );
  }

  function visit(node) {
    if (includeHelperInputs && ts.isFunctionLike(node)) {
      for (const parameter of node.parameters) {
        if (
          ts.isIdentifier(parameter.name)
          && !isDefinitelyArrayInput(parameter)
        ) {
          receivers.add(parameter.name.text);
        }
      }
    }
    if (
      ts.isVariableDeclaration(node)
      && ts.isIdentifier(node.name)
      && node.initializer
      && isTextFlow(node.initializer)
    ) {
      receivers.add(node.name.text);
    }
    if (
      ts.isBinaryExpression(node)
      && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
      && ts.isIdentifier(node.left)
      && isTextFlow(node.right)
    ) {
      receivers.add(node.left.text);
    }
    ts.forEachChild(node, visit);
  }

  visit(sourceFile);
  return receivers;
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
  const flowReceivers = textFlowReceivers(
    sourceFile,
    !isTestEntrypoint(relative),
  );

  function sourceLike(expression) {
    return (
      ts.isIdentifier(expression)
      && /^(?:source|sourceText|productionSource|content)$/i.test(
        expression.text,
      )
    );
  }

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
        && (
          sourceLike(node.expression.expression)
          || (
            ts.isIdentifier(node.expression.expression)
            && flowReceivers.has(node.expression.expression.text)
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
      "example.test.mjs",
      "const copy = values.slice().sort();",
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
