import { BookOpen } from "lucide-react";
import type { GlobalJob } from "../global-ask-api";

export function GlobalCoverageReceipt({ job, notebookNames }: { job: GlobalJob; notebookNames: Map<string, string> }) {
  const receipt = job.response ?? job;
  const skipped = receipt.skipped_notebooks ?? [];
  const degraded = receipt.degraded_notebook_ids ?? [];
  if (job.status === "running" && !skipped.length && !degraded.length) return null;
  return <div className={`global-coverage-receipt${skipped.length ? " partial" : ""}`} aria-label="本轮检索回执">
    <div className="global-coverage-counts"><BookOpen size={13} /><span>范围 {receipt.resolved_notebook_ids.length} 个 · 已检索 {receipt.searched_notebook_ids.length} 个 · 引用来自 {receipt.cited_notebook_ids.length} 个笔记本</span></div>
    {degraded.length > 0 && <p>{degraded.length} 个笔记本使用词法降级检索，跨语言召回可能不完整。</p>}
    {skipped.length > 0 && <>
      <p>部分笔记本未完成检索，{job.status === "running" ? "本轮将仅使用成功检索到的资料。" : "回答仅覆盖成功检索到的资料。"}</p>
      <ul>{skipped.map((item) => <li key={item.notebook_id}><strong>{notebookNames.get(item.notebook_id) ?? "不可用笔记本"}</strong><span>{item.reason}</span></li>)}</ul>
    </>}
  </div>;
}
