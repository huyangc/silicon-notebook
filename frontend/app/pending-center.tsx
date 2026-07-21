// frontend/app/pending-center.tsx
"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { Bell } from "lucide-react";
import { performApiRequest } from "./api-client.ts";
import { getToken } from "./auth";
import { itemSig, currentSigs, doneMessage, pruneSigs, pendingView } from "./pending-actions";

// localStorage 读写(私密模式/SSR 容错):待办的「已读/关掉」状态按用户存本地。
const loadSigs = (key: string): string[] => {
  try { const v = window.localStorage.getItem(key); return v ? JSON.parse(v) : []; } catch { return []; }
};
const saveSigs = (key: string, v: string[]) => {
  try { window.localStorage.setItem(key, JSON.stringify(v)); } catch { /* ignore */ }
};

export type PendingItem = {
  type: "report_outline" | "governance" | "index" | "paper_meta";
  notebook_id: string;
  notebook_name: string;
  subtype?: "merge" | "edge" | "promotion";
  report_id?: string;
  title?: string;
  count?: number;
  state?: string;
  progress?: number | { done: number; total: number };  // index 是 %，paper_meta 是 {done,total}
  _key?: string;  // 客户端 done 项用
};
export type DoneToast = { notebook_id: string; notebook_name: string; ts: number; kind?: string; text?: string };

export type Snapshot = { count: number; items: PendingItem[] };

export function usePendingActions(enabled: boolean) {
  const [snapshot, setSnapshot] = useState<Snapshot>({ count: 0, items: [] });
  const [doneItems, setDoneItems] = useState<DoneToast[]>([]);
  const [toast, setToast] = useState<DoneToast | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const retryRef = useRef(0);
  const stoppedRef = useRef(false);

  // kind 可选:省略时保持「清掉该 notebook 全部 done」旧语义(dismissAll / 小节「×」用);
  // 传 kind 时只清该 notebook 的这一种完成事件——因为本 diff 允许同一 notebook 的
  // index_done 与 paper_meta_done 并存,单条「×」若按 notebook_id 一刀切会连带清掉另
  // 一条(doneItems 是纯内存 SSE 态、无持久化,未读的那条就永久丢了)。
  const dismissDone = useCallback((notebook_id: string, kind?: string) => {
    setDoneItems((xs) => xs.filter((d) =>
      d.notebook_id !== notebook_id || (kind !== undefined && d.kind !== kind)
    ));
  }, []);

  useEffect(() => {
    if (!enabled || !getToken()) {
      setSnapshot({ count: 0, items: [] });
      setDoneItems([]);
      setToast(null);
      return;
    }
    stoppedRef.current = false;

    // REST 兜底:先拉一次秒开
    (async () => {
      try {
        const r = await performApiRequest("/me/pending-actions", { tag: "pending-actions" });
        if (r.ok) setSnapshot(await r.json());
      } catch { /* 交给流 */ }
    })();

    const connect = async () => {
      if (stoppedRef.current) return;
      const ac = new AbortController();
      abortRef.current = ac;
      try {
        const resp = await performApiRequest("/me/pending-actions/stream", {
          signal: ac.signal,
          tag: "pending-actions",
        });
        if (!resp.ok || !resp.body) throw new Error(`stream ${resp.status}`);
        retryRef.current = 0;
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let nl: number;
          while ((nl = buffer.indexOf("\n")) >= 0) {
            const line = buffer.slice(0, nl).trim();
            buffer = buffer.slice(nl + 1);
            if (!line || line.startsWith(":")) continue;  // 跳过 keepalive
            let msg: any;
            try { msg = JSON.parse(line); } catch { continue; }
            if (msg.kind === "snapshot") {
              setSnapshot(msg.data as Snapshot);
            } else if (msg.kind === "event" && msg.event) {
              const text = doneMessage(msg.event, msg);
              if (!text) continue;
              const d: DoneToast = {
                notebook_id: msg.notebook_id,
                notebook_name: msg.notebook_name || "",
                ts: Date.now(),
                kind: msg.event,
                text,
              };
              // 去重键含 kind:同一 notebook 的 index_done 与 paper_meta_done 各自独立展示，
              // 互不覆盖(旧逻辑仅按 notebook_id 去重，会丢掉先到的另一种完成事件)。
              setDoneItems((xs) => [d, ...xs.filter((x) => x.notebook_id !== d.notebook_id || x.kind !== d.kind)]);
              setToast(d);
            }
          }
        }
      } catch { /* 断线 → 退避重连 */ }
      if (stoppedRef.current) return;
      const delay = Math.min(30000, 1000 * 2 ** retryRef.current++);
      setTimeout(connect, delay);
    };
    connect();

    return () => { stoppedRef.current = true; abortRef.current?.abort(); };
  }, [enabled]);

  return { snapshot, doneItems, toast, setToast, dismissDone };
}

