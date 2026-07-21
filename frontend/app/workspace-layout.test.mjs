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
  assert.deepEqual(
    importsFrom(page, "./ask-session-header").map((item) => item.imported),
    ["AskSessionHeaderActions"],
  );
  assert.equal(jsxElements(page, "AskComposer").length, 1);
  assert.equal(jsxElements(page, "AccountMenu").length, 1);
  const sessionHeaders = jsxElements(page, "AskSessionHeaderActions");
  assert.equal(sessionHeaders.length, 1);
  assert.deepEqual(sessionHeaders[0].bindings, {
    sessionCount: "sessions.length",
    sessionPanelOpen: "sessionPanelOpen",
    onToggleSessionPanel: "() => setSessionPanelOpen(open => !open)",
    onStartNewSession: "startNewSession",
  });
  const pageFunctions = new Set(
    declarations(page)
      .filter((finding) => finding.kind === "function")
      .map((finding) => finding.name),
  );
  assert.equal(pageFunctions.has("AskComposer"), false);
  assert.equal(pageFunctions.has("AccountMenu"), false);
});


test("Ask session controls occupy one header row", () => {
  assert.equal(
    jsxElements(page, "div").some(
      ({ attributes }) => attributes.className === "chat-session-context",
    ),
    false,
  );
  assert.ok(
    jsxElements(page, "div").some(
      ({ attributes }) => (
        attributes.id === "ask-session-manager"
        && attributes.className === "chat-session-popover"
        && attributes.role === "dialog"
        && attributes["aria-label"] === "会话管理"
      ),
    ),
  );
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


test("source detail uses the dedicated draggable window shell", () => {
  assert.deepEqual(
    importsFrom(page, "./source-detail-window").map((item) => item.imported),
    ["SourceDetailWindow"],
  );
  const windows = jsxElements(page, "SourceDetailWindow");
  assert.equal(windows.length, 1);
  assert.deepEqual(windows[0].bindings, {
    onClose: "() => setSourceDetail(null)",
  });
  assert.equal(
    importsFrom(page, "lucide-react").some(({ imported }) => imported === "PanelRightClose"),
    false,
  );
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
