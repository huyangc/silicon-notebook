"use client";

import { useEffect, useRef, useState } from "react";

import { toUserMessage } from "./errors.ts";
import { collaboratorName, listCollaborators, type EditorName } from "./notebook-collaborators-api.ts";

type RosterState =
  | { kind: "loading" }
  | { kind: "ready"; editors: EditorName[] }
  | { kind: "failed"; text: string };

/**
 * 点名编辑者顶栏上的身份徽章「可编辑 · 来自 X」。点开列出这本笔记本的全部编辑者(决策 D17:
 * 持有编辑授权的人看得到其他编辑者的用户名与显示名,`GET .../collaborators` 的 editor 视图)。
 *
 * 徽章本身仍戴 `.reader-badge-chip`(顶栏单行版式的有界槽,见 reader-badge-layout-guard),只是
 * 换成 `<button>`。名单浮层是 fixed 定位的 `.popover`,按徽章此刻的位置摆在它正下方——顶栏
 * 固定 72px,在它里面开下拉会被裁掉。每次打开都现取一次:名单由库主随时增减。
 */
export function EditorRosterChip({
  notebookId,
  label,
  title,
}: {
  notebookId: string;
  label: string;
  title: string;
}) {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState({ left: 0, top: 0 });
  const [roster, setRoster] = useState<RosterState>({ kind: "loading" });
  const chipRef = useRef<HTMLButtonElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const requestRef = useRef(0);

  useEffect(() => {
    if (!open) return undefined;
    const request = ++requestRef.current;
    setRoster({ kind: "loading" });
    listCollaborators(notebookId)
      .then((list) => {
        if (requestRef.current === request) setRoster({ kind: "ready", editors: list.editors });
      })
      .catch((error) => {
        if (requestRef.current === request) {
          setRoster({ kind: "failed", text: toUserMessage(error, "编辑者名单暂时读不到，请稍后重试") });
        }
      });
    const onPointer = (event: MouseEvent) => {
      const target = event.target as Node;
      if (chipRef.current?.contains(target) || popoverRef.current?.contains(target)) return;
      setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
        chipRef.current?.focus();
      }
    };
    document.addEventListener("mousedown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      requestRef.current += 1;
      document.removeEventListener("mousedown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, notebookId]);

  return (
    <>
      <button
        ref={chipRef}
        type="button"
        className="reader-badge-chip reader-badge-chip-button"
        title={title}
        aria-expanded={open}
        aria-haspopup="dialog"
        onClick={() => {
          const rect = chipRef.current?.getBoundingClientRect();
          if (rect) setPosition({ left: rect.left, top: rect.bottom + 6 });
          setOpen((value) => !value);
        }}
      >
        {label}
      </button>
      {open && (
        <div
          ref={popoverRef}
          className="popover editor-roster-popover"
          role="dialog"
          aria-label="这本笔记本的编辑者"
          style={{ left: position.left, top: position.top }}
        >
          <span className="section-title">这本笔记本的编辑者</span>
          {roster.kind === "loading" && <p className="tool-hint">加载中…</p>}
          {roster.kind === "failed" && <p className="password-change-status error">{roster.text}</p>}
          {roster.kind === "ready" && (roster.editors.length === 0
            ? <p className="tool-hint">还没有编辑者。</p>
            : (
              <ul className="editor-roster-list">
                {roster.editors.map((editor) => <li key={editor.username}>{collaboratorName(editor)}</li>)}
              </ul>
            ))}
        </div>
      )}
    </>
  );
}
