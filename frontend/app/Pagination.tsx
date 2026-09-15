"use client";
import { useState } from "react";
import { pageMeta, clampPage } from "./pagination-logic.mjs";

// `label` 命名这组翻页控件(「成员分页」):一个页面上常有几份清单同时带分页,
// 读屏与测试都靠它区分按的是哪一份的「下一页」。
export function Pagination({ page, pageSize, total, onPage, busy, label }: {
  page: number; pageSize: number; total: number; onPage: (p: number) => void; busy?: boolean;
  label?: string;
}) {
  const { lastPage, canPrev, canNext, from, to } = pageMeta({ page, pageSize, total });
  const [jump, setJump] = useState("");
  if (lastPage === 0) return null;                 // single page → no control
  const go = (p: number) => onPage(clampPage(p, lastPage));
  const submitJump = () => {
    const n = parseInt(jump, 10);
    if (!Number.isNaN(n)) go(n - 1);               // user types 1-indexed
    setJump("");
  };
  return (
    <nav className="pagination" aria-label={label}>
      <span className="pagination-info">{from}–{to} / {total}</span>
      <button className="sort-button" disabled={busy || !canPrev} onClick={() => go(page - 1)}>上一页</button>
      <span className="pagination-page">第 {page + 1} / {lastPage + 1} 页</span>
      <button className="sort-button" disabled={busy || !canNext} onClick={() => go(page + 1)}>下一页</button>
      <input
        className="pagination-jump" type="number" min={1} max={lastPage + 1}
        value={jump} placeholder="跳页" aria-label="跳到第几页" disabled={busy}
        onChange={(e) => setJump(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter") submitJump(); }}
        onBlur={submitJump}
      />
    </nav>
  );
}
