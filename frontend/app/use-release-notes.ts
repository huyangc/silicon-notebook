"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  fetchReleaseNotes,
  type ReleaseNote,
  type ReleaseNotesBuild,
} from "./release-notes-api.ts";

export type ReleaseNotesNotice = { build: ReleaseNotesBuild; notes: ReleaseNote[] };

/**
 * 登录后每个用户 id、每次页面加载只向服务端问一次「有没有该看的更新说明」。
 * 只有清单可用且待看说明非空才给出 notice；取失败一律静默（不上横幅——下次加载
 * 会再问，这是可接受的退路）。本 hook 只负责取，不负责展示：弹窗槽位由 page.tsx
 * 依 notice 打开，关闭后调 `clear()`。
 */
export function useReleaseNotes(userId: string | null, authChecked: boolean) {
  const [notice, setNotice] = useState<ReleaseNotesNotice | null>(null);
  const askedRef = useRef(new Set<string>());

  useEffect(() => {
    setNotice(null);
    if (!authChecked || !userId || askedRef.current.has(userId)) return;
    let cancelled = false;
    // 只有「未被取消地落定」才记为已问:被取消的在途请求(StrictMode 重挂、登出再登入)
    // 没有把结果交给任何人,本次页面加载里必须允许重取。
    fetchReleaseNotes()
      .then((response) => {
        if (cancelled) return;
        askedRef.current.add(userId);
        if (response.available && response.build && response.notes.length > 0) {
          setNotice({ build: response.build, notes: response.notes });
        }
      })
      .catch(() => {
        if (!cancelled) askedRef.current.add(userId);
      });
    return () => { cancelled = true; };
  }, [authChecked, userId]);

  const clear = useCallback(() => setNotice(null), []);
  return { notice, clear };
}
