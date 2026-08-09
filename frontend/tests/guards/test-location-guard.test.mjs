import test from "node:test";
import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";


const FRONTEND_DIR = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../..",
);
const TESTS_DIR = path.join(FRONTEND_DIR, "tests");
const PRODUCTION_DIRS = [
  path.join(FRONTEND_DIR, "app"),
  path.join(FRONTEND_DIR, "features"),
];

async function testEntrypointsIn(directory) {
  const found = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const absolute = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      found.push(...await testEntrypointsIn(absolute));
    } else if (/\.(?:test\.mjs|component\.test\.tsx)$/.test(entry.name)) {
      found.push(path.relative(FRONTEND_DIR, absolute).replaceAll(path.sep, "/"));
    }
  }
  return found.sort();
}

test("frontend test entrypoints live only under the dedicated tests tree", async () => {
  const [registered, ...misplaced] = await Promise.all([
    testEntrypointsIn(TESTS_DIR),
    ...PRODUCTION_DIRS.map(testEntrypointsIn),
  ]);
  assert.ok(registered.some((entry) => entry.startsWith("tests/unit/")));
  assert.ok(registered.some((entry) => entry.startsWith("tests/component/")));
  assert.ok(registered.some((entry) => entry.startsWith("tests/guards/")));
  assert.deepEqual(misplaced.flat(), []);
});

test("test runners recursively collect the dedicated unit, guard, and component suites", async () => {
  const [packageSource, vitestSource] = await Promise.all([
    readFile(path.join(FRONTEND_DIR, "package.json"), "utf8"),
    readFile(path.join(FRONTEND_DIR, "vitest.config.ts"), "utf8"),
  ]);
  const packageJson = JSON.parse(packageSource);
  assert.equal(
    packageJson.scripts["test:node"],
    "node --test --test-concurrency=4 $(find tests/unit tests/guards -name '*.test.mjs' -type f -print)",
  );
  assert.match(vitestSource, /include:\s*\["tests\/component\/\*\*\/\*\.component\.test\.tsx"\]/);
});
