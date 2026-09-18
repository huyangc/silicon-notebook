"use client";

import { useEffect, useRef, useState } from "react";

type NotebookTitle = { id: string; name: string } | null;

export function useNotebookTitleDraft(notebook: NotebookTitle) {
  const [titleDraft, setTitleDraft] = useState(notebook?.name ?? "");
  const previousTitle = useRef(notebook);
  const notebookId = notebook?.id ?? null;
  const notebookName = notebook?.name ?? "";

  useEffect(() => {
    const previous = previousTitle.current;
    previousTitle.current = notebookId ? { id: notebookId, name: notebookName } : null;
    setTitleDraft((draft) => (
      previous?.id === notebookId && draft !== previous?.name
        ? draft
        : notebookName
    ));
  }, [notebookId, notebookName]);

  return [titleDraft, setTitleDraft] as const;
}
