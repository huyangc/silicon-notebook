import test from "node:test";
import assert from "node:assert/strict";

import {
  doneItemDestination,
  workspaceCapabilities,
  workspaceRequestIsCurrent,
} from "./workspace-transitions.ts";
import {
  declarations,
  importsFrom,
  jsxElements,
  jsxTextValues,
  parseModule,
} from "./test/semantic-source.mjs";


const page = await parseModule("page.tsx");


test("workspace composes executable Ask and account components", () => {
  assert.deepEqual(
    importsFrom(page, "./ask-composer").map((item) => item.imported),
    ["AskComposer"],
  );
  assert.deepEqual(
    importsFrom(page, "./account-menu").map((item) => item.imported),
    ["AccountMenu"],
  );
  assert.equal(jsxElements(page, "AskComposer").length, 1);
  assert.equal(jsxElements(page, "AccountMenu").length, 1);
  const pageFunctions = new Set(
    declarations(page)
      .filter((finding) => finding.kind === "function")
      .map((finding) => finding.name),
  );
  assert.equal(pageFunctions.has("AskComposer"), false);
  assert.equal(pageFunctions.has("AccountMenu"), false);
});


test("workspace has no retired Studio panel and keeps a labelled exit", () => {
  const classes = [
    ...jsxElements(page, "div"),
    ...jsxElements(page, "section"),
  ].map(({ attributes }) => attributes.className);
  assert.equal(classes.includes("workspace-panel studio-panel"), false);
  assert.ok(
    jsxElements(page, "button")
      .some(({ attributes }) => attributes.className === "notebook-home"),
  );
  assert.ok(jsxTextValues(page).includes("返回主页"));
});


test("source actions remain available by accessible meaning", () => {
  const buttons = jsxElements(page, "button");
  const links = jsxElements(page, "a");
  assert.ok(buttons.some(({ attributes }) => attributes.title === "删除来源"));
  assert.ok(links.some(({ attributes }) => attributes["aria-label"] === "打开原始链接"));
});


test("background responses require the same workspace and notebook", () => {
  assert.equal(
    workspaceRequestIsCurrent(false, 3, 3, "nb-1", "nb-1"),
    true,
  );
  assert.equal(
    workspaceRequestIsCurrent(true, 3, 3, "nb-1", "nb-1"),
    false,
  );
  assert.equal(
    workspaceRequestIsCurrent(false, 2, 3, "nb-1", "nb-1"),
    false,
  );
  assert.equal(
    workspaceRequestIsCurrent(false, 3, 3, "nb-1", "nb-2"),
    false,
  );
});


test("workspace capabilities mirror read-only and admin boundaries", () => {
  assert.deepEqual(workspaceCapabilities("reader", "user"), {
    canWriteNotebook: false,
    canGovernKnowledge: false,
    canManageReports: false,
    canManageSchemas: false,
  });
  assert.deepEqual(workspaceCapabilities("owner", "admin"), {
    canWriteNotebook: true,
    canGovernKnowledge: true,
    canManageReports: true,
    canManageSchemas: true,
  });
});


test("completed paper metadata opens sources while index work opens KG", () => {
  assert.equal(doneItemDestination("paper_meta_done"), "sources");
  assert.equal(doneItemDestination("index_done"), "kg");
  assert.equal(doneItemDestination(undefined), "kg");
});
