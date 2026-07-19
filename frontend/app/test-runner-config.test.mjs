import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const packageJson = JSON.parse(
  await readFile(new URL("../package.json", import.meta.url), "utf8"),
);

test("frontend Node tests import TypeScript modules under explicit ESM mode", () => {
  assert.equal(packageJson.type, "module");
});
