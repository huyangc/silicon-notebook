"use client";

import { useEffect, useRef, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";

import { FloatingModalCard } from "./floating-modal-card.tsx";
import { remarkGfmPlugin } from "./markdown-gfm";
import { markReleaseNotesSeen, type ReleaseNote, type ReleaseNotesBuild } from "./release-notes-api.ts";

// 说明里的链接一律新标签页打开:在当前页跳走会让 close() 没机会跑、丢掉工作区状态。
const markdownComponents = {
  a({ href, children }: { href?: string; children?: ReactNode }) {
    return <a href={href} target="_blank" rel="noreferrer">{children}</a>;
  },
} as Parameters<typeof ReactMarkdown>[0]["components"];

type ReleaseNotesModalProps = {
  build: ReleaseNotesBuild;
  notes: ReleaseNote[];
  onClose: () => void;
  interactive?: boolean;
  zIndex?: number;
};

/**
 * 「系统已更新」弹窗。骨架镜像 PasswordChangeModal。任何关闭入口（×、知道了）都
 * 立即关闭，并把「已看到 build.ordinal 为止」fire-and-forget 上报一次；上报失败
 * 静默——下次打开页面会再弹一次，这是可接受的退路。关闭入口不受任何忙态禁用。
 */
export function ReleaseNotesModal({ build, notes, onClose, interactive = true, zIndex }: ReleaseNotesModalProps) {
  const reportedRef = useRef(false);
  const confirmRef = useRef<HTMLButtonElement>(null);

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
          {notes.map((note) => (
            <li key={note.id} className="release-notes-item">
              <ReactMarkdown remarkPlugins={[remarkGfmPlugin]} components={markdownComponents}>{note.body}</ReactMarkdown>
            </li>
          ))}
        </ul>
        <div className="modal-actions padded">
          <button ref={confirmRef} type="button" className="new-pill" onClick={close}>知道了</button>
        </div>
        </>)}
      </FloatingModalCard>
    </section>
  );
}
