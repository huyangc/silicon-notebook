"use client";

import { lazy, Suspense, useLayoutEffect, useRef, useState } from "react";
import { Maximize2, MessageSquare, Minimize2, Minus } from "lucide-react";
import type { RootModalCoordinator } from "../use-root-modal-coordinator";
import "./global-ask-launcher.css";
import "./global-ask.css";

const GlobalAskWorkspace = lazy(() => import("./global-ask-workspace"));

type Presentation = Pick<RootModalCoordinator, "view" | "open" | "requestClose" | "captureActorOwner">;

export function GlobalAskLauncher({ presentation }: { presentation: Presentation }) {
  const [expanded, setExpanded] = useState(false);
  const [started, setStarted] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const launcher = useRef<HTMLButtonElement>(null);
  const view = presentation.view("global-ask");
  const open = view.open;
  const mode = !open ? "closed" : expanded ? "full" : "compact";
  function setMode(next: "closed" | "compact" | "full") {
    if (next === "closed") { presentation.requestClose("global-ask", "button"); return; }
    if (!open) {
      launcher.current?.focus();
      if (!presentation.open("global-ask", presentation.captureActorOwner())) return;
    }
    setStarted(true);
    setExpanded(next === "full");
  }

  useLayoutEffect(() => {
    const node = dialog.current;
    if (!node) return;
    const focused = node.contains(document.activeElement) ? document.activeElement as HTMLElement : null;
    // Native modal mode supplies focus containment and an inert background in full screen.
    if (node.open) node.close();
    if (mode === "full" && view.topmost) node.showModal();
    else if (mode !== "closed") node.show();
    if (mode !== "closed" && view.topmost) (focused ?? node.querySelector<HTMLButtonElement>("button"))?.focus();
    if (mode !== "full" || !view.topmost) return;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { document.body.style.overflow = overflow; };
  }, [mode, view.topmost]);

  return <>
    <button ref={launcher} className="global-ask-bubble" aria-label="打开全局问答" title="全局问答" aria-expanded={open} aria-controls="global-ask-window" hidden={open} onClick={() => { setStarted(true); setMode("compact"); }}><MessageSquare size={25} strokeWidth={1.7} /><span className="global-bubble-label">全局问答</span></button>
    <dialog ref={dialog} id="global-ask-window" className={`global-ask-window ${mode === "full" ? "is-full" : "is-compact"}`} style={{ zIndex: view.zIndex }} inert={open && !view.topmost ? true : undefined} aria-hidden={open && !view.topmost ? true : undefined} aria-label="全局问答" onCancel={(event) => { event.preventDefault(); presentation.requestClose("global-ask", "escape"); }} onKeyDown={(event) => { if (event.key === "Escape" && !event.defaultPrevented) { event.preventDefault(); presentation.requestClose("global-ask", "escape"); } }}>
      {started && <Suspense fallback={<div className="global-window-loading" role="status">正在打开全局问答…<button className="sort-button" onClick={() => setMode("closed")}>收起</button></div>}>
        <GlobalAskWorkspace embedded active={open} compact={mode !== "full"} onOpenNotebook={() => setMode("closed")} controls={<>
          <button autoFocus className="icon-button" aria-label={mode === "full" ? "退出全屏" : "全屏展开"} title={mode === "full" ? "退出全屏" : "全屏展开"} onClick={() => setMode(mode === "full" ? "compact" : "full")}>{mode === "full" ? <Minimize2 size={17} /> : <Maximize2 size={17} />}</button>
          <button className="icon-button" aria-label="收起全局问答" title="收起，保留当前对话" onClick={() => setMode("closed")}><Minus size={19} /></button>
        </>} />
      </Suspense>}
    </dialog>
  </>;
}
