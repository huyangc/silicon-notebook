import { Maximize2 } from "lucide-react";

export function SourcePreviewLink({ notebookId, sourceId }: { notebookId: string; sourceId: string }) {
  if (!notebookId) return null;
  return (
    <a className="button secondary source-preview-link"
      href={`/notebooks/${encodeURIComponent(notebookId)}/sources/${encodeURIComponent(sourceId)}`}
      target="_blank" rel="noopener noreferrer" title="在新标签页全屏阅读">
      <Maximize2 size={15} aria-hidden="true" />全屏阅读
    </a>
  );
}
