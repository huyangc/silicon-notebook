// frontend/app/pending-center.tsx
"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { Bell } from "lucide-react";
import { API_BASE, authHeaders, getToken } from "./auth";

export type PendingItem = {
  type: "report_outline" | "governance" | "index";
  notebook_id: string;
  notebook_name: string;
  subtype?: "merge" | "edge" | "promotion";
  report_id?: string;
  title?: string;
  count?: number;
  state?: string;
  progress?: number;
  _key?: string;  // 客户端 done 项用
};
export type DoneToast = { notebook_id: string; notebook_name: string; ts: number };

export type Snapshot = { count: number; items: PendingItem[] };

export function usePendingActions(enabled: boolean) {
  const [snapshot, setSnapshot] = useState<Snapshot>({ count: 0, items: [] });
  const [doneItems, setDoneItems] = useState<DoneToast[]>([]);
  const [toast, setToast] = useState<DoneToast | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const retryRef = useRef(0);
  const stoppedRef = useRef(false);

  const dismissDone = useCallback((notebook_id: string) => {
    setDoneItems((xs) => xs.filter((d) => d.notebook_id !== notebook_id));
  }, []);

  useEffect(() => {
    if (!enabled || !getToken()) return;
    stoppedRef.current = false;

    // REST 兜底:先拉一次秒开
    (async () => {
      try {
        const r = await fetch(`${API_BASE}/me/pending-actions`, { headers: authHeaders() });
        if (r.ok) setSnapshot(await r.json());
      } catch { /* 交给流 */ }
    })();

    const connect = async () => {
      if (stoppedRef.current) return;
      const ac = new AbortController();
      abortRef.current = ac;
      try {
        const resp = await fetch(`${API_BASE}/me/pending-actions/stream`, {
          headers: authHeaders(), signal: ac.signal,
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
            } else if (msg.kind === "event" && msg.event === "index_done") {
              const d: DoneToast = { notebook_id: msg.notebook_id, notebook_name: msg.notebook_name || "", ts: Date.now() };
              setDoneItems((xs) => [d, ...xs.filter((x) => x.notebook_id !== d.notebook_id)]);
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
  onOpenItem: (item: PendingItem) => void;
  onOpenDone: (d: DoneToast) => void;
  onDismissDone: (notebook_id: string) => void;
}) {
  const { snapshot, doneItems, onOpenItem, onOpenDone, onDismissDone } = props;
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);
  const badge = snapshot.count + doneItems.length;

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  const groups: { key: string; label: string; items: PendingItem[] }[] = [
    { key: "report_outline", label: "深度报告待确认", items: snapshot.items.filter((i) => i.type === "report_outline") },
    { key: "governance", label: "治理待办", items: snapshot.items.filter((i) => i.type === "governance") },
    { key: "index", label: "索引状态", items: snapshot.items.filter((i) => i.type === "index") },
  ];

  const labelFor = (it: PendingItem): string => {
    if (it.type === "report_outline") return `深度报告《${it.title}》`;
    if (it.type === "governance") {
      const n = it.subtype === "merge" ? "待合并" : it.subtype === "edge" ? "边审" : "晋升";
      return `${it.notebook_name} · ${n} ${it.count}`;
    }
    const s = it.state === "building" ? (it.progress != null ? `索引构建中(${it.progress}%)` : "索引构建中")
      : it.state === "suggested" ? "建议建立索引" : "建议重建索引";
    return `${it.notebook_name} · ${s}`;
  };

  return (
    <div className="pending-center" ref={ref}>
      <button className="pending-bell" onClick={() => setOpen((o) => !o)} aria-label="待确认中心">
        <Bell size={18} />
        {badge > 0 && <span className="pending-badge">{badge > 99 ? "99+" : badge}</span>}
      </button>
      {open && (
        <div className="pending-popover">
          {badge === 0 && <p className="pending-empty">暂无待确认</p>}
          {groups.map((g) => g.items.length > 0 && (
            <div className="pending-group" key={g.key}>
              <div className="pending-group-title">{g.label}</div>
              {g.items.map((it, idx) => (
                <button className="pending-row" key={`${g.key}-${idx}`}
                        onClick={() => { setOpen(false); onOpenItem(it); }}>
                  {it.type !== "report_outline" && <span className="pending-row-nb">{it.notebook_name}</span>}
                  <span className="pending-row-label">{labelFor(it)}</span>
                </button>
              ))}
            </div>
          ))}
          {doneItems.length > 0 && (
            <div className="pending-group pending-group-done">
              <div className="pending-group-title">已完成</div>
              {doneItems.map((d) => (
                <button className="pending-row pending-row-done" key={d.notebook_id}
                        onClick={() => { setOpen(false); onOpenDone(d); onDismissDone(d.notebook_id); }}>
                  <span className="pending-row-label">{d.notebook_name} · 索引构建完成</span>
                  <span className="pending-row-x" onClick={(e) => { e.stopPropagation(); onDismissDone(d.notebook_id); }}>×</span>
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
      「{toast.notebook_name}」索引构建完成,点击查看
    </div>
  );
}
