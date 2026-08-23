import test from "node:test";
import assert from "node:assert/strict";
import { readFile, readdir, stat } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";


// `node --test` 对 `.tsx` 报 `Unknown file extension`，而两个 node 泳道的用例
// (extension-ui-parity / extension-ui-registry) 真的 import 生产 registry 模块。
// 这条守卫把那个隐含前提写成断言：registry.ts 的整个模块闭包必须全是 `.ts`，
// 而 node 泳道的用例不得 import 任何只有 .tsx-aware 打包器才装得下的模块。
// 违反它不会在这里报错，只会让整条 node 泳道在 import 阶段炸掉。
const FRONTEND_DIR = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../..",
);
const NODE_LANE_ROOT = "features/extension-sdk/registry.ts";
// 空转保护：闭包必须真的走到内建插件模块。少了这一条，registry.ts 变成一个孤立
// 模块（或解析函数整个失效）时守卫仍然全绿。刻意**不带扩展名**：把内建插件改成
// `.tsx` 时该报红的是下面那条「闭包必须全是 .ts」——它才写着怎么修——而不是这条
// 说不出所以然的空转保护。
const NODE_LANE_WITNESS_BASE = "features/agent-profile/workspace-plugin";
const TSX_ONLY_MODULES = new Set([
  // 合并 registry：它 import 生成物，生成物 import 插件包的 .tsx 入口。
  "features/extension-sdk/workspace-registry.ts",
  "features/extension-sdk/registry.local.ts",
  // host 与 SDK 的共享 UI 都是 JSX。
  "features/extension-sdk/host.tsx",
  "features/extension-sdk/ui.tsx",
]);
// 仓库外插件包的落点，整目录都可能是 .tsx。
const TSX_ONLY_PACKAGE = /^features\/ext-[a-z0-9-]+\//;
// node 泳道的扫描面。`tests/{unit,guards}` 是入口；`test-support/**.mjs` 被那些入口
// 真实 import（semantic-source.mjs、static-source-contracts.mjs），它们的说明符同样
// 会在 import 阶段被解析——把这些模块自己也当扫描根，就是把那一跳跟到底：只扫入口
// 的直接说明符时，一个 test-support 模块 import 了 host.tsx 照样会炸整条泳道，而守卫
// 全绿。
const NODE_LANE_SCAN = [
  { root: "tests/unit", suffix: ".test.mjs" },
  { root: "tests/guards", suffix: ".test.mjs" },
  { root: "test-support", suffix: ".mjs" },
];


function scriptKind(relative) {
  if (relative.endsWith(".tsx")) return ts.ScriptKind.TSX;
  if (relative.endsWith(".ts")) return ts.ScriptKind.TS;
  return ts.ScriptKind.JS;
}


/** 自写解析：原样 → `.ts` → `.tsx` → `/index.ts`。与 tsconfig 的
 *  `allowImportingTsExtensions` 一致，够覆盖本仓库的写法。 */
function resolutionCandidates(base) {
  return [base, `${base}.ts`, `${base}.tsx`, `${base}/index.ts`];
}


function resolveFrom(relative, specifier) {
  return path.posix.normalize(
    path.posix.join(path.posix.dirname(relative), specifier),
  );
}


async function isFile(relative) {
  try {
    return (await stat(path.join(FRONTEND_DIR, relative))).isFile();
  } catch {
    return false;
  }
}


/**
 * 整条语句都是 type-only 的 import/export **不是**模块图上的边：Node 的 type
 * stripping 会把整条语句擦掉，那个模块根本不会被加载（仓库现存写法：
 * `app/ask-retrieval-effort.ts` 的 `import type { … } from "./…"`）。把它算成边，
 * 这条守卫给出的失败文案就是事实错误——它会说「node --test 装不下这个模块」，
 * 而 node 压根不会去装它。
 *
 * 内联的 `import { type X, y } from "./z"` 仍然是边：那条语句在运行时照常加载
 * `./z`，被擦掉的只是花括号里的那个名字。
 */
function typeOnlyStatement(item) {
  if (ts.isImportDeclaration(item)) return Boolean(item.importClause?.isTypeOnly);
  if (ts.isExportDeclaration(item)) return Boolean(item.isTypeOnly);
  return false;
}


function relativeSpecifiersIn(parsed) {
  const found = [];
  function collect(item) {
    if (
      (ts.isImportDeclaration(item) || ts.isExportDeclaration(item))
      && item.moduleSpecifier
      && ts.isStringLiteral(item.moduleSpecifier)
      && !typeOnlyStatement(item)
    ) {
      found.push(item.moduleSpecifier.text);
    }
    ts.forEachChild(item, collect);
  }
  collect(parsed);
  return found.filter((specifier) => specifier.startsWith("."));
}


