import test from "node:test";
import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { STATIC_SOURCE_CONTRACTS } from "./static-source-contracts.mjs";


const APP_DIR = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const DIRECT_READ_ALLOWLIST = new Set([
  "test/semantic-source.mjs",
  "test/static-source-policy.test.mjs",
  "test-runner-config.test.mjs",
]);


async function testModules(directory = APP_DIR) {
  const entries = await readdir(directory, { withFileTypes: true });
  const modules = [];
  for (const entry of entries) {
    const absolute = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      modules.push(...await testModules(absolute));
    } else if (
      entry.name.endsWith(".test.mjs")
      || entry.name.endsWith(".component.test.tsx")
    ) {
      modules.push(absolute);
    }
  }
  return modules.sort();
}


async function policyOffenders() {
  const offenders = [];
  for (const absolute of await testModules()) {
    const relative = path.relative(APP_DIR, absolute).replaceAll(path.sep, "/");
    const source = await readFile(absolute, "utf8");
    const readsFiles = (
      /from\s+["']node:fs(?:\/promises)?["']/.test(source)
      || /import\(\s*["']node:fs(?:\/promises)?["']\s*\)/.test(source)
    );
    if (readsFiles && !DIRECT_READ_ALLOWLIST.has(relative)) {
      offenders.push(`${relative}: direct production-source read`);
    }
    if (
      readsFiles
      && /\.(?:indexOf|slice|substring|getStart|getEnd)\s*\(/.test(source)
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


test("frontend tests do not inspect production source layout directly", async () => {
  assert.deepEqual(await policyOffenders(), []);
});
