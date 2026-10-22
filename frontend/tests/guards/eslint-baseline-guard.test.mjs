// ESLint 门禁基线的守卫：补 ESLint 自己核对不到的几件事。
//
// `npm run lint` 拿 eslint-suppressions.json 做基线跑（ESLint 的 bulk suppressions，按
// 「文件 + 规则」计数）。对本次 lint 到的每个文件，它在两种情况下报错：某条规则的违反数
// 超过该格计数；或该格计数大于实际仍存在的违反数（exit 2）。它只核对 lint 到的文件。下面
// 每一节针对一个它管不到的洞：
//   1. 每条规则的总条数与涉及文件数零松弛钉在 `BASELINE_CEILING`：`eslint --suppress-all`
//      把新违反收进基线后，这里的数字就对不上。它按规则合计，所以「A 文件修掉一条、B 文件
//      新增一条」这种同一规则跨文件对冲，总数不变，这里看不出来，只能靠评审
//      eslint-suppressions.json 的 diff。
//   2. 基线里的文件必须仍存在且仍受检：文件删除、改名或被加进 `ignores` 之后，ESLint 不再
//      lint 它，也就从不核对它那几格。
//   3. 规则不许被调松：`ignores` 列表与 `reportUnusedDisableDirectives` 钉死；全部受检源码都
//      没被忽略，两条 react-hooks 规则在每个文件上都解析为 error，且没有规则停在 warn。
//   4. rules-of-hooks 永不豁免：既不进基线，也不许出现针对它的 disable 注释（它的违反要么
//      是真 bug，要么是被误命名成 `use*` 的 helper，都要当场改）。
import test from "node:test";
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { ESLint } from "eslint";
import ts from "typescript";

const FRONTEND_DIR = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../..",
);

/** 每条规则在基线里的总条数与涉及文件数。只许调低，且与基线逐项相等。 */
const BASELINE_CEILING = {
  "react-hooks/exhaustive-deps": { count: 43, files: 17 },
  "@typescript-eslint/no-explicit-any": { count: 13, files: 4 },
  "@next/next/no-img-element": { count: 6, files: 3 },
  "@next/next/no-html-link-for-pages": { count: 3, files: 2 },
};

/** eslint.config.mjs 里那份只含 `ignores` 的全局忽略表。改它要同时改这里。 */
const EXPECTED_IGNORES = [
  ".next/**",
  "node_modules/**",
  "next-env.d.ts",
  ".local/**",
  "coverage/**",
  "dist/**",
  "build/**",
  "features/ext-*/**",
  "features/extension-sdk/registry.local.ts",
];

/** 受检源码的根目录（frontend/ 下全部手写源码所在）。 */
const SOURCE_ROOTS = ["app", "features", "scripts", "test-support", "tests"];
const SOURCE_FILE = /\.(?:[cm]?[jt]sx?)$/;
/** 生成物：由 sync-ui-plugins 写出，按设计不受检。 */
const GENERATED = [
  /^features\/ext-[^/]+\//,
  /^features\/extension-sdk\/registry\.local\.ts$/,
];

async function readJson(relative) {
  return JSON.parse(await readFile(path.join(FRONTEND_DIR, relative), "utf8"));
}

async function sourceFiles() {
  const files = [];
  for (const root of SOURCE_ROOTS) {
    const entries = await readdir(path.join(FRONTEND_DIR, root), { recursive: true });
    for (const entry of entries) {
      const relative = `${root}/${entry.split(path.sep).join("/")}`;
      if (!SOURCE_FILE.test(relative) || GENERATED.some((pattern) => pattern.test(relative))) continue;
      files.push(relative);
    }
  }
  return files.sort();
}

function baselineTotals(suppressions) {
  const totals = {};
  for (const rules of Object.values(suppressions)) {
    for (const [rule, { count }] of Object.entries(rules)) {
      totals[rule] ??= { count: 0, files: 0 };
      totals[rule].count += count;
      totals[rule].files += 1;
    }
  }
  return totals;
}

/**
 * eslint.config.mjs 里每个带 `ignores` 的对象字面量：它的键名与 `ignores` 数组的字符串。
 * 不 import 配置模块：FlatCompat 载入 eslint-config-next 时会给 ESLint 打补丁，只能在
 * ESLint 自己的调用栈里执行；配置文件是元数据，按 AST 读。
 */
async function ignoresBlocks() {
  const source = await readFile(path.join(FRONTEND_DIR, "eslint.config.mjs"), "utf8");
  const file = ts.createSourceFile("eslint.config.mjs", source, ts.ScriptTarget.Latest, true);
  const blocks = [];
  (function visit(node) {
    if (ts.isObjectLiteralExpression(node)) {
      const keys = node.properties.map((property) => property.name?.getText(file));
      const ignores = node.properties.find((property) => property.name?.getText(file) === "ignores");
      if (ignores) {
        assert.ok(
          ts.isPropertyAssignment(ignores) && ts.isArrayLiteralExpression(ignores.initializer),
          "ignores must be a literal array",
        );
        const values = ignores.initializer.elements.map((element) => {
          assert.ok(ts.isStringLiteral(element), "ignores entries must be string literals");
          return element.text;
        });
        blocks.push({ keys, values });
      }
    }
    ts.forEachChild(node, visit);
  })(file);
  return blocks;
}

