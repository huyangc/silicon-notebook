import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const page = await readFile(new URL("./page.tsx", import.meta.url), "utf8");
const css = await readFile(new URL("./globals.css", import.meta.url), "utf8");

test("workspace uses a two-column source and ask layout without the inactive Studio sidebar", () => {
  assert.ok(page.includes('className="workspace-grid"'));
  assert.equal(page.includes('className="workspace-panel studio-panel"'), false);
  assert.match(css, /grid-template-columns:\s*minmax\(270px,\s*25%\)\s+minmax\(0,\s*1fr\);/);
  assert.equal(css.includes('"studio studio"'), false);
});

test("workspace top actions use a designed toolbar instead of generic sort buttons", () => {
  assert.ok(page.includes('className="workspace-toolbar"'));
  assert.ok(page.includes('className="workspace-nav-button"'));
  assert.equal(page.includes('<button className="sort-button" onClick={() => openAnalytics()'), false);
  assert.match(css, /\.workspace-nav-button\s*{/);
});

test("workspace title bar keeps the notebook description out of the header", () => {
  const headerStart = page.indexOf('<section className="workspace-header">');
  const toolbarStart = page.indexOf('<div className="workspace-toolbar"', headerStart);
  assert.ok(headerStart > -1);
  assert.ok(toolbarStart > headerStart);
  const titleArea = page.slice(headerStart, toolbarStart);

  assert.equal(titleArea.includes("currentNotebook.purpose"), false);
  assert.equal(titleArea.includes("This notebook has not defined a purpose yet."), false);
});

test("ask welcome copy can surface the notebook description when no conversation exists", () => {
  assert.ok(page.includes("const notebookPurpose = notebook?.purpose?.trim();"));
  assert.match(page, /description:\s*notebookPurpose\s*\|\|/);
});

test("workspace toolbar has overflow protection so action labels are not clipped", () => {
  assert.match(css, /\.workspace-title\s*{[^}]*max-width:\s*min\(48vw,\s*720px\);/s);
  assert.match(css, /\.workspace-toolbar\s*{[^}]*overflow-x:\s*auto;/s);
  assert.match(css, /\.workspace-nav-button\s*{[^}]*flex:\s*0 0 auto;/s);
});

test("ask input submits with Enter while preserving Shift+Enter for new lines", () => {
  assert.ok(page.includes("function handleAskInputKeyDown"));
  assert.match(page, /event\.key === "Enter"[\s\S]*!event\.shiftKey/);
  assert.match(page, /event\.preventDefault\(\);[\s\S]*runAsk\(\)\.catch\(reportError\)/);
  assert.match(page, /onKeyDown=\{handleAskInputKeyDown\}/);
});

test("ask streaming exposes an abort path through the send button", () => {
  assert.ok(page.includes("const askAbortRef = useRef<AbortController | null>(null);"));
  assert.ok(page.includes("function abortAsk()"));
  assert.match(page, /readAskStream<AskResponse>\([\s\S]*controller\.signal/);
  assert.match(page, /asking \? abortAsk\(\) : runAsk\(\)\.catch\(reportError\)/);
  assert.match(page, /aria-label=\{asking \? "中断生成" : "发送"\}/);
});

test("ask controls lock input and prevent resend while the model is running", () => {
  assert.match(page, /async function runAsk[\s\S]*if \(asking\) return;/);
  assert.match(page, /className="chat-input"[\s\S]*disabled=\{asking\}/);
  assert.match(page, /disabled=\{!asking && !question\.trim\(\)\}/);
  assert.ok((page.match(/disabled=\{asking\}/g) ?? []).length >= 3);
  assert.match(css, /\.chat-input:disabled\s*{/);
  assert.match(css, /\.mode-tab:disabled,[\s\S]*\.mode-engine:disabled/);
  assert.match(css, /\.send-button\.stop\s*{/);
});
