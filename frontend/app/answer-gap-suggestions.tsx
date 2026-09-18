/**
 * answer-gap-suggestions.tsx
 *
 * Renders the ``ask.gap_consult`` disclosure block: pointers to material
 * OUTSIDE this notebook, offered when a reasoning run came up thin or left a
 * confirmed direction uncovered. Real source of the wire shape:
 * `backend/app/models/ask.py AskGapSuggestion` / `AskResponse.gap_suggestions`.
 *
 * Three things this block deliberately is NOT, and must keep reading as:
 *   - not notebook evidence — it never touches `buildAnswerReferences`/
 *     `computeSourceTierCounts`, takes no `[k]` key, and cannot move a word
 *     of the answer above it. A separate supplement can refer to it as [Xn];
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
 * Long-task button rule (`docs/development.md`): the import button
 * has no server-side single-flight guard behind it, so a click must disable
 * that one row immediately and swap in progress wording; success freezes it
 * into a terminal "已导入" state; failure surfaces the message persistently
 * under that row (never a toast) and leaves the button clickable again for a
 * retry. That state machine itself now lives in `./import-row-state`, shared
 * verbatim with the external-evidence citation card
 * (`answer-panel.tsx`, `ask.reflect_action` 的 `object_type === "external"`)
 * — same red line, one implementation.
 *
 * 逐行状态**按 URL 分格，且与引用卡共用同一个 controller**（生产调用点 AnswerView
 * 建一份传进来）。这是对早先「按数组下标分格」的一次有意反转，理由变了：那时下标
 * 是更安全的键——url 的唯一性由 `GapConsultHost.consult()` 的 `seen_urls`
 * (backend/app/extensions/gap_consult.py) 保证，是这个组件证明不了的假设，而把两条
 * 同 url 的建议算成一行会是个 bug。现在恰恰相反：同一个库外链接可能同时出现在这份
 * 建议清单和引用卡里，导入的又是**同一件东西**，所以「同 url 即同一格」正是要的语义
 * ——一处导入完，另一处立刻显示「已导入」，不会重复排入同一个链接来源。真出现两条
 * 同 url 的建议时它们一起冻结，也是对的：第二次导入本来就是一次重复。
 */
import { ChevronRight, ExternalLink } from "lucide-react";

import {
  ImportRowButton,
  useImportRowController,
  type ImportOutcome,
  type ImportRowController,
} from "./import-row-state";
import type { ExternalEvidenceSection as ExternalEvidenceSectionData, GapEgress, GapSuggestion } from "./workspace-model";

