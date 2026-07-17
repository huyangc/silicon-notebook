import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const page = await readFile(new URL("./page.tsx", import.meta.url), "utf8");
const css = await readFile(new URL("./globals.css", import.meta.url), "utf8");

test("workspace uses a two-column source and ask layout without the inactive Studio sidebar", () => {
  assert.ok(page.includes('className={`workspace-grid'));
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

test("topbar keeps logout inside a click-to-open account popover", () => {
  assert.match(page, /const \[accountMenuOpen, setAccountMenuOpen\] = useState\(false\);/);
  assert.match(page, /const accountMenuRef = useRef<HTMLDivElement \| null>\(null\);/);
  assert.match(page, /<div className="user-menu" ref=\{accountMenuRef\}>/);
  assert.match(page, /className="user-menu-trigger"[\s\S]*aria-haspopup="menu"[\s\S]*aria-expanded=\{accountMenuOpen\}/);
  assert.match(page, /\{accountMenuOpen && \(/);
  assert.match(page, /className="user-menu-popover"[\s\S]*role="menu"[\s\S]*className="user-logout"/);

  const userMenuStart = page.indexOf('<div className="user-menu" ref={accountMenuRef}>');
  const userMenuEnd = page.indexOf("</header>", userMenuStart);
  const userMenuMarkup = page.slice(userMenuStart, userMenuEnd);
  assert.ok(userMenuMarkup.indexOf('className="user-menu-trigger"') < userMenuMarkup.indexOf("{accountMenuOpen && ("));
  assert.ok(userMenuMarkup.indexOf('className="user-logout"') > userMenuMarkup.indexOf('className="user-menu-popover"'));

  assert.match(css, /\.user-menu\s*{[^}]*position:\s*relative;/s);
  assert.match(css, /\.user-menu-trigger\s*{[^}]*border-radius:\s*999px;/s);
  assert.match(css, /\.user-menu-popover\s*{[^}]*position:\s*absolute;[^}]*right:\s*0;/s);
  assert.match(css, /\.user-logout\s*{[^}]*width:\s*100%;/s);
});

test("ask abort uses a square stop control while a response is running", () => {
  assert.match(page, /Square/);
  assert.match(page, /\{asking \? <Square size=\{16\}/);
  assert.equal(page.includes("asking ? <X size={18} strokeWidth={3} />"), false);
  assert.match(css, /\.send-button\.stop\s*{[^}]*border-radius:\s*14px;/s);
  assert.match(css, /\.send-button\.stop\s*{[^}]*border:\s*1px solid/s);
  assert.match(css, /\.send-button\.stop svg\s*{[^}]*fill:\s*currentColor;/s);
});

test("source row keeps link and delete actions in the right action column", () => {
  assert.match(page, /<div className="source-row-actions">[\s\S]*source\.source_url[\s\S]*source-delete-button[\s\S]*<\/div>/);
  assert.match(css, /\.source-row-actions\s*{[^}]*display:\s*flex;[^}]*align-items:\s*center;[^}]*justify-content:\s*flex-end;/s);
  assert.match(css, /\.source-row-actions\s+\.source-link-button,\s*\.source-row-actions\s+\.source-delete-button\s*{[^}]*flex:\s*0 0 30px;/s);
});

test("source action column expands to fit both status-independent actions", () => {
  assert.match(css, /\.source-row\s*{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)\s+max-content;/s);
  assert.match(css, /\.source-row-actions\s*{[^}]*min-width:\s*max-content;/s);
  assert.equal(css.includes(".source-row {\n    grid-template-columns: minmax(0, 1fr) 34px;"), false);
});

test("session manager groups cleanup controls above the scrollable session list", () => {
  assert.match(page, /<div className="chat-session-popover-top">[\s\S]*className="chat-session-popover-head"[\s\S]*className="chat-session-cleanup"[\s\S]*<\/div>\s*<div className="chat-session-list">/);
  assert.match(css, /\.chat-session-popover\s*{[^}]*grid-template-rows:\s*auto\s+minmax\(0,\s*1fr\);/s);
  assert.match(css, /\.chat-session-popover-top\s*{[^}]*display:\s*grid;[^}]*gap:\s*10px;/s);
});

test("switching or leaving a notebook clears any pending ask-job reconnect to avoid cross-notebook state bleed", () => {
  const openNotebookStart = page.indexOf("async function openNotebook(notebookId: string)");
  const openNotebookEnd = page.indexOf("\n  }\n", openNotebookStart);
  assert.ok(openNotebookStart > -1);
  const openNotebookBody = page.slice(openNotebookStart, openNotebookEnd);
  assert.ok(openNotebookBody.includes("setReconnectJob(null);"));

  const showCollectionStart = page.indexOf("function showCollection() {");
  const showCollectionEnd = page.indexOf("\n  }\n", showCollectionStart);
  assert.ok(showCollectionStart > -1);
  const showCollectionBody = page.slice(showCollectionStart, showCollectionEnd);
  assert.ok(showCollectionBody.includes("setReconnectJob(null);"));
});

test("ask results are owned by a run and workspace epoch", () => {
  assert.match(page, /const askRunEpochRef = useRef\(0\);/);
  assert.match(page, /const workspaceEpochRef = useRef\(0\);/);
  assert.match(page, /const ownsRun = \(\) =>[\s\S]*askRunEpochRef\.current === runEpoch[\s\S]*workspaceEpochRef\.current === workspaceEpoch/);
  assert.match(page, /if \(!ownsRun\(\)\) return;[\s\S]*setTurns/);
});

test("logout aborts local work and remounts the authenticated application", () => {
  const start = page.indexOf("async function handleLogout() {");
  const end = page.indexOf("\n  }", start);
  const body = page.slice(start, end);
  assert.ok(body.includes("askAbortRef.current?.abort()"));
  assert.ok(body.includes("setCurrentUser(null)"));
  assert.ok(body.includes("window.location.reload()"));
});

test("shared notebook transitions always use the atomic notebook opener", () => {
  const copyStart = page.indexOf("async function handleCopyShared");
  const leaveEnd = page.indexOf("// E. owner", copyStart);
  const transitions = page.slice(copyStart, leaveEnd);
  assert.ok(transitions.includes("await openNotebook(String(created.id))"));
  assert.ok(transitions.includes("await openNotebook(String(joined.id))"));
  assert.equal(transitions.includes("setCurrentNotebookId(String(created.id))"), false);
  assert.equal(transitions.includes("setCurrentNotebookId(String(joined.id))"), false);
});

test("frontend capabilities mirror backend write and admin boundaries", () => {
  assert.match(page, /const capabilities = \{[\s\S]*canWriteNotebook: !isReader[\s\S]*canManageSchemas: currentUser\?\.role === "admin"/);
  assert.match(page, /readOnly=\{!capabilities\.canGovernKnowledge\}/);
  assert.match(page, /readOnly=\{!capabilities\.canManageReports\}/);
  assert.match(page, /schemaModalOpen && capabilities\.canManageSchemas/);
});

test("opening a notebook restores its most recent conversation instead of a blank one", () => {
  // loadSessions 必须把列表交回给调用方,否则 openNotebook 无从知道该恢复哪条。
  assert.match(page, /async function loadSessions\([\s\S]*?\): Promise<ConversationSummary\[\] \| null> \{/);

  // 会话详情的灌入内核必须独立于 openSession 存在,且自己不碰 epoch——
  // openSession 第一行就 ++workspaceEpochRef.current,openNotebook 复用它会自撞守卫。
  const applyStart = page.indexOf("async function applySessionDetail(");
  assert.ok(applyStart > -1, "applySessionDetail 必须存在");
  const applyEnd = page.indexOf("\n  }\n", applyStart);
  const applyBody = page.slice(applyStart, applyEnd);
  assert.equal(applyBody.includes("++workspaceEpochRef.current"), false);
  assert.equal(applyBody.includes("workspaceEpochRef.current +="), false);
  assert.ok(applyBody.includes("setConversationId(id);"));
  assert.ok(applyBody.includes("detail.active_job"), "在途 job 重连必须留在内核里,恢复时才能接上");

  // openSession 只负责推进 epoch + 清场,详情灌入委派给内核(零重复)。
  const openSessionStart = page.indexOf("async function openSession(id: string)");
  const openSessionEnd = page.indexOf("\n  }\n", openSessionStart);
  const openSessionBody = page.slice(openSessionStart, openSessionEnd);
  assert.ok(openSessionBody.includes("++workspaceEpochRef.current"));
  assert.match(openSessionBody, /await applySessionDetail\(id, workspaceEpoch\)/);
  assert.equal(openSessionBody.includes("api<ConversationDetail>"), false, "详情请求不该在 openSession 里重复");

  // openNotebook 用自己的 epoch 恢复最近一条,不新开 epoch。
  const openNotebookStart = page.indexOf("async function openNotebook(notebookId: string");
  const openNotebookEnd = page.indexOf("\n  }\n", openNotebookStart);
  const openNotebookBody = page.slice(openNotebookStart, openNotebookEnd);
  assert.match(openNotebookBody, /const sessionList = await loadSessions\(notebookId, workspaceEpoch\);/);
  assert.match(openNotebookBody, /await applySessionDetail\(sessionList\[0\]\.id, workspaceEpoch\)/);
  assert.equal(openNotebookBody.includes("await openSession("), false, "复用 openSession 会自撞 epoch 守卫");
});
