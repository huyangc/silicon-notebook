import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const packageJson = JSON.parse(
  await readFile(new URL("../../package.json", import.meta.url), "utf8"),
);
const vitestConfig = await readFile(
  new URL("../../vitest.config.ts", import.meta.url),
  "utf8",
);

test("frontend Node tests import TypeScript modules under explicit ESM mode", () => {
  assert.equal(packageJson.type, "module");
});

test("frontend test command runs pure and component suites", () => {
  assert.equal(
    packageJson.scripts["test:node"],
    "node --test --test-concurrency=4 $(find tests/unit tests/guards -name '*.test.mjs' -type f -print)",
  );
  assert.equal(packageJson.scripts["test:component"], "vitest run");
  assert.equal(
    packageJson.scripts.test,
    "npm run test:node && npm run test:component",
  );
});

test("component tests have an exclusive suffix and jsdom environment", () => {
  assert.match(vitestConfig, /include:\s*\["tests\/component\/\*\*\/\*\.component\.test\.tsx"\]/);
  assert.match(vitestConfig, /environment:\s*"jsdom"/);
  assert.match(vitestConfig, /maxWorkers:\s*4/);
});
