"use client";

import { useMemo } from "react";

import { Pagination } from "../Pagination.tsx";
import { ReleaseNoteBody } from "../release-note-body.tsx";
import type { ReleaseNote } from "../release-notes-api.ts";
import { RELEASE_NOTE_LEVEL_LABELS } from "../release-notes-model.ts";
import { useClientPagination } from "../use-client-pagination.ts";

export const UPDATES_PAGE_SIZE = 20;

/** 更新记录列表:按传入顺序(服务端已是新到旧)分页渲染,正文直接展开。 */
export function UpdatesList({ notes, showInternal }: { notes: ReleaseNote[]; showInternal: boolean }) {
  const visible = useMemo(
    () => (showInternal ? notes : notes.filter((note) => note.level !== "internal")),
    [notes, showInternal],
  );
  // 切换「显示后台改进」换了一份清单,回到第一页。
  const paging = useClientPagination(visible, UPDATES_PAGE_SIZE, showInternal);
  if (visible.length === 0) {
    return (
      <p className="updates-empty">
        {notes.length > 0 ? "没有可显示的更新，勾选「显示后台改进」可以查看后台改进。" : "暂无更新记录"}
      </p>
    );
  }
  return (
    <>
      <ul className="updates-list">
        {paging.pageItems.map((note) => (
          <li key={note.id} className="updates-item">
            <div className="updates-item-head">
              <span className={`release-notes-level ${note.level}`}>{RELEASE_NOTE_LEVEL_LABELS[note.level]}</span>
              <strong>{note.title}</strong>
            </div>
            {note.body && <div className="updates-item-body"><ReleaseNoteBody body={note.body} /></div>}
          </li>
        ))}
      </ul>
      <Pagination page={paging.page} pageSize={paging.pageSize} total={paging.total} onPage={paging.setPage} label="更新记录分页" />
    </>
  );
}
