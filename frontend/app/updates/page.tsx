"use client";

import { useEffect, useState } from "react";

import { fetchMe } from "../auth.ts";
import { PageHeader } from "../components/PageHeader.tsx";
import { toUserMessage } from "../errors.ts";
import { fetchReleaseNotesHistory, type ReleaseNotesHistoryResponse } from "../release-notes-api.ts";
import { UpdatesList } from "./updates-list.tsx";
import "./updates.css";

const SHOW_INTERNAL_KEY = "updates.showInternal";

type LoadState =
  | { kind: "loading" }
  | { kind: "error"; notice: string }
  | { kind: "ready"; history: ReleaseNotesHistoryResponse };

function readShowInternal(): boolean {
  try {
    return window.localStorage.getItem(SHOW_INTERNAL_KEY) === "1";
  } catch {
    return false;
  }
}

export default function UpdatesPage() {
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [showInternal, setShowInternal] = useState(false);

  useEffect(() => { setShowInternal(readShowInternal()); }, []);

  useEffect(() => {
    let cancelled = false;
    Promise.all([fetchMe(), fetchReleaseNotesHistory()])
      .then(([, history]) => { if (!cancelled) setState({ kind: "ready", history }); })
      .catch((error) => {
        if (!cancelled) setState({ kind: "error", notice: toUserMessage(error, "更新记录加载失败，请重试") });
      });
    return () => { cancelled = true; };
  }, []);

  function changeShowInternal(value: boolean) {
    setShowInternal(value);
    try {
      window.localStorage.setItem(SHOW_INTERNAL_KEY, value ? "1" : "0");
    } catch {
      // 记不住只是下次重新勾选,不影响本次筛选。
    }
  }

  return (
    <>
      <PageHeader title="更新记录" />
      <main className="updates-page">
        <h1>更新记录</h1>
        {state.kind === "loading" && <p className="updates-status" role="status">正在加载…</p>}
        {state.kind === "error" && <p className="updates-status error" role="alert">{state.notice}</p>}
        {state.kind === "ready" && (
          state.history.available && state.history.build ? (
            <>
              <p className="updates-build">当前版本 {state.history.build.version}</p>
              <label className="updates-filter">
                <input type="checkbox" checked={showInternal} onChange={(event) => changeShowInternal(event.target.checked)} />
                显示后台改进
              </label>
              <UpdatesList notes={state.history.notes} showInternal={showInternal} />
            </>
          ) : (
            <p className="updates-empty">暂无更新记录</p>
          )
        )}
      </main>
    </>
  );
}
