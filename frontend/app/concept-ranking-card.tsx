"use client";

import { LatexText } from "./citation-card";
import { verificationLabel, verificationOf } from "./citation-verification";
import type { ConceptRankingCoverage, ConceptRankingResult } from "./workspace-model";

function coverageNotes(coverage: ConceptRankingCoverage): string[] {
  const notes: string[] = [];
  if (coverage.exact) {
    notes.push(`按提及它的来源篇数统计，范围：${coverage.scope_sources} 篇来源`);
  } else {
    if (coverage.drifted) notes.push("统计期间来源有变化");
    if (coverage.candidates_lexical_only) notes.push("只按字面匹配找候选");
    if (coverage.path === "query") notes.push("只在与主题最相关的概念里排行");
    if (coverage.stale_counts) notes.push("统计建于较早的图谱状态");
    if (coverage.recount_capped || coverage.recount_skipped) notes.push("部分候选未计入");
    if (coverage.unnamed_dropped) notes.push("部分概念取不到名称");
  }
  if (coverage.duplicate_import_pairs > 0) notes.push("有疑似重复导入的来源，计数可能偏高");
  return notes;
}

/** The server already accumulates delivered page snapshots; render every supplied row. */
export function ConceptRankingCard({ resultSet, notebookNames, onOpenSource }: {
  resultSet: ConceptRankingResult;
  notebookNames: Record<string, string>;
  onOpenSource?: (sourceId: string, elementId?: string) => void;
}) {
  const multiNotebook = resultSet.sections.length > 1;
  return (
    <section className="answer-collection-result" aria-label="概念排行">
      <div className="answer-collection-result-heading"><strong>概念排行</strong></div>
      {resultSet.query && <p className="answer-collection-scope">主题：{resultSet.query}</p>}
      <div className="answer-collection-groups">
        {resultSet.sections.map(({ coverage, rows, notice }, sectionIndex) => (
          <section className="answer-collection-group" key={`${coverage.notebook_id}-${sectionIndex}`}>
            {multiNotebook && <h4 className="answer-collection-group-title">
              {notebookNames[coverage.notebook_id] || coverage.notebook_name}
            </h4>}
            {coverage.status !== "ok" && rows.length === 0 ? <p>{notice}</p> : <>
              {coverageNotes(coverage).map((note) => (
                <p className="answer-collection-coverage-note" key={note}>{note}</p>
              ))}
              {rows.length === 0 ? <p className="tool-hint">没有符合条件的概念</p> : (
                <div className="answer-table-wrap">
                  <table className="answer-table answer-knowhow-table">
                    <thead><tr><th>概念</th><th>来源数</th><th>提及条数</th></tr></thead>
                    <tbody>{rows.map((row, rowIndex) => {
                      const groupNumber = /^g(\d+)$/.exec(row.same_name_group ?? "")?.[1];
                      return <tr key={`${row.notebook_id}-${rowIndex}`}>
                        <td>
                          <strong><LatexText text={row.name} /></strong>
                          {groupNumber && <small className="answer-collection-scope">
                            可能与第 {groupNumber} 组同名概念重复
                          </small>}
                          {row.citations.map((citation, citationIndex) => (
                            <div className="answer-collection-kg-citation" key={`${citation.source_id}-${citation.element_id}-${citationIndex}`}>
                              <details className="answer-collection-citation">
                                <summary>原文引用：{citation.label || citation.location_label || "来源片段"}</summary>
                                {citation.quoted_span && <p><LatexText text={citation.quoted_span} /></p>}
                              </details>
                              {verificationOf(citation) && <span>{verificationLabel(verificationOf(citation))}</span>}
                              {onOpenSource && citation.source_id && !verificationOf(citation) && (
                                <button type="button" className="answer-collection-open"
                                  onClick={() => onOpenSource(citation.source_id, citation.element_id)}>
                                  查看原文
                                </button>
                              )}
                            </div>
                          ))}
                        </td>
                        <td>{row.source_count}</td><td>{row.object_count}</td>
                      </tr>;
                    })}</tbody>
                  </table>
                </div>
              )}
            </>}
          </section>
        ))}
        {resultSet.sections.length === 0 && <p className="tool-hint">没有符合条件的概念</p>}
      </div>
    </section>
  );
}
