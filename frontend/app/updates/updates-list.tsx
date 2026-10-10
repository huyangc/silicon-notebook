"use client";

import { ReleaseNoteBody } from "../release-note-body.tsx";
import type { ReleaseNote } from "../release-notes-api.ts";
import { RELEASE_NOTE_LEVEL_LABELS } from "../release-notes-model.ts";

/** 更新记录列表:按传入顺序(服务端已是新到旧)渲染,正文直接展开。 */
export function UpdatesList({ notes, showInternal }: { notes: ReleaseNote[]; showInternal: boolean }) {
  const visible = showInternal ? notes : notes.filter((note) => note.level !== "internal");
  if (visible.length === 0) return <p className="updates-empty">暂无更新记录</p>;
  return (
    <ul className="updates-list">
      {visible.map((note) => (
        <li key={note.id} className="updates-item">
          <div className="updates-item-head">
            <span className={`release-notes-level ${note.level}`}>{RELEASE_NOTE_LEVEL_LABELS[note.level]}</span>
            <strong>{note.title}</strong>
          </div>
          {note.body && <div className="updates-item-body"><ReleaseNoteBody body={note.body} /></div>}
        </li>
      ))}
    </ul>
  );
}
