"use client";

import { useEffect, useRef, useState } from "react";

import { FloatingModalCard } from "./floating-modal-card.tsx";
import { ReleaseNoteBody } from "./release-note-body.tsx";
import { markReleaseNotesSeen, type ReleaseNote, type ReleaseNotesBuild } from "./release-notes-api.ts";
import { RELEASE_NOTE_LEVEL_LABELS } from "./release-notes-model.ts";

type ReleaseNotesModalProps = {
  build: ReleaseNotesBuild;
  notes: ReleaseNote[];
  /** 没放进 notes 的修复与改进条数。 */
  moreCount?: number;
  onClose: () => void;
  interactive?: boolean;
  zIndex?: number;
};

/**
 * 「系统已更新」弹窗。骨架镜像 PasswordChangeModal。任何关闭入口（×、知道了）都
 * 立即关闭，并把「已看到 build.ordinal 为止」fire-and-forget 上报一次；上报失败
 * 静默——下次打开页面会再弹一次，这是可接受的退路。关闭入口不受任何忙态禁用。
 * 每条只显示级别与标题,有正文的点标题展开;其余修复与改进收在「更新记录」页。
 */
export function ReleaseNotesModal({ build, notes, moreCount = 0, onClose, interactive = true, zIndex }: ReleaseNotesModalProps) {
  const reportedRef = useRef(false);
  const confirmRef = useRef<HTMLButtonElement>(null);
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());

  function toggle(id: string) {
    setExpanded((current) => {
      const next = new Set(current);
      if (!next.delete(id)) next.add(id);
      return next;
    });
  }

  // 登录后异步冒出来的弹窗要接住焦点,否则按键还在被它盖住的输入框里;
  // 关闭时的焦点归还由协调器负责。
  useEffect(() => { confirmRef.current?.focus(); }, []);

  function close() {
    if (!reportedRef.current) {
      reportedRef.current = true;
      try {
        markReleaseNotesSeen(build.ordinal).catch(() => undefined);
      } catch {
        // 同步抛出也不许挡住关闭。
      }
    }
    onClose();
  }

  return (
    <section className="utility-modal" role="dialog" aria-modal={interactive} aria-hidden={!interactive} inert={interactive ? undefined : true} style={{ zIndex }}>
      <FloatingModalCard storageKey="releaseNotes.window" className="utility-modal-card narrow release-notes-card">
        {(floating) => (<>
        <div className="source-modal-header" {...floating.dragHandleProps}>
          <div>
            <h2>系统已更新</h2>
            <p>当前版本 {build.version}</p>
          </div>
          <button className="icon-button" onClick={close} title="关闭">×</button>
        </div>
        <ul className="release-notes-list">
          {notes.map((note) => {
            const open = expanded.has(note.id);
            const heading = (<>
              <span className={`release-notes-level ${note.level}`}>{RELEASE_NOTE_LEVEL_LABELS[note.level]}</span>
              <strong>{note.title}</strong>
            </>);
            return (
              <li key={note.id} className="release-notes-item">
                {note.body ? (
                  <button type="button" className="release-notes-toggle" aria-expanded={open} onClick={() => toggle(note.id)}>
                    {heading}
                    <span className="release-notes-caret" aria-hidden="true">{open ? "▾" : "▸"}</span>
                  </button>
                ) : (
                  <div className="release-notes-toggle static">{heading}</div>
                )}
                {note.body && open && <div className="release-notes-body"><ReleaseNoteBody body={note.body} /></div>}
              </li>
            );
          })}
        </ul>
        <p className="release-notes-more">
          {moreCount > 0 ? `另有 ${moreCount} 项修复与改进，` : ""}
          <a href="/updates" target="_blank" rel="noreferrer">{moreCount > 0 ? "查看更新记录" : "查看全部更新记录"}</a>
        </p>
        <div className="modal-actions padded">
          <button ref={confirmRef} type="button" className="new-pill" onClick={close}>知道了</button>
        </div>
        </>)}
      </FloatingModalCard>
    </section>
  );
}
