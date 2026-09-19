import { BookOpen, Check, ChevronDown, Search, X } from "lucide-react";
import { useMemo, useState } from "react";
import { GLOBAL_ASK_MAX_NOTEBOOKS, type GlobalScope } from "../global-ask-api.ts";
import type { NotebookSummary } from "../workspace-model.ts";

export function scopeLabel(scope: GlobalScope, count: number): string {
  if (scope.mode === "all") return `全部笔记本 · ${count} 个`;
  return scope.notebook_ids.length ? `已选择 ${scope.notebook_ids.length} 个笔记本` : "未选择笔记本";
}

export function NotebookScopePicker({ notebooks, scope, onChange, disabled }: {
  notebooks: NotebookSummary[];
  scope: GlobalScope;
  onChange: (scope: GlobalScope) => void;
  disabled: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const matching = useMemo(() => notebooks.filter((item) =>
    `${item.name} ${item.purpose}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()),
  ), [notebooks, query]);
  const selected = scope.mode === "include" ? scope.notebook_ids : [];
  const missing = selected.filter((id) => !notebooks.some((item) => item.id === id));
  // 可读笔记本超过上限时「全部」不是一个可提交的范围：那颗按钮整个不出现，清空选择
  // 也不回落到「全部」（否则会被立刻重新预选，用户刚取消的勾又回来）。
  const overCap = notebooks.length > GLOBAL_ASK_MAX_NOTEBOOKS;
  const full = selected.length >= GLOBAL_ASK_MAX_NOTEBOOKS;

  function toggle(id: string) {
    if (!selected.includes(id) && full) return;
    const ids = selected.includes(id) ? selected.filter((value) => value !== id) : [...selected, id];
    onChange(ids.length || overCap ? { mode: "include", notebook_ids: ids } : { mode: "all" });
  }

  return <div className="global-scope">
    <button className="global-scope-trigger" type="button" aria-expanded={open} aria-controls="global-scope-options" disabled={disabled} onClick={() => setOpen(!open)}>
      <BookOpen size={15} />{scopeLabel(scope, notebooks.length)}<ChevronDown size={14} />
    </button>
    {open && <section id="global-scope-options" className="global-scope-panel" aria-label="选择检索范围" onKeyDown={(event) => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); setOpen(false); } }}>
      <div className="global-scope-heading"><strong>选择检索范围</strong><button type="button" className="icon-button" aria-label="收起检索范围" onClick={() => setOpen(false)}><X size={16} /></button></div>
      <label className="global-scope-search"><Search size={16} /><input autoFocus type="search" aria-label="搜索笔记本" placeholder="搜索名称或用途" value={query} onChange={(event) => setQuery(event.target.value)} /></label>
      {overCap ? <p className="global-scope-cap" role="note">一次最多检索 {GLOBAL_ASK_MAX_NOTEBOOKS} 个笔记本，已预选来源最多的 {GLOBAL_ASK_MAX_NOTEBOOKS} 个，可以改选。{full && " 已选满，取消一个后才能再选。"}</p> : <button type="button" className="global-scope-all" disabled={disabled} onClick={() => onChange({ mode: "all" })}>
        <span><strong>全部可访问的笔记本</strong><small>不指定笔记本时，默认检索全部</small></span>{scope.mode === "all" ? <Check size={17} /> : <span>恢复全部</span>}
      </button>}
      <div className="global-scope-list">
        {matching.map((item) => <label key={item.id} className={`global-scope-option${selected.includes(item.id) ? " selected" : ""}`}>
          <input type="checkbox" checked={selected.includes(item.id)} disabled={disabled || (full && !selected.includes(item.id))} onChange={() => toggle(item.id)} />
          <BookOpen size={17} /><span><strong>{item.name}</strong><small>{item.purpose || "暂未填写用途"}</small></span><small>{item.counts.sources ?? 0} 个来源</small>
        </label>)}
        {!matching.length && <p className="global-muted">{notebooks.length ? "没有匹配的笔记本，试试其他关键词" : "还没有可访问的笔记本"}</p>}
      </div>
      {missing.length > 0 && <div className="global-inline-error">有 {missing.length} 个已选笔记本不可访问，请重新选择范围。
        <button type="button" className="global-text-button" disabled={disabled} onClick={() => {
          const ids = selected.filter((id) => !missing.includes(id));
          onChange(ids.length || overCap ? { mode: "include", notebook_ids: ids } : { mode: "all" });
        }}>移除不可访问的选择</button>
      </div>}
      <div className="global-scope-footer"><span>{scopeLabel(scope, notebooks.length)}</span><button type="button" className="sort-button" onClick={() => setOpen(false)}>完成</button></div>
    </section>}
  </div>;
}
