"use client";

import { lazy, Suspense, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Maximize2, MessageSquare, Minimize2, Minus } from "lucide-react";
import type { RootModalCoordinator } from "../use-root-modal-coordinator";
import { citationPopoverHoldsEscape } from "../citation-card";
import { globalAskLayerHoldsEscape } from "./global-ask-escape";
import type { UiMode } from "../ui-mode";
import type { AnswerImagePreviewRequest } from "../image-preview";
import "./global-ask-launcher.css";
import "./global-ask.css";

const GlobalAskWorkspace = lazy(() => import("./global-ask-workspace"));

type Presentation = Pick<RootModalCoordinator, "view" | "open" | "requestClose" | "captureActorOwner">;

/** 宿主(笔记本页)要求打开某个全局会话——目前只有铃铛里「进行中的提问」的全局
 *  条目会发,`jobId` 是那条在途作业(浮窗据此判断眼前的轮次是否已含它)。`nonce`
 *  每次点击都换新值:同一个会话点两次也是两次独立的导航意图。 */
export type GlobalConversationRequest = { conversationId: string; jobId?: string; nonce: number };

/** 首次提示「看过了」按用户记在本机;读写失败(私密模式等)按没看过/写不进处理,
 *  最坏只是提示再出现一次。 */
export const discoveryHintStorageKey = (userId: string) => `globalAsk.discoveryHintSeen.${userId}`;
function hintSeen(key: string) {
  try { return window.localStorage.getItem(key) === "1"; } catch { return false; }
}
function rememberHintSeen(key: string) {
  try { window.localStorage.setItem(key, "1"); } catch { /* ignore */ }
}

