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
  const openNotebookStart = page.indexOf("async function openNotebook(notebookId: string");
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
  assert.equal(openSessionBody.includes("api<ConversationDetail>"), false, "详情请求只应存在于 applySessionDetail 内核里,openSession 必须委派而非自己再发一份");

  // openNotebook 用自己的 epoch 恢复最近一条,不新开 epoch。
  const openNotebookStart = page.indexOf("async function openNotebook(notebookId: string");
  const openNotebookEnd = page.indexOf("\n  }\n", openNotebookStart);
  const openNotebookBody = page.slice(openNotebookStart, openNotebookEnd);
  assert.match(openNotebookBody, /const sessionList = await loadSessions\(notebookId, workspaceEpoch\);/);
  assert.match(openNotebookBody, /await applySessionDetail\(sessionList\[0\]\.id, workspaceEpoch\)/);
  assert.equal(openNotebookBody.includes("await openSession("), false, "复用 openSession 会自撞 epoch 守卫");
});

test("openNotebook's automatic restore degrades gracefully instead of blocking the notebook from opening", () => {
  const openNotebookStart = page.indexOf("async function openNotebook(notebookId: string");
  const openNotebookEnd = page.indexOf("\n  }\n", openNotebookStart);
  assert.ok(openNotebookStart > -1);
  const openNotebookBody = page.slice(openNotebookStart, openNotebookEnd);

  // 恢复调用必须裹在 try/catch 里:它是无条件自动发生的,api() 对非 2xx 会 throw,
  // 不 catch 就会让异常冒出 openNotebook,导致该 notebook(没有任何变通手段)彻底打不开。
  const tryStart = openNotebookBody.indexOf(
    "try {\n        await applySessionDetail(sessionList[0].id, workspaceEpoch);\n      } catch {"
  );
  assert.ok(tryStart > -1, "恢复调用必须被 try/catch 包裹,而不是让 applySessionDetail 的异常直接冒出 openNotebook");

  const catchBraceIndex = openNotebookBody.indexOf("} catch {", tryStart);
  const catchEnd = openNotebookBody.indexOf("\n      }\n", catchBraceIndex);
  assert.ok(catchEnd > catchBraceIndex);
  const catchBody = openNotebookBody.slice(catchBraceIndex, catchEnd);

  // catch 分支不能提前 return——那样恢复失败时 openNotebook 仍会被短路掉,等于没修。
  assert.equal(catchBody.includes("return"), false, "catch 分支不能提前 return,否则恢复失败仍会阻塞 notebook 打开");
  // 降级后的状态就是本改动前的正常空白新会话状态,不是错误状态,不该弹 toast/报错。
  assert.equal(catchBody.includes("reportError"), false, "恢复失败是优雅降级,不是错误,不该报错给用户");
  assert.equal(catchBody.includes("setStatusText"), false, "恢复失败是优雅降级,不是错误,不该报错给用户");

  // catch 之后,epoch 守卫与开库收尾(写 URL hash、return true)必须继续执行——
  // 证明 try/catch 只吞了恢复失败这一步,没有连带吞掉函数剩下的逻辑,notebook 正常打开。
  // 写 URL hash 那行在 Task 3(history 三件套)中从无条件 replaceState 改为
  // "push" 时才 pushState——断言随之更新为字面新形式,守卫意图(hash 必须写)不变。
  const afterCatch = openNotebookBody.slice(catchEnd);
  assert.match(afterCatch, /if \(workspaceEpochRef\.current !== workspaceEpoch\) return false;/);
  assert.match(afterCatch, /if \(history === "push"\) \{\s*window\.history\.pushState\(null, "", notebookHash\(notebookId\)\);/);
  assert.match(afterCatch, /return true;/);
});

test("entering a notebook pushes history so the browser back button leaves it", () => {
  assert.match(page, /async function openNotebook\(\s*notebookId: string,\s*history: "push" \| "none" = "push",?\s*\): Promise<boolean>/);

  const openNotebookStart = page.indexOf("async function openNotebook(notebookId: string");
  const openNotebookEnd = page.indexOf("\n  }\n", openNotebookStart);
  const openNotebookBody = page.slice(openNotebookStart, openNotebookEnd);
  assert.match(openNotebookBody, /if \(history === "push"\) \{\s*window\.history\.pushState\(null, "", notebookHash\(notebookId\)\);/);
  assert.equal(openNotebookBody.includes("window.history.replaceState"), false, "进 notebook 改用 pushState");

  // 默认必须是 push,否则所有既有调用点(卡片/列表/新建/分享拷贝)都不进历史栈。
  // 既有调用点一律不带第二参,靠默认值——它们的字面形式被 workspace-layout.test.mjs
  // 的 "shared notebook transitions" 断言锁着,不许改。
  assert.match(page, /await openNotebook\(String\(created\.id\)\)/);
  assert.match(page, /await openNotebook\(String\(joined\.id\)\)/);
});

test("openNotebookMemory keeps its signature and its own history write", () => {
  // memory-navigation.test.mjs:46 按字面签名锁死了它,不能加参数。
  assert.match(page, /async function openNotebookMemory\(notebookId: string\) \{/);
  const start = page.indexOf("async function openNotebookMemory(notebookId: string)");
  const end = page.indexOf("\n  }\n", start);
  const body = page.slice(start, end);
  assert.match(body, /await openNotebook\(notebookId, "none"\)/);
  assert.match(body, /window\.history\.replaceState\(null, "", memoryHash\(notebookId\)\)/);
});

test("browser back and refresh both restore the notebook workspace", () => {
  // popstate:hash 变了要跟着切视图,且不能再写 history(浏览器已经改过 URL 了)。
  assert.match(page, /window\.addEventListener\("popstate", onPopState\)/);
  assert.match(page, /window\.removeEventListener\("popstate", onPopState\)/);

  const popStart = page.indexOf("function onPopState()");
  const popEnd = page.indexOf("\n    }\n", popStart);
  const popBody = page.slice(popStart, popEnd);
  assert.ok(popBody.includes('openNotebook(workspace.notebookId, "none")'));
  assert.ok(popBody.includes("showCollection();"));

  // 挂载:#notebook=<id>(不带 tab=memory)要能还原到工作区。
  const mountStart = page.indexOf("const target = parseMemoryHash(window.location.hash);");
  const mountEnd = page.indexOf("\n      })\n      .catch(() => { clearToken(); })", mountStart);
  const mountBlock = page.slice(mountStart, mountEnd);
  assert.ok(mountBlock.includes("parseWorkspaceHash(window.location.hash)"));
  assert.ok(mountBlock.includes('openNotebook(workspace.notebookId, "none")'));
});

test("the workspace exit is a labelled back control, not a bare brand mark", () => {
  // 裸 SN 方块是用户明确反馈「太抽象」的那个控件。
  assert.equal(page.includes('<button className="notebook-home" onClick={showCollection}>SN</button>'), false);
  assert.match(page, /className="notebook-home"[\s\S]{0,220}<ArrowLeft size=\{16\} \/>[\s\S]{0,80}<span>返回主页<\/span>/);
  assert.match(page, /onClick=\{\(\) => showCollection\(\)\}/);
  assert.match(page, /^import \{[^}]*\bArrowLeft\b/m);

  // 从固定 46px 方块变成自适应宽度的胶囊,文字不能被压断。
  assert.match(css, /\.notebook-home\s*{[^}]*white-space:\s*nowrap;/s);
  assert.match(css, /\.notebook-home\s*{[^}]*flex:\s*0 0 auto;/s);
  assert.doesNotMatch(css, /\.notebook-home\s*{[^}]*width:\s*46px;/s);
});

// --- 最终整支审查修复:五条发现 --------------------------------------------

test("popstate degrades gracefully instead of leaving a hidden topbar error when a deep link is stale", () => {
  // 工作区模式下整个 topbar 被 CSS 隐藏(globals.css: .app.workspace-mode .topbar
  // { display: none; }),而 reportError 只会把消息写进 statusText,渲染在 topbar
  // 里——用户什么都看不到,且 openNotebook 失败会把 activeNotebookIdRef 卡在 null
  // 不恢复,静默关掉来源轮询与会话列表刷新。两个分支都必须走与挂载还原分支
  // (parseMemoryHash/parseWorkspaceHash 那段,见上一条测试)同款的优雅降级。
  const popStart = page.indexOf("function onPopState()");
  const popEnd = page.indexOf("\n    }\n", popStart);
  assert.ok(popStart > -1);
  const popBody = page.slice(popStart, popEnd);

  assert.equal(
    popBody.includes(".catch(reportError)"),
    false,
    "popstate 失败必须优雅降级,不能走裸 reportError(工作区模式下 topbar 不可见,用户看不到)",
  );
  assert.match(
    popBody,
    /openNotebookMemory\(memory\.notebookId\)\.catch\(\(\) => \{\s*showCollection\(\);\s*setToast\("Memory 深链接不可用或已失效"\);\s*\}\)/,
  );
  assert.match(
    popBody,
    /openNotebook\(workspace\.notebookId, "none"\)\.catch\(\(\) => \{\s*showCollection\(\);\s*setToast\("笔记本链接不可用或已失效"\);\s*\}\)/,
  );
});

test("leaving a shared notebook replaces the stale deep link instead of pushing a new history entry", () => {
  // 退出只读共享后必须传 "none" 且自己 replaceState 顶替被退出的 #notebook=<leftId>——
  // 改动前是 replaceState,不存在这问题;默认 "push" 会让那条历史条目存活,
  // 用户按返回键就会撞上已经 403 的旧笔记本(配合上一条测试修的静默失败会甩出裸报错)。
  const start = page.indexOf("async function handleLeaveShared() {");
  const end = page.indexOf("\n  }", start);
  assert.ok(start > -1);
  const body = page.slice(start, end);

  assert.equal(
    body.includes("await openNotebook(firstOwned.id);"),
    false,
    "退出共享后不能再用默认 push,会让被退出的旧 #notebook=<id> 历史条目存活",
  );
  assert.match(body, /await openNotebook\(firstOwned\.id, "none"\)/);
  assert.match(body, /window\.history\.replaceState\(null, "", notebookHash\(firstOwned\.id\)\)/);
});

test("the workspace back button's tooltip does not fight its own visible label", () => {
  // title="返回笔记本列表" 与可见文字「返回主页」不一致。按钮已有可见文字,
  // title 是纯冗余(也不参与可访问名计算,可访问名取自可见 span),删掉零 a11y 影响。
  assert.equal(page.includes('title="返回笔记本列表"'), false);
});

test("switchChatMode reuses notebookHash instead of hand-rolling the same hash syntax", () => {
  // memory-model.ts 里 notebookHash/memoryHash 住同一个文件就是为了不让两条
  // hash 的语法拆开漂移;switchChatMode 手搓的模板字符串是唯一漏扫的站点。
  const start = page.indexOf('function switchChatMode(mode: ChatMode) {');
  const end = page.indexOf("\n  }", start);
  assert.ok(start > -1);
  const body = page.slice(start, end);

  assert.equal(body.includes("`#notebook=${encodeURIComponent(currentNotebookId)}`"), false);
  assert.match(body, /mode === "memory" \? memoryHash\(currentNotebookId\) : notebookHash\(currentNotebookId\)/);
});

test("opening a completed-item toast replaces history when already in that notebook, else pushes", () => {
  // PendingToast 是 position: fixed,工作区模式下(topbar 被隐藏)仍然可见可点。
  // 若用户已经在通知所属的笔记本里,默认 "push" 会压入一条重复的 #notebook=<同一 id>;
  // 按返回键触发 openNotebook(id, "none") 全量重载,把用户从当前视图甩回 ask 聊天,
  // URL 却没变。判据用 activeNotebookIdRef.current 而非 currentNotebookId state,
  // 因为要在 openNotebook 把 ref 清空之前拿到「导航前」的值。
  const start = page.indexOf("async function openDoneItem(");
  const end = page.indexOf("\n  }", start);
  assert.ok(start > -1);
  const body = page.slice(start, end);
  assert.match(body, /activeNotebookIdRef\.current === d\.notebook_id/);
  assert.match(body, /openNotebook\(d\.notebook_id, history\)/);

  // openPendingItem(铃铛入口)必须保持不变:它只在集合页可点(工作区模式下
  // topbar 被 CSS 隐藏,铃铛在 topbar 里),那里的默认 push 本来就对——改成条件性
  // 反而会让 URL 停在 / 却显示着笔记本,打断刷新深链和返回键。
  const pendingStart = page.indexOf("async function openPendingItem(");
  const pendingEnd = page.indexOf("\n  }", pendingStart);
  assert.ok(pendingStart > -1);
  const pendingBody = page.slice(pendingStart, pendingEnd);
  assert.match(pendingBody, /if \(!await openNotebook\(item\.notebook_id\)\) return;/);
  assert.equal(pendingBody.includes("activeNotebookIdRef.current"), false);
});
