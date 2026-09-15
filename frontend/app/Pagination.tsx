"use client";
import { useState } from "react";
import { pageMeta, clampPage } from "./pagination-logic.mjs";

// `label` 命名这组翻页控件(「成员分页」):一个页面上常有几份清单同时带分页,
// 读屏与测试都靠它区分按的是哪一份的「下一页」。
// `compact` 给窄侧栏(群组清单、图谱侧栏、活动左栏)用:翻页按钮只画箭头(可访问名称
// 不变),区间单独占一行,不然两百多像素宽里会折成参差的三行。
export function Pagination({ page, pageSize, total, onPage, busy, label, compact }: {
  page: number; pageSize: number; total: number; onPage: (p: number) => void; busy?: boolean;
  label: string; compact?: boolean;
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
  // 跳页只在回车时提交。失焦也提交的话,「在框里敲 3、再点下一页」会先因失焦跳到第 3 页、
  // 点击再翻一页,一次点击翻两次;翻页按钮顺带清掉框里没提交的页码。
  const step = (p: number) => { setJump(""); go(p); };
  return (
    <nav className={compact ? "pagination compact" : "pagination"} aria-label={label}>
      <span className="pagination-info">{from}–{to} / {total}</span>
      <button className="sort-button" aria-label={compact ? "上一页" : undefined} disabled={busy || !canPrev} onClick={() => step(page - 1)}>{compact ? "‹" : "上一页"}</button>
      <span className="pagination-page">{compact ? `${page + 1} / ${lastPage + 1}` : `第 ${page + 1} / ${lastPage + 1} 页`}</span>
      <button className="sort-button" aria-label={compact ? "下一页" : undefined} disabled={busy || !canNext} onClick={() => step(page + 1)}>{compact ? "›" : "下一页"}</button>
      <input
        className="pagination-jump" type="number" min={1} max={lastPage + 1}
        value={jump} placeholder="跳页" aria-label="跳到第几页" disabled={busy}
        onChange={(e) => setJump(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter") submitJump(); }}
      />
    </nav>
  );
}
