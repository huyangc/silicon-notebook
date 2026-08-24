/**
 * answer-gap-suggestions.tsx
 *
 * Renders the ``ask.gap_consult`` disclosure block: pointers to material
 * OUTSIDE this notebook, offered when a reasoning run came up thin or left a
 * confirmed direction uncovered. Real source of the wire shape:
 * `backend/app/models/ask.py AskGapSuggestion` / `AskResponse.gap_suggestions`.
 *
 * Three things this block deliberately is NOT, and must keep reading as:
 *   - not evidence — it never touches `buildAnswerReferences`/
 *     `computeSourceTierCounts`, takes no `[k]` key, and cannot move a word
 *     of the answer above it;
 *   - not silent about that — the disclaimer line says so before any link is
 *     shown, and the block is visually distinct from cited content (a plain
 *     list under a collapsed <details>, not a citation card);
 *   - not a shortcut into notebook content — importing one is an ordinary
 *     source add (the same `POST /notebooks/{id}/sources/url` used by the
 *     "add source" dialog's link box), with its own parsing and permissions.
 *     This component never calls a plugin-owned `/api/extensions/*` route.
 *
 * Default collapsed, visually mirroring `.answer-retrieval-scope`'s
 * <details>/<summary> shape (X9 PR-A decision U2) — but under its own CSS
 * class family (`.answer-gap-consult`), because the two blocks are unrelated
 * information categories and must be free to diverge in style later.
 *
 * Long-task button rule (CLAUDE.md "长任务按钮的忙碌态"): the import button
 * has no server-side single-flight guard behind it, so a click must disable
 * that one row immediately and swap in progress wording; success freezes it
 * into a terminal "已导入" state; failure surfaces the message persistently
 * under that row (never a toast) and leaves the button clickable again for a
 * retry. State is per-item, keyed by array **index**, not by `url`: url
 * de-duplication happens inside `GapConsultHost.consult()`'s `seen_urls` set
 * (backend/app/extensions/gap_consult.py) — an injected seat this component
 * cannot see the internals of — and the core code that hands the host's
 * tuple straight into `AskResponse.gap_suggestions`
 * (`ask_service.py::draft.response.gap_suggestions = list(gap_suggestions)`)
 * never re-verifies that invariant. Keying UI state on a value whose
 * uniqueness this component cannot itself prove is exactly the kind of
 * assumption that quietly breaks when the host changes; keying on index is
 * unconditionally safe regardless of what the host does or doesn't guarantee.
 */
import { useState } from "react";
import { ChevronRight, ExternalLink } from "lucide-react";

import type { GapSuggestion } from "./workspace-model";

type ImportOutcome = { ok: boolean; message?: string };

type ImportState =
  | { status: "idle" }
  | { status: "busy" }
  | { status: "done" }
  | { status: "failed"; message: string };

const IDLE: ImportState = { status: "idle" };

export function GapSuggestionsPanel({
  suggestions,
  onImport,
}: {
  suggestions: GapSuggestion[];
  /** 缺省即不渲染导入按钮（onSaveMemory 的既有惯例：写回服务端的动作没有回调
   *  就不出按钮）——只读排障视图传不了这个回调，也就没有导入入口。 */
  onImport?: (url: string) => Promise<ImportOutcome>;
}) {
  const [states, setStates] = useState<Record<number, ImportState>>({});

  if (suggestions.length === 0) return null;

  async function handleImport(index: number, url: string) {
    if (!onImport) return;
    setStates((previous) => ({ ...previous, [index]: { status: "busy" } }));
    let outcome: ImportOutcome;
    try {
      outcome = await onImport(url);
    } catch {
      outcome = { ok: false, message: "未能添加这个链接" };
    }
    setStates((previous) => ({
      ...previous,
      [index]: outcome.ok
        ? { status: "done" }
        : { status: "failed", message: outcome.message || "未能添加这个链接" },
    }));
  }

  return (
    <details className="answer-gap-consult">
      <summary title="这些结果来自笔记本之外，没有参与本次回答">
        <ChevronRight size={14} aria-hidden="true" />
        站外来源建议 · {suggestions.length} 条
      </summary>
      <p className="answer-gap-consult-disclaimer">
        以下结果来自笔记本之外，没有参与本次回答，也不会被引用。导入后才会进入这个笔记本。
      </p>
      <ul className="answer-gap-consult-list">
        {suggestions.map((suggestion, index) => {
          const state = states[index] ?? IDLE;
          return (
            <li className="answer-gap-consult-item" key={`${suggestion.url}#${index}`}>
              <div className="answer-gap-consult-item-head">
                <a href={suggestion.url} target="_blank" rel="noopener noreferrer">
                  {suggestion.title}
                  <ExternalLink size={12} aria-hidden="true" />
                </a>
                {suggestion.source_label && (
                  <span className="answer-gap-consult-source">{suggestion.source_label}</span>
                )}
              </div>
              {suggestion.summary && (
                <p className="answer-gap-consult-summary">{suggestion.summary}</p>
              )}
              {onImport && (
                <button
                  type="button"
                  className={`answer-gap-consult-import ${state.status === "done" ? "is-done" : ""}`}
                  disabled={state.status === "busy" || state.status === "done"}
                  onClick={() => handleImport(index, suggestion.url)}
                >
                  {state.status === "busy"
                    ? "导入中…"
                    : state.status === "done"
                      ? "已导入"
                      : "导入"}
                </button>
              )}
              {state.status === "failed" && (
                <p className="answer-gap-consult-error">{state.message}</p>
              )}
            </li>
          );
        })}
      </ul>
    </details>
  );
}
