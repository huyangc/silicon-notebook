"use client";

import { useEffect, useState } from "react";
import { isAskBlocked } from "./ask-availability.ts";
import {
  fetchNotebookQuestionSuggestions,
  type NotebookQuestionSuggestions as SuggestionsResponse,
} from "./notebook-api.ts";
import type { NotebookSummary } from "./workspace-model.ts";

type Props = {
  actorId: string | null;
  workspaceEpoch: number;
  notebook: NotebookSummary | null;
  contentRevision: number;
  sourceTotal: number;
  fallbackPrompts: Array<[string, string]>;
  onSubmit(question: string): void;
};

/** A welcome-only read of a backend-cached projection, independent of Ask state. */
export function NotebookQuestionSuggestions({
  actorId, workspaceEpoch, notebook, contentRevision, sourceTotal, fallbackPrompts, onSubmit,
}: Props) {
  const notebookId = notebook?.id ?? null;
  const manual = Boolean(notebook?.expected_questions?.some((question) => question.trim()));
  const eligible = Boolean(actorId && notebookId && sourceTotal > 0 && !manual && !isAskBlocked(notebook));
  // Identity is checked in render as well as in the effect cleanup. Old results
  // must never flash while React is switching actors, notebooks or source data.
  // This is a revalidation trigger, not the authoritative backend content hash.
  const revision = JSON.stringify([
    actorId, workspaceEpoch, notebookId, eligible, sourceTotal,
    notebook?.name, notebook?.purpose, notebook?.primary_domain,
    contentRevision,
  ]);
  const [settled, setSettled] = useState<{
    revision: string;
    result: SuggestionsResponse | null;
  } | null>(null);

  useEffect(() => {
    if (!eligible || !notebookId) return;
    const controller = new AbortController();
    let current = true;
    fetchNotebookQuestionSuggestions(notebookId, controller.signal).then(
      (result) => {
        if (current) setSettled({ revision, result });
      },
      () => {
        // Suggestions are optional. A transport/model failure leaves useful
        // template questions in place and never blocks the real Ask action.
        if (current) setSettled({ revision, result: null });
      },
    );
    return () => {
      current = false;
      controller.abort();
    };
  }, [eligible, notebookId, revision]);

  if (isAskBlocked(notebook)) return null;
  const result = eligible && settled?.revision === revision ? settled.result : null;
  const generated = result?.status === "ready" && result.questions.length > 0;
  const prompts: Array<[string, string]> = generated
    ? result.questions.map(({ label, question }) => [label, question])
    : fallbackPrompts;
  const loading = eligible && settled?.revision !== revision;

  return <>
    <div className="prompt-chips">
      {prompts.map(([label, question]) => (
        <button key={question} title={question} onClick={() => onSubmit(question)}>{label}</button>
      ))}
    </div>
    {loading && <small role="status">正在根据来源整理问题…</small>}
    {generated && <small>{result.sampled ? "根据部分来源内容推荐" : "根据来源内容推荐"}</small>}
  </>;
}
