"use client";

import { useParams } from "next/navigation";
import { SourceReader } from "../../../../source-reader";

export default function SourceReaderPage() {
  const { notebookId, sourceId } = useParams<{ notebookId: string; sourceId: string }>();
  return <SourceReader key={`${notebookId}:${sourceId}`} notebookId={notebookId} sourceId={sourceId} />;
}
