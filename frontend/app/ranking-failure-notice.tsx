"use client";

import { useLayoutEffect, useRef, useState } from "react";
import { rankingNoticeLines } from "./answer-completeness";
import type { RankingFailure } from "./workspace-model";

export type RankingRebuildPhase = "idle" | "confirming" | "running" | "produced" | "nothing";

export type RankingNoticeActions = {
  selectAllAndReask?: () => void;
  startRebuild?: () => Promise<void>;
  rebuildNotebookId?: string;
  rebuildBusyReason?: string;
  rebuildError?: string;
  rebuildPhase?: RankingRebuildPhase;
  reask?: () => void;
  reaskDisabledReason?: string;
};

function RankingNoticeLine({ line, failure, actions }: {
  line: string;
  failure?: RankingFailure;
  actions?: RankingNoticeActions;
}) {
  const paragraph = useRef<HTMLParagraphElement>(null);
  const heldFocus = useRef(false);
  const requestedRebuild = useRef(false);
  const restoreAfterAction = useRef(false);
  const [starting, setStarting] = useState(false);
  const phase = actions?.rebuildPhase ?? "idle";
  const canRebuild = Boolean(actions?.startRebuild && actions.rebuildNotebookId
    && failure?.notebook_ids.includes(actions.rebuildNotebookId));

  // Consume focus requests from this user submission only. Polling, task completion
  // and expiring notices must never move focus (or scroll) back into an old answer.
  useLayoutEffect(() => {
    const shouldRestore = restoreAfterAction.current
      || (phase === "running" && requestedRebuild.current && heldFocus.current);
    restoreAfterAction.current = false;
    if (phase === "running") requestedRebuild.current = false;
    if (shouldRestore
      && (document.activeElement === document.body || document.activeElement === paragraph.current)) {
      const next = paragraph.current?.querySelector<HTMLButtonElement>("button:not(:disabled)");
      (next ?? paragraph.current)?.focus({ preventScroll: true });
    }
  }, [phase, starting]);

  let text = line;
  let action: React.ReactNode = null;
  const reask = actions?.reask && (
    <button
      type="button"
      className="link-button inline-action"
      disabled={Boolean(actions.reaskDisabledReason)}
      title={actions.reaskDisabledReason}
      onClick={actions.reask}
    >重新提问</button>
  );

  if (actions && failure) {
    switch (failure.reason) {
      case "scoped_large_notebook":
        action = actions.selectAllAndReask && (
          <button
            type="button"
            className="link-button inline-action"
            disabled={Boolean(actions.reaskDisabledReason)}
            title={actions.reaskDisabledReason}
            onClick={actions.selectAllAndReask}
          >全选本库来源并重新提问</button>
        );
        break;
      case "not_computed":
        if (!canRebuild) break;
        if (phase === "nothing") {
          text = failure.fallback_line ?? line;
        } else if (phase === "produced") {
          action = reask;
        } else if (phase === "running") {
          action = <span role="status">正在更新分析…</span>;
        } else {
          action = <>
            <button
              type="button"
              className="link-button inline-action"
              disabled={starting || phase === "confirming" || Boolean(actions.rebuildBusyReason)}
              title={actions.rebuildBusyReason}
              onClick={async () => {
                requestedRebuild.current = true;
                setStarting(true);
                try {
                  await actions.startRebuild?.();
                } finally {
                  // A cancelled/rejected start may replace its disabled trigger.
                  // Once running was observed there is no further focus request.
                  restoreAfterAction.current = requestedRebuild.current;
                  requestedRebuild.current = false;
                  setStarting(false);
                }
              }}
            >更新分析</button>
            <span>（后台进行，大库可能较久）</span>
            {actions.rebuildBusyReason && <span>{actions.rebuildBusyReason}</span>}
            {actions.rebuildError && <span role="status">{actions.rebuildError}</span>}
          </>;
        }
        break;
      case "recount_budget_exhausted":
      case "rank_failed":
      case "library_skipped":
        action = reask;
        break;
      case "analysis_unavailable":
      case "refresh_produced_nothing":
        break;
    }
  }

  return (
    <p
      ref={paragraph}
      tabIndex={-1}
      onFocusCapture={() => { heldFocus.current = true; }}
      onBlurCapture={(event) => {
        if (!event.relatedTarget || !event.currentTarget.contains(event.relatedTarget as Node)) {
          heldFocus.current = false;
        }
      }}
    >{text}{action && <> {action}</>}</p>
  );
}

/** Failure and disclosure text is owned by the backend; only action labels live here. */
export function RankingFailureNotice({ notice, failures, actions }: {
  notice: string;
  failures: RankingFailure[];
  actions?: RankingNoticeActions;
}) {
  if (!notice) return null;
  return (
    <div role="note" className="answer-collection-coverage-note">
      {rankingNoticeLines(notice).map((line, index) => (
        <RankingNoticeLine
          key={`${index}-${line}`}
          line={line}
          failure={failures.find((candidate) => candidate.line === line)}
          actions={actions}
        />
      ))}
    </div>
  );
}
