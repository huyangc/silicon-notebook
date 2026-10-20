// ESLint 门禁的基线只降不升。
//
// `npm run lint` 对照 eslint-suppressions.json（ESLint 的 bulk suppressions，按「文件 +
// 规则」计数）运行：某文件某规则的违反数超过基线就报错，少于基线也报错（exit 2，「基线里
// 有已经不再发生的条目」），所以 ESLint 自己保证基线与源码逐格相等。它保证不了三件事，由
// 本守卫补上：
//   1. 基线不得变大。`eslint --suppress-all` 一条命令就能把任何新违反收进基线；下面
//      `BASELINE_CEILING` 钉住每条规则的总条数与涉及文件数，零松弛——任何一格变大都要改
//      本文件的数字，评审看得见；清掉违反、`npm run lint:prune` 之后同一改动里把数字调低。
//   2. 文件删除或改名后的残留条目。ESLint 只拿本次 lint 到的文件去核对基线，不存在的文件
//      的条目它永远看不见；这里要求每个条目的文件都还在。
//   3. 规则本身没被调松。基线只记录 error，warning 既进不了基线也会被 `--max-warnings 0`
//      直接拦下；这里钉住两条 react-hooks 规则是 error，且 rules-of-hooks 永不进基线
//      （它的违反是真 bug 或必须改名的误报，不能「先记账」）。
import test from "node:test";
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { ESLint } from "eslint";

const FRONTEND_DIR = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../..",
);

/** 每条规则在基线里的总条数与涉及文件数。只许调低，且与基线逐格相等。 */
const BASELINE_CEILING = {
  "react-hooks/exhaustive-deps": { count: 44, files: 17 },
  "@typescript-eslint/no-explicit-any": { count: 13, files: 4 },
  "@next/next/no-img-element": { count: 6, files: 3 },
  "@next/next/no-html-link-for-pages": { count: 3, files: 2 },
};

async function readJson(relative) {
  return JSON.parse(await readFile(path.join(FRONTEND_DIR, relative), "utf8"));
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

test("ESLint 基线每条规则的条数与文件数等于钉住的上限（只降不升、零松弛）", async () => {
  const totals = baselineTotals(await readJson("eslint-suppressions.json"));
  assert.deepEqual(totals, BASELINE_CEILING);
});

test("rules-of-hooks 永不进基线", async () => {
  const suppressions = await readJson("eslint-suppressions.json");
  const offenders = Object.entries(suppressions)
    .filter(([, rules]) => "react-hooks/rules-of-hooks" in rules)
    .map(([file]) => file);
  assert.deepEqual(offenders, []);
});

test("基线里每个条目的文件都还存在（删除/改名不留残账）", async () => {
  const suppressions = await readJson("eslint-suppressions.json");
  const missing = Object.keys(suppressions)
    .filter((file) => !existsSync(path.join(FRONTEND_DIR, file)));
  assert.deepEqual(missing, []);
  for (const [file, rules] of Object.entries(suppressions)) {
    for (const [rule, entry] of Object.entries(rules)) {
      assert.ok(
        Number.isInteger(entry.count) && entry.count > 0,
        `${file} ${rule}: count must be a positive integer`,
      );
    }
  }
});

test("lint 脚本是带基线的 ESLint 门，不放过 warning、不吞掉过期基线", async () => {
  const { scripts } = await readJson("package.json");
  assert.equal(scripts.lint, "eslint --max-warnings 0 .");
  assert.equal(scripts["lint:prune"], "eslint --prune-suppressions .");
  assert.equal(scripts.typecheck, "tsc --noEmit");
});

test("react-hooks 两条规则对应用与测试代码都是 error，且没有一条规则停在 warn", async () => {
  const eslint = new ESLint({ cwd: FRONTEND_DIR });
  for (const file of ["app/page.tsx", "app/use-ask-session.ts", "tests/component/auth-gate.component.test.tsx"]) {
    const config = await eslint.calculateConfigForFile(path.join(FRONTEND_DIR, file));
    const severity = (rule) => {
      const value = config.rules[rule];
      return Array.isArray(value) ? value[0] : value;
    };
    assert.equal(severity("react-hooks/rules-of-hooks"), 2, `${file}: rules-of-hooks`);
    assert.equal(severity("react-hooks/exhaustive-deps"), 2, `${file}: exhaustive-deps`);
    const warned = Object.keys(config.rules).filter((rule) => [1, "warn"].includes(severity(rule)));
    assert.deepEqual(warned, [], `${file}: rules left at "warn"`);
  }
});