function parseSpecifiers(relative, moduleText) {
  return relativeSpecifiersIn(ts.createSourceFile(
    relative,
    moduleText,
    ts.ScriptTarget.Latest,
    true,
    scriptKind(relative),
  ));
}


async function relativeSpecifiers(relative) {
  return parseSpecifiers(
    relative,
    await readFile(path.join(FRONTEND_DIR, relative), "utf8"),
  );
}


async function nodeLaneClosure() {
  const reached = new Set();
  const unresolved = [];
  const pending = [NODE_LANE_ROOT];
  while (pending.length > 0) {
    const current = pending.pop();
    if (reached.has(current)) continue;
    reached.add(current);
    for (const specifier of await relativeSpecifiers(current)) {
      const base = resolveFrom(current, specifier);
      let resolved;
      for (const candidate of resolutionCandidates(base)) {
        if (await isFile(candidate)) {
          resolved = candidate;
          break;
        }
      }
      if (resolved === undefined) {
        unresolved.push(`${current} -> ${specifier}`);
        continue;
      }
      pending.push(resolved);
    }
  }
  return { reached, unresolved };
}


async function nodeLaneTestModules() {
  const found = [];
  async function visit(relative, suffix) {
    for (
      const entry of await readdir(path.join(FRONTEND_DIR, relative), { withFileTypes: true })
    ) {
      const child = `${relative}/${entry.name}`;
      if (entry.isDirectory()) {
        await visit(child, suffix);
        continue;
      }
      if (entry.name.endsWith(suffix)) found.push(child);
    }
  }
  for (const scan of NODE_LANE_SCAN) await visit(scan.root, scan.suffix);
  return found.sort();
}


test("the builtin registry's module closure stays free of .tsx", async () => {
  const closure = await nodeLaneClosure();
  assert.deepEqual(
    closure.unresolved.sort(),
    [],
    "every relative specifier reachable from the builtin registry must resolve on disk",
  );
  assert.ok(
    resolutionCandidates(NODE_LANE_WITNESS_BASE).some(
      (candidate) => closure.reached.has(candidate),
    ),
    "closure must reach the builtin plugin module (guard would otherwise be vacuous)",
  );
  assert.deepEqual(
    [...closure.reached].filter(
      (relative) => !relative.endsWith(".ts") || relative.endsWith(".d.ts"),
    ).sort(),
    [],
    "node --test cannot load .tsx: every module reachable from the builtin registry must be .ts",
  );
});


test("only statements that survive type stripping count as module-graph edges", () => {
  const sample = [
    'import type { Alpha } from "./whole-statement-type-only.ts";',
    'import type Bravo from "./whole-statement-default-type-only.ts";',
    'export type { Charlie } from "./whole-statement-type-reexport.ts";',
    'import { type Delta, echo } from "./inline-type.ts";',
    'import "./side-effect.ts";',
    'import { foxtrot } from "./value.ts";',
    'export { golf } from "./value-reexport.ts";',
    'import { hotel } from "bare-package";',
  ].join("\n");
  assert.deepEqual(parseSpecifiers("features/sample.ts", sample).sort(), [
    "./inline-type.ts",
    "./side-effect.ts",
    "./value-reexport.ts",
    "./value.ts",
  ]);
});


test("node --test modules never import a .tsx-only module", async () => {
  const scanned = await nodeLaneTestModules();
  assert.ok(scanned.length > 0, "node lane must contain test modules to scan");
  assert.ok(
    scanned.some((relative) => relative.startsWith("test-support/")),
    "the shared node lane helpers must be scanned too (they are imported by the entrypoints)",
  );
  const offenders = [];
  let resolvesBuiltinRegistry = false;
  for (const relative of scanned) {
    for (const specifier of await relativeSpecifiers(relative)) {
      const base = resolveFrom(relative, specifier);
      for (const candidate of resolutionCandidates(base)) {
        if (candidate === NODE_LANE_ROOT) resolvesBuiltinRegistry = true;
        if (TSX_ONLY_MODULES.has(candidate) || TSX_ONLY_PACKAGE.test(candidate)) {
          offenders.push(`${relative} -> ${specifier}`);
        }
      }
    }
  }
  // 空转保护：至少一个 node 泳道用例的说明符真的解析到了内建 registry，证明这套
  // 跨目录解析确实在工作，而不是把每个说明符都解析成了无。
  assert.ok(
    resolvesBuiltinRegistry,
    "at least one node lane module must resolve to the builtin registry (guard would otherwise be vacuous)",
  );
  assert.deepEqual(
    [...new Set(offenders)].sort(),
    [],
    "node --test cannot load these modules; import the builtin registry instead, or move the case to tests/component",
  );
});