function ExternalReferenceText({ text, suggestions }: { text: string; suggestions: GapSuggestion[] }) {
  return <>{text.split(/(\[X[1-9]\d*\])/g).map((part, index) => {
    const match = /^\[X([1-9]\d*)\]$/.exec(part);
    const suggestion = match ? suggestions[Number(match[1]) - 1] : undefined;
    if (!suggestion || !/^https?:\/\//i.test(suggestion.url)) return part;
    return <a key={index} href={suggestion.url} target="_blank" rel="noopener noreferrer" title={suggestion.title}>{part}</a>;
  })}</>;
}

export function ExternalEvidenceSection({ section, suggestions }: {
  section: ExternalEvidenceSectionData | null | undefined;
  suggestions: GapSuggestion[];
}) {
  if (!section?.text?.trim()) return null;
  return (
    <section className="answer-external-evidence" aria-label="根据外部来源补充">
      <h3>根据外部来源补充</h3>
      <p className="answer-external-evidence-warning">以下内容基于站外来源的摘要生成，尚未经过笔记本核验，不计入本次回答的引用与覆盖率。</p>
      <div className="answer-external-evidence-text">
        <ExternalReferenceText text={section.text} suggestions={suggestions} />
      </div>
      {section.conflicts?.length > 0 && (
        <div className="answer-external-evidence-conflicts">
          <h4>与笔记本内容不一致</h4>
          <ul>{section.conflicts.map((conflict, index) => (
            <li key={index}>
              <p><strong>笔记本：</strong><ExternalReferenceText text={conflict.kb_says} suggestions={suggestions} /></p>
              <p><strong>站外来源：</strong><ExternalReferenceText text={conflict.external_says} suggestions={suggestions} /></p>
              {conflict.note && <p><strong>说明：</strong><ExternalReferenceText text={conflict.note} suggestions={suggestions} /></p>}
            </li>
          ))}</ul>
        </div>
      )}
    </section>
  );
}

function GapEgressReceipt({ egress }: { egress: GapEgress }) {
  const sourceQueries = egress.source_queries ?? {};
  return (
    <div className="answer-gap-consult-egress" aria-label="本轮站外检索记录">
      <strong>本轮实际尝试：</strong>
      {egress.sources.length > 0 ? (
        <ul>{egress.sources.map((source, index) => {
          const sourceValue = Object.hasOwn(sourceQueries, source) ? sourceQueries[source] : undefined;
          const queries = Array.isArray(sourceValue)
            ? sourceValue.filter((query) => typeof query === "string" && query.trim())
            : [];
          return <li key={`${source}#${index}`}>
            <span>{source}</span>
            <span> · 检索词：{queries.length > 0 ? queries.join("、") : egress.query}</span>
          </li>;
        })}</ul>
      ) : <span>没有调用站外来源。</span>}
    </div>
  );
}

export function GapSuggestionsPanel({
  suggestions,
  egress,
  controller,
  onImport,
  importDisabledReason,
}: {
  suggestions: GapSuggestion[];
  egress?: GapEgress | null;
  /** 生产调用点（AnswerView）传的**共享** controller：同一个 URL 在这份清单与外部
   *  证据引用卡之间共用一格「已导入」终态（见 import-row-state.tsx 顶部）。传了它
   *  就以它为准，`onImport`/`importDisabledReason` 不再参与——那两个是给独立渲染
   *  这个组件的调用方（组件测试、未来别的入口）用的自建路径。 */
  controller?: ImportRowController;
  /** 缺省即不渲染导入按钮（onSaveMemory 的既有惯例：写回服务端的动作没有回调
   *  就不出按钮）——只读排障视图传不了这个回调，也就没有导入入口。 */
  onImport?: (url: string) => Promise<ImportOutcome>;
  /** 非空时每一条建议的导入按钮都渲染为禁用态，并把这句话作为 `title` 提示
   *  ——用于「可写但已达笔记本文档数量上限」：这种情形不必先发一次远端 PDF
   *  探测再撞后端必然的容量拒绝，与「添加来源」弹窗满额时置灰同一形态
   *  （复用同一份 `resolveDocumentCapacity` 判据，见 page.tsx）。已导入/导入中
   *  两个终态/进行态的展示优先级高于它——那两态本身已经解释了按钮为什么
   *  点不动，不需要再叠加这句话。区块与免责句照常渲染，用户仍应看见建议
   *  本身。 */
  importDisabledReason?: string;
}) {
  // hook 无条件调用（`controller` 传了它也照跑，只是结果不被采用）——条件调用 hook
  // 会在 prop 出现/消失的那一次渲染上炸掉 hook 顺序。
  const ownController = useImportRowController(onImport, importDisabledReason);
  const importController = controller ?? ownController;

  if (suggestions.length === 0 && !egress) return null;

  return (
    <details className="answer-gap-consult">
      <summary title="这些结果来自笔记本之外，没有参与本次回答">
        <ChevronRight size={14} aria-hidden="true" />
        站外来源建议 · {suggestions.length} 条
      </summary>
      <p className="answer-gap-consult-disclaimer">
        {suggestions.length > 0
          ? "以下结果来自笔记本之外，没有参与本次回答，也不计入正文引用。导入后才会进入这个笔记本。"
          : "本轮站外检索没有返回建议；以下记录显示实际尝试的来源与检索词。"}
      </p>
      {egress && <GapEgressReceipt egress={egress} />}
      {suggestions.length > 0 && <ul className="answer-gap-consult-list">
        {suggestions.map((suggestion, index) => (
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
            {suggestion.actual_query?.trim() && (
              <p className="answer-gap-consult-query">检索词：{suggestion.actual_query.trim()}</p>
            )}
            {suggestion.summary && (
              <p className="answer-gap-consult-summary">{suggestion.summary}</p>
            )}
            <ImportRowButton
              controller={importController}
              rowKey={suggestion.url}
              url={suggestion.url}
              className="answer-gap-consult-import"
              errorClassName="answer-gap-consult-error"
              idleLabel="导入"
            />
          </li>
        ))}
      </ul>}
    </details>
  );
}
