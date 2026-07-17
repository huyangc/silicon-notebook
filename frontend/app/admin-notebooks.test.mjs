import { test } from "node:test";
import assert from "node:assert/strict";
import { notebookStatusLabel } from "./admin/usage/notebooks.ts";

test("notebookStatusLabel maps known + falls back", () => {
  assert.equal(notebookStatusLabel("ready"), "就绪");
  assert.equal(notebookStatusLabel("draft"), "草稿");
  assert.equal(notebookStatusLabel("weird"), "未知状态");
});
