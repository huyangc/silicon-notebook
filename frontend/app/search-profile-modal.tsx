"use client";

import { useState, type FormEvent, type KeyboardEvent } from "react";

import type { AuthUser } from "./auth.ts";
import { patchSearchProfile } from "./auth.ts";
import { toUserMessage } from "./errors.ts";
import { FloatingModalCard } from "./floating-modal-card.tsx";
import {
  ANSWER_DETAIL_OPTIONS,
  ANSWER_LANGUAGE_OPTIONS,
  ANSWER_SHAPE_OPTIONS,
  DOMAIN_TERMS_MAX,
  answerDetailLabel,
  answerLanguageLabel,
  answerShapeLabel,
  buildSearchProfilePatch,
  domainTermRejectionMessage,
  emptyDrafts,
  hasPendingChanges,
  parseSearchProfile,
  searchProfileOriginLabel,
  validateNewDomainTerm,
  type AnswerDetail,
  type AnswerLanguage,
  type AnswerShape,
  type FieldDraft,
  type SearchProfileDrafts,
  type SearchProfileFieldName,
} from "./search-profile-model.ts";

type SearchProfileModalProps = {
  currentUser: AuthUser;
  onSaved: (user: AuthUser) => void;
  onClose: () => void;
  interactive?: boolean;
  zIndex?: number;
};

const DEFAULT_LANGUAGE: AnswerLanguage = "auto";
const DEFAULT_SHAPE: AnswerShape = "auto";
const DEFAULT_DETAIL: AnswerDetail = "auto";

/**
 * 「我的回答偏好」弹窗（Agentic Memory P3, T9）。骨架镜像
 * password-change-modal.tsx（utility-modal > FloatingModalCard.utility-modal-card
 * .narrow > source-modal-header + 拖动手柄 + .edit-form + .modal-actions.padded）。
 * 表单是「本地草稿 + 显式保存」而非逐项即时提交——一次 PATCH 提交本轮所有改动，
 * 避免每改一个下拉框就打一次请求。
 *
 * 三态草稿（见 search-profile-model.ts 的 FieldDraft）：下拉框选任意选项（含
 * “自动”/“跟随提问”）都是显式 `set`；「恢复自动」是显式 `clear`（清空该字段，
 * 交还给后续的自动归纳）；两者对后端语义不同，草稿状态因此必须分开表达，不能
 * 用「当前显示值 == 默认值」反推用户有没有碰过这个字段。
 */
