import { Maximize2 } from "lucide-react";

export function SourcePreviewLink({ notebookId, sourceId }: { notebookId: string; sourceId: string }) {
  if (!notebookId) return null;
  return (
    <a className="icon-button subtle-icon source-preview-link"
      href={`/notebooks/${encodeURIComponent(notebookId)}/sources/${encodeURIComponent(sourceId)}`}
      target="_blank" rel="noopener noreferrer" title="全屏阅读（在新标签页打开）" aria-label="全屏阅读">
      <Maximize2 size={20} aria-hidden="true" />
    </a>
  );
}
