import type { NotebookAnalytics } from "./workspace-model";

/** 全局问答那一份的标注:只在有全局问答时出现,数字是前面那个数里的子集。 */
function globalShare(count: number) {
  return count > 0 ? `（其中全局问答 ${count} 条）` : "";
}

/**
 * 知识分析看板的「回答质量」与「低分提问（知识缺口）」两段。
 *
 * 提问数、👍/👎 与有用率都含归入本库的全局问答(检索到或引用了本库的那几轮,后端
 * `global_ask_attribution` 口径,提问数读 `global_ask_job_notebooks`);全局那一份
 * 各自另行标注。低分提问列表只列笔记本内的提问原文——全局问答的原文不在这里出现,
 * 只在列表末尾计数。
 */
export function AnalyticsAnswerQuality({ analytics }: { analytics: NotebookAnalytics }) {
  const globalAnswers = analytics.answers_global ?? 0;
  const globalUseful = analytics.feedback_useful_global ?? 0;
  const globalNotUseful = analytics.feedback_not_useful_global ?? 0;
  const showLowRated = analytics.low_rated_questions.length > 0 || globalNotUseful > 0;
  return (
    <>
      <p className="section-title">回答质量</p>
      <div className="tag-row">
        <span className="tag">提问 {analytics.answers_total}{globalShare(globalAnswers)}</span>
        <span className="tag">👍 {analytics.feedback_useful}{globalShare(globalUseful)}</span>
        <span className="tag">👎 {analytics.feedback_not_useful}{globalShare(globalNotUseful)}</span>
        <span className="tag">有用率 {Math.round(analytics.usefulness_rate * 100)}%</span>
      </div>
      {showLowRated && (
        <>
          <p className="section-title">低分提问（知识缺口）</p>
          <div className="stack">
            {analytics.low_rated_questions.map((q) => <div className="checklist-row" key={q}>{q}</div>)}
            {globalNotUseful > 0 && <p className="tool-hint">另有 {globalNotUseful} 条来自全局问答</p>}
          </div>
        </>
      )}
    </>
  );
}