function severity(config, rule) {
  const value = config.rules?.[rule];
  return Array.isArray(value) ? value[0] : value;
}

test("ESLint 基线按规则的总条数与文件数等于钉住的上限（零松弛）", async () => {
  const totals = baselineTotals(await readJson("eslint-suppressions.json"));
  assert.deepEqual(totals, BASELINE_CEILING);
});

test("基线里每个文件都存在、未被忽略，每格计数是正整数", async () => {
  const suppressions = await readJson("eslint-suppressions.json");
  const eslint = new ESLint({ cwd: FRONTEND_DIR });
  const missing = [];
  const ignored = [];
  for (const [file, rules] of Object.entries(suppressions)) {
    if (!existsSync(path.join(FRONTEND_DIR, file))) missing.push(file);
    if (await eslint.isPathIgnored(path.join(FRONTEND_DIR, file)) !== false) ignored.push(file);
    for (const [rule, entry] of Object.entries(rules)) {
      assert.ok(
        Number.isInteger(entry.count) && entry.count > 0,
        `${file} ${rule}: count must be a positive integer`,
      );
    }
  }
  assert.deepEqual(missing, [], "baseline entries for files that no longer exist");
  assert.deepEqual(ignored, [], "baseline entries for files ESLint no longer lints");
});

test("全局 ignores 表与 reportUnusedDisableDirectives 钉死", async () => {
  // 只有一个带 ignores 的块，而且它只有 ignores 一个键（即全局忽略表）。别的块再写
  // ignores，要么是另一份全局忽略表，要么悄悄缩小了某组规则的适用面，都要先改这里。
  assert.deepEqual(await ignoresBlocks(), [{ keys: ["ignores"], values: EXPECTED_IGNORES }]);
  const config = await new ESLint({ cwd: FRONTEND_DIR })
    .calculateConfigForFile(path.join(FRONTEND_DIR, "app/page.tsx"));
  assert.equal(config.linterOptions.reportUnusedDisableDirectives, 2);
});

test("受检源码一个不漏：都未被忽略，react-hooks 两条规则都是 error，没有规则停在 warn", async () => {
  const eslint = new ESLint({ cwd: FRONTEND_DIR });
  const files = await sourceFiles();
  // 非空判据：根目录读错或判据写坏时不能「零个文件、全部通过」。
  assert.ok(files.length > 500, `only ${files.length} source files found`);
  assert.ok(files.includes("app/page.tsx"));
  const ignored = [];
  const notError = [];
  const warned = [];
  for (const file of files) {
    const absolute = path.join(FRONTEND_DIR, file);
    if (await eslint.isPathIgnored(absolute) !== false) {
      ignored.push(file);
      continue;
    }
    const config = await eslint.calculateConfigForFile(absolute);
    for (const rule of ["react-hooks/rules-of-hooks", "react-hooks/exhaustive-deps"]) {
      if (severity(config, rule) !== 2) notError.push(`${file}: ${rule}`);
    }
    for (const rule of Object.keys(config.rules ?? {})) {
      if ([1, "warn"].includes(severity(config, rule))) warned.push(`${file}: ${rule}`);
    }
  }
  assert.deepEqual(ignored, []);
  assert.deepEqual(notError, []);
  assert.deepEqual(warned, []);
});

test("rules-of-hooks 永不豁免：不进基线，也没有针对它的 disable 注释", async () => {
  const suppressions = await readJson("eslint-suppressions.json");
  assert.deepEqual(
    Object.entries(suppressions)
      .filter(([, rules]) => "react-hooks/rules-of-hooks" in rules)
      .map(([file]) => file),
    [],
  );
  // 注释不在 AST 里，文本是唯一诚实的输入；这里只找 disable 指令，不做任何位置查询。
  const directive = /eslint-disable(?:-next-line|-line)?\b[^\n]*react-hooks\/rules-of-hooks/;
  const offenders = [];
  for (const file of await sourceFiles()) {
    if (file === "tests/guards/eslint-baseline-guard.test.mjs") continue;
    if (directive.test(await readFile(path.join(FRONTEND_DIR, file), "utf8"))) offenders.push(file);
  }
  assert.deepEqual(offenders, []);
});

test("lint 脚本是带基线的 ESLint 门，不放过 warning；类型检查是单独的 typecheck", async () => {
  const { scripts } = await readJson("package.json");
  assert.equal(scripts.lint, "eslint --max-warnings 0 .");
  assert.equal(scripts["lint:prune"], "eslint --prune-suppressions .");
  assert.equal(scripts.typecheck, "tsc --noEmit");
});
