import { useEffect, useState } from "react";
import { Check, ChevronRight, X } from "lucide-react";

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
  understandingMs,
  onConfirm,
  onCancel,
}: {
  contract: QueryIntentContract;
  busy?: boolean;
  // 产出这份合同花掉的墙钟时间,原样带进确认里 —— 用户在这张卡片上思考的时间
  // 不属于系统耗时,所以只透传理解阶段的那一段。
  understandingMs?: number;
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
  const answered = (id: string) => Boolean((answers[id] || "").trim());
  const pending = contract.ambiguities.filter(
    (item) => item.required !== false && !answered(item.id),
  ).length;
  const details = [
    ["必须回答", contract.mandatory_topics.map((item) => item.question)],
    ["研究对象", contract.entities],
    ["比较维度", contract.comparison_axes],
    ["约束条件", contract.constraints],
    ["不纳入范围", contract.excluded_topics],
    ["成立前提", contract.assumptions],
    ["期望输出", contract.expected_output ? [contract.expected_output] : []],
  ] as const;
  // 「必须回答」是整句,其余是短标签 —— 短标签排成 chips,整句排成列表。
  const asChips = new Set(["研究对象", "比较维度", "不纳入范围"]);

  return (
    <section
      className="intent-card"
      aria-label={`确认${reasoningLabel}的问题理解`}
    >
      <div className="intent-card-head">
        <div>
          <h3>先补充问题信息</h3>
          <p>这一步只理解你的问题，不读取资料；确认后才会开始逐步检索。</p>
        </div>
        {/* 按钮钉在卡头:确认动作不该藏在一张最高 58vh、可滚动的卡片底部。 */}
        <div className="intent-card-actions">
          <button type="button" className="button secondary" disabled={busy} onClick={onCancel}>
            <X size={15} /> 返回修改
          </button>
          <button
            type="button"
            className="button"
            disabled={busy || missingRequired || !resolvedQuestion.trim()}
            onClick={() => void onConfirm(buildAskIntentConfirmation(
              contract, resolvedQuestion, answers, understandingMs,
            ))}
          >
            <Check size={15} /> {busy ? "提交中…" : "确认并开始检索"}
          </button>
        </div>
      </div>

      <div className="intent-card-zone">
        <p className="intent-card-eyebrow">
          待你确认
          {pending > 0 && <em className="todo">还差 {pending} 项</em>}
          {pending === 0 && contract.ambiguities.length > 0 && <em className="done">已补齐</em>}
        </p>

        <label className="intent-card-question">
          <span>确认后的问题</span>
          <textarea
            rows={2}
            value={resolvedQuestion}
            disabled={busy}
            onChange={(event) => setResolvedQuestion(event.target.value)}
          />
        </label>

        <div className="intent-card-asks">
          {contract.ambiguities.map((item, index) => (
            <div
              className={`intent-card-ask${answered(item.id) ? " answered" : ""}`}
              key={item.id}
            >
              {/* 刻度即状态:未答是琥珀空心编号,答完变蓝底对勾。 */}
              <span className="intent-card-mark" aria-hidden="true">
                {answered(item.id) ? <Check size={12} /> : index + 1}
              </span>
              <span className="intent-card-ask-question">
                {item.question}
                {item.required !== false && <em>必填</em>}
              </span>
              {item.reason && <small>{item.reason}</small>}
              {item.options && item.options.length > 0 && (
                <span className="intent-card-options">
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
      </div>

      {/* 只读读出:系统理解成了什么。默认展开——这张卡片的意义正是核对它。 */}
      <details className="intent-card-readout" open>
        <summary>
          <ChevronRight size={14} />
          系统的理解
          <span className="intent-card-tags">
            <span>{INTENT_TYPE_LABELS[contract.intent_type] || "综合分析"}</span>
            <span>{RESULT_SCOPE_LABELS[contract.result_scope]}</span>
            <span>置信度 {confidence}%</span>
          </span>
        </summary>
        <dl className="intent-card-rows">
          {details.map(([title, values]) => values.length > 0 && (
            <div className="intent-card-row" key={title}>
              <dt>{title}</dt>
              <dd>
                {asChips.has(title) ? (
                  <span className="chips">
                    {values.map((value) => <span key={value}>{value}</span>)}
                  </span>
                ) : values.length === 1 ? values[0] : (
                  <ul>{values.map((value) => <li key={value}>{value}</li>)}</ul>
                )}
              </dd>
            </div>
          ))}
        </dl>
      </details>
    </section>
  );
}