export function GlobalAskLauncher({ presentation, uiMode, openRequest, discoveryHint, onPreviewImage, imagePreviewOpen }: {
  presentation: Presentation; uiMode?: UiMode; openRequest?: GlobalConversationRequest | null;
  /** 引用配图点开：宿主把同一格页面级预览作为子层叠在本窗口之上（协调器的 `stacksOver`）。
   *  预览盖住窗口时 `view.topmost` 转假，窗口按既有规则退出交互树、全屏态降成非模态
   *  ——Esc 因此先归预览，这里不需要为它写任何判断。不传则图片只能原位看。 */
  onPreviewImage?: (request: AnswerImagePreviewRequest) => void;
  /** 预览此刻开着：引用卡在它关闭之前不收起，焦点才回得到那张缩略图。 */
  imagePreviewOpen?: boolean;
  /** 集合页传 `{ userId, active: true }`:用户第一次看到气泡时在旁边说一句它能做什么。
   *  点「知道了」、打开全局问答、或离开集合页(active 变 false)都算看过,之后不再出现。 */
  discoveryHint?: { userId: string; active: boolean };
}) {
  const [expanded, setExpanded] = useState(false);
  const [started, setStarted] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const launcher = useRef<HTMLButtonElement>(null);
  // 真正交给工作区的那一份请求:只有窗口确实打开了才转交。窗口被更上层的弹窗挡住、
  // 没能打开时不转交——否则用户之后自己点开浮窗,会被一条早已过时的点击甩到那个会话。
  const [forwarded, setForwarded] = useState<GlobalConversationRequest | null>(null);
  const view = presentation.view("global-ask");
  const open = view.open;
  const hintKey = discoveryHint ? discoveryHintStorageKey(discoveryHint.userId) : null;
  // 首屏渲染不读 localStorage(服务端没有它);挂载后再决定要不要显示。
  const [hintPending, setHintPending] = useState(false);
  useEffect(() => { if (hintKey) setHintPending(!hintSeen(hintKey)); }, [hintKey]);
  const hintActive = Boolean(discoveryHint?.active);
  const dismissHint = () => {
    if (!hintPending || !hintKey) return;
    rememberHintSeen(hintKey);
    setHintPending(false);
  };
  // 提示出现过之后离开集合页(进笔记本)就算看过;一次都没出现过的不算。
  const hintShown = useRef(false);
  const showHint = hintPending && hintActive && !open;
  if (showHint) hintShown.current = true;
  useEffect(() => {
    if (!hintActive && hintShown.current) dismissHint();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hintActive]);
  const mode = !open ? "closed" : expanded ? "full" : "compact";
  function setMode(next: "closed" | "compact" | "full") {
    if (next === "closed") { presentation.requestClose("global-ask", "button"); return; }
    if (!open) {
      launcher.current?.focus();
      if (!presentation.open("global-ask", presentation.captureActorOwner())) return;
    }
    dismissHint();
    setStarted(true);
    setExpanded(next === "full");
  }

  useEffect(() => {
    if (!openRequest) return;
    if (!open) {
      launcher.current?.focus();
      if (!presentation.open("global-ask", presentation.captureActorOwner())) return;
      setExpanded(false);
    }
    dismissHint();
    setStarted(true);
    setForwarded(openRequest);
    // 只对新的一次点击(nonce)起反应;open/presentation 的变化不是新的导航意图。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openRequest?.nonce]);

  useLayoutEffect(() => {
    const node = dialog.current;
    if (!node) return;
    const active = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const focused = active && node.contains(active) ? active : null;
    // 盖在窗口之上的那一层（大图预览、提示框……）此刻持有的焦点。`show()` 会跑 dialog
    // focusing steps，把焦点挪进本窗口——而窗口此刻是 inert，焦点于是掉到 body，那一层
    // 的 Esc / 键盘操作全部失灵。被盖住时把它原样还回去。
    const covering = !view.topmost && active && !focused && active !== document.body ? active : null;
    // Native modal mode supplies focus containment and an inert background in full screen.
    if (node.open) node.close();
    if (mode === "full" && view.topmost) node.showModal();
    else if (mode !== "closed") node.show();
    if (mode !== "closed" && view.topmost) (focused ?? node.querySelector<HTMLButtonElement>("button"))?.focus();
    else if (covering?.isConnected && document.activeElement !== covering) covering.focus({ preventScroll: true });
    if (mode !== "full" || !view.topmost) return;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { document.body.style.overflow = overflow; };
  }, [mode, view.topmost]);

  // 全屏窗口被盖住时降成了非模态（否则 top layer 会把盖上来的页面级弹层整个压住），
  // 原生 showModal 给的「其余页面 inert」也随之没了：Tab 会把焦点送进被全屏窗口挡住、
  // 根本看不见的页面元素。被盖住期间焦点只许留在**盖上来的那一层**里；跑出去就拉回
  // 刚才所在的那个元素。只对全屏态：小窗本来就不挡页面，与笔记本内弹层一样不设焦点陷阱。
  //
  // 「盖上来的那一层」认的是协调器此刻的最上层落地的地方，而不是全文档任意一个
  // `aria-modal`：被挡住的页面里也可能有写死 aria-modal 的整页视图。协调器只管租约、
  // 不持有 DOM，所以按事实认：窗口被盖住之后，第一个落在窗口之外的焦点就是最上层
  // 那一层取得的（打开它的那次 autoFocus / 补聚焦；此刻只有它可交互，下面各层都
  // inert），它所在的 `aria-modal` 容器就是允许的范围。
  useLayoutEffect(() => {
    const node = dialog.current;
    if (!node || mode !== "full" || view.topmost) return;
    let layer: HTMLElement | null = null;
    let last: HTMLElement | null = null;
    const admit = (element: HTMLElement) => {
      if (!layer) layer = element.closest<HTMLElement>('[aria-modal="true"]');
      if (!layer?.contains(element)) return false;
      last = element;
      return true;
    };
    const active = document.activeElement;
    if (active instanceof HTMLElement && active !== document.body && !node.contains(active)) admit(active);
    const onFocusIn = (event: FocusEvent) => {
      const target = event.target;
      if (!(target instanceof HTMLElement) || node.contains(target)) return;
      if (admit(target)) return;
      if (last?.isConnected) last.focus({ preventScroll: true });
    };
    document.addEventListener("focusin", onFocusIn);
    return () => document.removeEventListener("focusin", onFocusIn);
  }, [mode, view.topmost]);

  return <>
    <button ref={launcher} className="global-ask-bubble" aria-label="打开全局问答" title="全局问答" aria-expanded={open} aria-controls="global-ask-window" hidden={open} onClick={() => { setStarted(true); setMode("compact"); }}><MessageSquare size={20} strokeWidth={1.8} aria-hidden /><span className="global-bubble-label" aria-hidden>全局问答</span></button>
    {showHint && <aside className="global-ask-hint" role="note" aria-label="全局问答提示">
      <p><strong>跨笔记本提问</strong>一次问遍你的全部笔记本，回答会注明出处。</p>
      <button className="global-ask-hint-dismiss" onClick={dismissHint}>知道了</button>
    </aside>}
    <dialog ref={dialog} id="global-ask-window" className={`global-ask-window ${mode === "full" ? "is-full" : "is-compact"}`} style={{ zIndex: view.zIndex }} inert={open && !view.topmost ? true : undefined} aria-hidden={open && !view.topmost ? true : undefined} aria-label="全局问答"
      // 引用小卡片与会话分享弹窗都在 window 捕获期就把 Esc 吃掉了（preventDefault +
      // stopPropagation），所以下面那个 onKeyDown 在有内层弹层时根本不会触发。剩下的
      // 缺口只有原生 <dialog> 的 close request：捕获期 preventDefault 能否掐掉它各
      // 浏览器不一致，这里读内层自己报出的「我正接管 Esc」兜一道，避免「内层关了、
      // 窗口也跟着关」。
      onCancel={(event) => { event.preventDefault(); if (citationPopoverHoldsEscape() || globalAskLayerHoldsEscape()) return; presentation.requestClose("global-ask", "escape"); }}
      onKeyDown={(event) => { if (event.key === "Escape" && !event.defaultPrevented) { event.preventDefault(); presentation.requestClose("global-ask", "escape"); } }}>
      {started && <Suspense fallback={<div className="global-window-loading" role="status">正在打开全局问答…<button className="sort-button" onClick={() => setMode("closed")}>收起</button></div>}>
        <GlobalAskWorkspace embedded uiMode={uiMode} active={open} compact={mode !== "full"} requestedConversation={forwarded} onOpenNotebook={() => setMode("closed")} onPreviewImage={onPreviewImage} imagePreviewOpen={imagePreviewOpen} controls={<>
          <button autoFocus className="icon-button" aria-label={mode === "full" ? "退出全屏" : "全屏展开"} title={mode === "full" ? "退出全屏" : "全屏展开"} onClick={() => setMode(mode === "full" ? "compact" : "full")}>{mode === "full" ? <Minimize2 size={17} /> : <Maximize2 size={17} />}</button>
          <button className="icon-button" aria-label="收起全局问答" title="收起，保留当前对话" onClick={() => setMode("closed")}><Minus size={19} /></button>
        </>} />
      </Suspense>}
    </dialog>
  </>;
}