export function SearchProfileModal({ currentUser, onSaved, onClose, interactive = true, zIndex }: SearchProfileModalProps) {
  const parsed = parseSearchProfile(currentUser.search_profile);

  const [drafts, setDrafts] = useState<SearchProfileDrafts>(emptyDrafts());
  const [termDraft, setTermDraft] = useState("");
  const [termError, setTermError] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  // 当前应该显示在控件里的值：草稿覆盖服务端值，服务端值覆盖字段默认值。
  const displayedTerms: string[] = (() => {
    const draft = drafts.domain_terms;
    if (draft.kind === "set") return draft.value;
    if (draft.kind === "clear") return [];
    return parsed.domain_terms?.value ?? [];
  })();

  function displayedValue<T extends string>(field: SearchProfileFieldName, fallback: T): T {
    const draft = drafts[field] as FieldDraft<T>;
    if (draft.kind === "set") return draft.value;
    if (draft.kind === "clear") return fallback;
    const state = parsed[field] as { value: T; origin: string } | null;
    return state?.value ?? fallback;
  }

  function setFieldValue<T extends string>(field: SearchProfileFieldName, value: T) {
    setDrafts((prev) => ({ ...prev, [field]: { kind: "set", value } }));
  }

  function resetField(field: SearchProfileFieldName) {
    setDrafts((prev) => ({ ...prev, [field]: { kind: "clear" } }));
    if (field === "domain_terms") setTermError("");
  }

  function resetAll() {
    setDrafts({
      answer_language: { kind: "clear" },
      answer_shape: { kind: "clear" },
      answer_detail: { kind: "clear" },
      domain_terms: { kind: "clear" },
    });
    setTermDraft("");
    setTermError("");
  }

  function addTerm() {
    const result = validateNewDomainTerm(displayedTerms, termDraft);
    if (!result.ok) {
      setTermError(domainTermRejectionMessage(result.reason));
      return;
    }
    setDrafts((prev) => ({
      ...prev,
      domain_terms: { kind: "set", value: [...displayedTerms, result.term] },
    }));
    setTermDraft("");
    setTermError("");
  }

  function removeTerm(term: string) {
    setDrafts((prev) => ({
      ...prev,
      domain_terms: { kind: "set", value: displayedTerms.filter((t) => t !== term) },
    }));
    setTermError("");
  }

  function handleTermKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Enter" || event.key === ",") {
      event.preventDefault();
      addTerm();
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    if (!hasPendingChanges(drafts)) {
      onClose();
      return;
    }
    setBusy(true);
    try {
      const updated = await patchSearchProfile(buildSearchProfilePatch(drafts));
      onSaved(updated);
      onClose();
    } catch (err) {
      setError(toUserMessage(err, "保存失败，请稍后重试"));
    } finally {
      setBusy(false);
    }
  }

  function originBadge(field: SearchProfileFieldName) {
    // 草稿一旦改动，服务端的旧来源标记就不再准确（要么马上被覆盖，要么被清空），
    // 保存前不再显示，避免「你设置的」这个标记显得比它实际过时的状态更权威。
    if (drafts[field].kind !== "unset") return null;
    const state = parsed[field];
    if (!state) return null;
    return (
      <>
        <span className="badge search-profile-origin">{searchProfileOriginLabel(state.origin)}</span>
        {state.origin === "job" ? (
          // codex #535 R2 P2：推断值在下拉框里已是选中项，再选同一项不触发
          // onChange，用户没有任何入口把它确认成「你设置的」（只有 user 来源
          // 的值才会注入回答提示）。这颗按钮把当前推断值显式写进草稿，保存
          // 即完成 origin=user 的确认写入。
          <button
            type="button"
            className="search-profile-field-adopt"
            disabled={busy}
            onClick={() => setDrafts((prev) => ({
              ...prev,
              [field]: { kind: "set", value: state.value },
            }))}
          >
            设为你的选择
          </button>
        ) : null}
      </>
    );
  }

  return (
    <section className="utility-modal" role="dialog" aria-modal={interactive} aria-hidden={!interactive} inert={interactive ? undefined : true} style={{ zIndex }}>
      <FloatingModalCard storageKey="searchProfile.window" className="utility-modal-card narrow search-profile-modal">
        {(floating) => (<>
        <div className="source-modal-header" {...floating.dragHandleProps}>
          <div>
            <h2>我的回答偏好</h2>
            <p>这些偏好只影响回答的组织方式与用词，不会改变检索范围，也不会改变依据。</p>
          </div>
          <button className="icon-button" disabled={busy} onClick={onClose} title="Close">×</button>
        </div>
        <form id="search-profile-form" className="edit-form" onSubmit={(event) => { void submit(event); }}>
          <div className="search-profile-toolbar">
            <button
              type="button"
              className="search-profile-reset-all"
              disabled={busy}
              onClick={resetAll}
            >
              全部恢复自动
            </button>
          </div>

          <div className="search-profile-field">
            <div className="search-profile-field-head">
              <span>回答语言</span>
              {originBadge("answer_language")}
            </div>
            <div className="search-profile-field-body">
              <select
                aria-label="回答语言"
                disabled={busy}
                value={displayedValue<AnswerLanguage>("answer_language", DEFAULT_LANGUAGE)}
                onChange={(event) => setFieldValue("answer_language", event.target.value as AnswerLanguage)}
              >
                {ANSWER_LANGUAGE_OPTIONS.map((option) => (
                  <option key={option} value={option}>{answerLanguageLabel(option)}</option>
                ))}
              </select>
              <button
                type="button"
                className="search-profile-field-reset"
                disabled={busy}
                onClick={() => resetField("answer_language")}
              >
                恢复自动
              </button>
            </div>
          </div>

          <div className="search-profile-field">
            <div className="search-profile-field-head">
              <span>组织方式</span>
              {originBadge("answer_shape")}
            </div>
            <div className="search-profile-field-body">
              <select
                aria-label="组织方式"
                disabled={busy}
                value={displayedValue<AnswerShape>("answer_shape", DEFAULT_SHAPE)}
                onChange={(event) => setFieldValue("answer_shape", event.target.value as AnswerShape)}
              >
                {ANSWER_SHAPE_OPTIONS.map((option) => (
                  <option key={option} value={option}>{answerShapeLabel(option)}</option>
                ))}
              </select>
              <button
                type="button"
                className="search-profile-field-reset"
                disabled={busy}
                onClick={() => resetField("answer_shape")}
              >
                恢复自动
              </button>
            </div>
          </div>

          <div className="search-profile-field">
            <div className="search-profile-field-head">
              <span>详略</span>
              {originBadge("answer_detail")}
            </div>
            <div className="search-profile-field-body">
              <select
                aria-label="详略"
                disabled={busy}
                value={displayedValue<AnswerDetail>("answer_detail", DEFAULT_DETAIL)}
                onChange={(event) => setFieldValue("answer_detail", event.target.value as AnswerDetail)}
              >
                {ANSWER_DETAIL_OPTIONS.map((option) => (
                  <option key={option} value={option}>{answerDetailLabel(option)}</option>
                ))}
              </select>
              <button
                type="button"
                className="search-profile-field-reset"
                disabled={busy}
                onClick={() => resetField("answer_detail")}
              >
                恢复自动
              </button>
            </div>
          </div>

          <div className="search-profile-field">
            <div className="search-profile-field-head">
              <span>常用术语</span>
              {originBadge("domain_terms")}
            </div>
            <div className="search-profile-terms">
              {displayedTerms.length > 0 && (
                <div className="tag-row">
                  {displayedTerms.map((term) => (
                    <button
                      type="button"
                      key={term}
                      className="tag"
                      disabled={busy}
                      title="移除这条术语"
                      onClick={() => removeTerm(term)}
                    >
                      {term} ×
                    </button>
                  ))}
                </div>
              )}
              <div className="search-profile-term-input">
                <input
                  type="text"
                  aria-label="常用术语"
                  placeholder="输入术语后按回车添加"
                  value={termDraft}
                  disabled={busy || displayedTerms.length >= DOMAIN_TERMS_MAX}
                  onChange={(event) => { setTermDraft(event.target.value); setTermError(""); }}
                  onKeyDown={handleTermKeyDown}
                />
                <button
                  type="button"
                  className="search-profile-field-reset"
                  disabled={busy}
                  onClick={() => resetField("domain_terms")}
                >
                  恢复自动
                </button>
              </div>
              {termError && <p className="search-profile-term-hint">{termError}</p>}
            </div>
          </div>

          {error && <p className="password-change-status error">{error}</p>}
        </form>
        <div className="modal-actions padded">
          <button type="button" className="sort-button" disabled={busy} onClick={onClose}>取消</button>
          <button type="submit" form="search-profile-form" className="new-pill" disabled={busy}>
            {busy ? "保存中…" : "保存"}
          </button>
        </div>
        </>)}
      </FloatingModalCard>
    </section>
  );
}
