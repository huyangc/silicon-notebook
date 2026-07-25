import { useEffect, useState } from "react";
import { Check, X } from "lucide-react";

import {
  buildAskIntentConfirmation,
  missingRequiredIntentAnswers,
  type AskIntentConfirmation,
  type QueryIntentContract,
} from "./ask-intent-model";
import { modeLabel } from "./ask-modes";


const INTENT_TYPE_LABELS: Record<string, string> = {
  explain: "解释机理",
  compare: "比较分析",
  diagnose: "诊断问题",
  design: "设计方案",
  review: "综述评估",
  other: "综合分析",
};

const RESULT_SCOPE_LABELS: Record<QueryIntentContract["result_scope"], string> = {
  ranked: "相关性结果",
  complete: "完整清单",
  aggregate: "完整统计",
  hybrid: "完整清单 + 分析",
};


export function AskIntentReview({
  contract,
  busy = false,
  onConfirm,
  onCancel,
}: {
  contract: QueryIntentContract;
  busy?: boolean;
  onConfirm: (confirmation: AskIntentConfirmation) => void | Promise<void>;
  onCancel: () => void;
}) {
  const reasoningLabel = modeLabel("reasoning");
  const [resolvedQuestion, setResolvedQuestion] = useState(contract.resolved_question);
  const [answers, setAnswers] = useState<Record<string, string>>({});

  useEffect(() => {
    setResolvedQuestion(contract.resolved_question);
    setAnswers({});
  }, [contract]);

  const missingRequired = missingRequiredIntentAnswers(contract, answers);
  const confidence = Math.round(Math.max(0, Math.min(1, contract.confidence)) * 100);
  const details = [
    ["研究对象", contract.entities],
    ["比较维度", contract.comparison_axes],
    ["约束条件", contract.constraints],
    ["不纳入范围", contract.excluded_topics],
    ["成立前提", contract.assumptions],
    ["期望输出", contract.expected_output ? [contract.expected_output] : []],
  ] as const;

  return (
    <section className="report-intent-review ask-intent-review" aria-label={`确认${reasoningLabel}的问题理解`}>
      <div className="report-intent-review-head">
        <div>
          <h3>先补充问题信息</h3>
          <p>这一步只理解你的问题，不读取资料；确认后才会开始逐步检索。</p>
        </div>
        <div className="report-intent-meta">
          <span>{INTENT_TYPE_LABELS[contract.intent_type] || "综合分析"}</span>
          <span>{RESULT_SCOPE_LABELS[contract.result_scope]}</span>
          <span>理解置信度 {confidence}%</span>
        </div>
      </div>

      <label className="report-intent-question">
        <span>确认后的问题</span>
        <textarea
          rows={3}
          value={resolvedQuestion}
          disabled={busy}
          onChange={(event) => setResolvedQuestion(event.target.value)}
        />
      </label>

      <div className="report-clarification-list">
        {contract.ambiguities.map((item, index) => (
          <div className="report-clarification-card" key={item.id}>
            <span className="report-clarification-title">
              {index + 1}. {item.question}
              {item.required !== false && <em>必填</em>}
            </span>
            {item.reason && <small>{item.reason}</small>}
            {item.options && item.options.length > 0 && (
              <span className="report-clarification-options">
                {item.options.map((option) => (
                  <button
                    type="button"
                    key={option}
                    disabled={busy}
                    aria-pressed={(answers[item.id] || "") === option}
                    className={(answers[item.id] || "") === option ? "selected" : ""}
                    onClick={() => setAnswers((current) => ({ ...current, [item.id]: option }))}
                  >
                    {option}
                  </button>
                ))}
              </span>
            )}
            <textarea
              aria-label={`${item.question}的补充答案`}
              rows={2}
              value={answers[item.id] || ""}
              disabled={busy}
              placeholder="补充你的答案"
              onChange={(event) => setAnswers((current) => ({
                ...current,
                [item.id]: event.target.value,
              }))}
            />
          </div>
        ))}
      </div>

      {contract.mandatory_topics.length > 0 && (
        <div className="report-intent-block">
          <strong>{reasoningLabel}必须回答</strong>
          <ul>{contract.mandatory_topics.map((item) => <li key={item.id}>{item.question}</li>)}</ul>
        </div>
      )}

      <div className="report-intent-details">
        {details.map(([title, values]) => values.length > 0 && (
          <div key={title}>
            <strong>{title}</strong>
            <ul>{values.map((value) => <li key={value}>{value}</li>)}</ul>
          </div>
        ))}
      </div>

      <div className="report-intent-actions">
        <button type="button" className="button secondary" disabled={busy} onClick={onCancel}>
          <X size={15} /> 返回修改
        </button>
        <button
          type="button"
          className="button"
          disabled={busy || missingRequired || !resolvedQuestion.trim()}
          onClick={() => void onConfirm(buildAskIntentConfirmation(
            contract, resolvedQuestion, answers,
          ))}
        >
          <Check size={15} /> {busy ? "提交中…" : "确认并开始检索"}
        </button>
      </div>
    </section>
  );
}