export function PendingBell(props: {
  snapshot: { count: number; items: PendingItem[] };
  doneItems: DoneToast[];
  userId?: string;
  onOpenItem: (item: PendingItem) => void;
  onOpenDone: (d: DoneToast) => void;
  onDismissDone: (notebook_id: string, kind?: string) => void;
}) {
  const { snapshot, doneItems, userId, onOpenItem, onOpenDone, onDismissDone } = props;
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);

  // 已读(seen)/关掉(dismissed)按用户存 localStorage。
  const seenKey = `pending:seen:${userId || "anon"}`;
  const dismKey = `pending:dismissed:${userId || "anon"}`;
  const [seen, setSeen] = useState<string[]>([]);
  const [dismissed, setDismissed] = useState<string[]>([]);

  // 用户切换(登录/登出)→ 重载各自的集合。
  useEffect(() => { setSeen(loadSigs(seenKey)); setDismissed(loadSigs(dismKey)); }, [seenKey, dismKey]);

  // 每次快照变化:把关掉集合剪到「仍存在」的签名(限大小 + 计数变化的项复活)。
  useEffect(() => {
    const cur = currentSigs(snapshot.items, doneItems);
    setDismissed((prev) => {
      const pruned = pruneSigs(prev, cur);
      if (pruned.length === prev.length) return prev;
      saveSigs(dismKey, pruned);
      return pruned;
    });
  }, [snapshot.items, doneItems, dismKey]);

  const view = pendingView(snapshot.items, doneItems, seen, dismissed);
  const badge = view.unread;

  // 打开面板(或打开时来了新项)→ 把当前可见项全部标为已读 → 徽标归零。seen 不在依赖里,
  // 故 setSeen 不会自触发循环;dismissed/快照变化时若面板开着会重新标记。
  useEffect(() => {
    if (!open) return;
    const sigs = currentSigs(view.visibleItems, view.visibleDone);
    setSeen((prev) => {
      if (prev.length === sigs.length && sigs.every((s) => prev.includes(s))) return prev;
      saveSigs(seenKey, sigs);
      return sigs;
    });
  }, [open, snapshot.items, doneItems, dismissed, seenKey]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  const dismissItem = (sig: string) => {
    setDismissed((prev) => {
      if (prev.includes(sig)) return prev;
      const n = [...prev, sig];
      saveSigs(dismKey, n);
      return n;
    });
  };
  const dismissAll = () => {
    const sigs = view.visibleItems.map(itemSig);
    setDismissed((prev) => {
      const n = Array.from(new Set([...prev, ...sigs]));
      saveSigs(dismKey, n);
      return n;
    });
    view.visibleDone.forEach((d) => onDismissDone(d.notebook_id));
  };
  // 小节标题「×」:关闭该小节全部(治理/报告/索引 走签名 dismissed;已完成走 onDismissDone)。
  const dismissGroup = (sigs: string[]) => {
    if (sigs.length === 0) return;
    setDismissed((prev) => {
      const n = Array.from(new Set([...prev, ...sigs]));
      saveSigs(dismKey, n);
      return n;
    });
  };

  const groups: { key: string; label: string; items: PendingItem[] }[] = [
    { key: "report_outline", label: "深度报告待确认", items: view.visibleItems.filter((i) => i.type === "report_outline") },
    { key: "governance", label: "治理待办", items: view.visibleItems.filter((i) => i.type === "governance") },
    { key: "index", label: "索引状态", items: view.visibleItems.filter((i) => i.type === "index") },
    { key: "paper_meta", label: "论文元数据", items: view.visibleItems.filter((i) => i.type === "paper_meta") },
  ];

  const labelFor = (it: PendingItem): string => {
    if (it.type === "report_outline") return `深度报告《${it.title}》`;
    if (it.type === "governance") {
      const n = it.subtype === "merge" ? "待合并" : it.subtype === "edge" ? "关系审核" : "内容审核";
      return `${it.notebook_name} · ${n} ${it.count}`;
    }
    if (it.type === "paper_meta") {
      const p = it.progress as { done: number; total: number } | undefined;
      return `${it.notebook_name} · 论文信息补全中 · ${p?.done ?? 0}/${p?.total ?? 0}`;
    }
    const s = it.state === "building" ? (it.progress != null ? `索引构建中(${it.progress}%)` : "索引构建中")
      : it.state === "suggested" ? "建议建立索引" : "建议重建索引";
    return `${it.notebook_name} · ${s}`;
  };

  const hasAny = view.visibleItems.length + view.visibleDone.length > 0;

  return (
    <div className="pending-center" ref={ref}>
      <button className="pending-bell" onClick={() => setOpen((o) => !o)} aria-label="待确认中心">
        <Bell size={18} />
        {badge > 0 && <span className="pending-badge">{badge > 99 ? "99+" : badge}</span>}
      </button>
      {open && (
        <div className="pending-popover">
          {hasAny && (
            <div className="pending-actions-bar">
              <button className="pending-clear" type="button" onClick={dismissAll}>全部清空</button>
            </div>
          )}
          {!hasAny && <p className="pending-empty">暂无待确认</p>}
          {groups.map((g) => g.items.length > 0 && (
            <div className="pending-group" key={g.key}>
              <div className="pending-group-title">
                <span>{g.label}</span>
                <span className="pending-group-x" title={`关闭全部「${g.label}」`}
                      onClick={() => dismissGroup(g.items.map(itemSig))}>×</span>
              </div>
              {g.items.map((it, idx) => (
                <button className="pending-row" key={`${g.key}-${idx}`}
                        onClick={() => { setOpen(false); onOpenItem(it); }}>
                  <span className="pending-row-main">
                    {it.type !== "report_outline" && <span className="pending-row-nb">{it.notebook_name}</span>}
                    <span className="pending-row-label">{labelFor(it)}</span>
                  </span>
                  <span className="pending-row-x" title="关掉"
                        onClick={(e) => { e.stopPropagation(); dismissItem(itemSig(it)); }}>×</span>
                </button>
              ))}
            </div>
          ))}
          {view.visibleDone.length > 0 && (
            <div className="pending-group pending-group-done">
              <div className="pending-group-title">
                <span>已完成</span>
                <span className="pending-group-x" title="关闭全部「已完成」"
                      onClick={() => view.visibleDone.forEach((d) => onDismissDone(d.notebook_id))}>×</span>
              </div>
              {view.visibleDone.map((d) => (
                <button className="pending-row pending-row-done" key={`${d.notebook_id}:${d.kind ?? ""}`}
                        onClick={() => { setOpen(false); onOpenDone(d); onDismissDone(d.notebook_id, d.kind); }}>
                  <span className="pending-row-main"><span className="pending-row-label">{d.notebook_name} · {d.kind === "paper_meta_done" ? "论文信息补全完成" : "索引构建完成"}</span></span>
                  <span className="pending-row-x" title="关掉" onClick={(e) => { e.stopPropagation(); onDismissDone(d.notebook_id, d.kind); }}>×</span>
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function PendingToast(props: { toast: DoneToast | null; onClose: () => void; onClick: () => void }) {
  const { toast, onClose, onClick } = props;
  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(onClose, 6000);
    return () => clearTimeout(t);
  }, [toast, onClose]);
  if (!toast) return null;
  return (
    <div className="pending-toast" onClick={() => { onClick(); onClose(); }}>
      {toast.text ?? `「${toast.notebook_name}」索引构建完成,点击查看`}
    </div>
  );
}
