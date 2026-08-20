// Agentic Memory P3(T9)——「我的回答偏好」弹窗的纯函数层：枚举→界面词映射、
// 服务端 search_profile 文档的防御性解析、以及 PATCH 请求体的三态构造（没传/
// 传值/传 null）。真源契约见 backend/app/services/search_profile.py 的模块
// docstring 与 backend/app/models/identity.py 的封闭值域；本文件只做前端侧的
// 镜像，不重新定义语义。

import { label } from "./vocabulary.ts";

export type AnswerLanguage = "auto" | "zh" | "en";
export type AnswerShape = "auto" | "bullets" | "table_first" | "prose";
export type AnswerDetail = "auto" | "concise" | "detailed";
export type SearchProfileOrigin = "user" | "job";
export type SearchProfileFieldName =
  | "answer_language"
  | "answer_shape"
  | "answer_detail"
  | "domain_terms";

// 镜像 backend/app/models/identity.py 的 SEARCH_PROFILE_DOMAIN_TERMS_MAX /
// SEARCH_PROFILE_DOMAIN_TERM_MAX_CHARS——数值上限的精确登记在 docs/product-and-api*.md
// （T10），这里只是前端侧输入校验要用的同一对常量，不是第二份真源。
export const DOMAIN_TERMS_MAX = 10;
export const DOMAIN_TERM_MAX_CHARS = 32;

export const ANSWER_LANGUAGE_OPTIONS: AnswerLanguage[] = ["auto", "zh", "en"];
export const ANSWER_SHAPE_OPTIONS: AnswerShape[] = ["auto", "bullets", "table_first", "prose"];
export const ANSWER_DETAIL_OPTIONS: AnswerDetail[] = ["auto", "concise", "detailed"];

// Record<string,string>（而非 Record<AnswerLanguage,string> 这类字面量联合）是
// 刻意的——与 vocabulary.ts 里其余表（REPORT_DEPTH、ASK_STATUS…）同一形状，保持
// label() 的一处签名对所有调用方都适用；「取值全覆盖」由 search-profile-model.test.mjs
// 对着上面的 OPTIONS 数组逐个断言，而不是指望 TS 的结构类型来保证。
const ANSWER_LANGUAGE_LABELS: Record<string, string> = {
  auto: "跟随提问",
  zh: "中文",
  en: "英文",
};
const ANSWER_SHAPE_LABELS: Record<string, string> = {
  auto: "自动",
  bullets: "要点清单",
  table_first: "表格优先",
  prose: "连贯段落",
};
const ANSWER_DETAIL_LABELS: Record<string, string> = {
  auto: "自动",
  concise: "简明",
  detailed: "详尽",
};
const ORIGIN_LABELS: Record<string, string> = {
  user: "你设置的",
  job: "自动判断",
};

export const answerLanguageLabel = (value: string): string =>
  label(ANSWER_LANGUAGE_LABELS, value, "跟随提问");
export const answerShapeLabel = (value: string): string =>
  label(ANSWER_SHAPE_LABELS, value, "自动");
export const answerDetailLabel = (value: string): string =>
  label(ANSWER_DETAIL_LABELS, value, "自动");
export const searchProfileOriginLabel = (value: string): string =>
  label(ORIGIN_LABELS, value, "");

// --------------------------------------------------------------------------
// 服务端文档解析（防御性）——AuthUser.search_profile 在 wire 上是后端
// UserProfile.search_profile: Optional[Dict[str, Any]]，形状为
// {"version": 1, "fields": {field: {value, origin, updated_at}}}。后端
// parse_search_profile 已经在写入这个字段前做过同样的校验（未知字段/非法
// origin/非法值域一律丢弃），这里再做一遍不是不信任后端，而是这份文档是宽松
// 类型的 Dict[str, Any]，前端消费方不应该对着一个 `any` 硬转型——与
// system-api.ts 对 /system/config 的防御性解析同一惯例。
// --------------------------------------------------------------------------

export type ParsedFieldState<T> = { value: T; origin: SearchProfileOrigin } | null;

export type ParsedSearchProfile = {
  answer_language: ParsedFieldState<AnswerLanguage>;
  answer_shape: ParsedFieldState<AnswerShape>;
  answer_detail: ParsedFieldState<AnswerDetail>;
  domain_terms: ParsedFieldState<string[]>;
};

function emptyParsedProfile(): ParsedSearchProfile {
  return { answer_language: null, answer_shape: null, answer_detail: null, domain_terms: null };
}

function isValidDomainTerms(value: unknown): value is string[] {
  return (
    Array.isArray(value)
    && value.length <= DOMAIN_TERMS_MAX
    && value.every((term) => (
      typeof term === "string" && term.length > 0 && term.length <= DOMAIN_TERM_MAX_CHARS
    ))
  );
}

function readField<T>(
  fields: Record<string, unknown>,
  name: SearchProfileFieldName,
  isValid: (value: unknown) => value is T,
): ParsedFieldState<T> {
  const entry = fields[name];
  if (!entry || typeof entry !== "object" || Array.isArray(entry)) return null;
  const record = entry as Record<string, unknown>;
  const origin = record.origin;
  if (origin !== "user" && origin !== "job") return null;
  if (!isValid(record.value)) return null;
  return { value: record.value, origin };
}

/** 未知/损坏/旧版文档一律 fail-open 到「未设置」，绝不让一个字段的解析失败拖垮
 * 整个弹窗——与 system-api.ts parseSystemConfiguration 的 fail-open 方向一致。 */
export function parseSearchProfile(raw: unknown): ParsedSearchProfile {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return emptyParsedProfile();
  const fields = (raw as Record<string, unknown>).fields;
  if (!fields || typeof fields !== "object" || Array.isArray(fields)) return emptyParsedProfile();
  const record = fields as Record<string, unknown>;
  return {
    answer_language: readField(record, "answer_language", (v): v is AnswerLanguage => (
      typeof v === "string" && (ANSWER_LANGUAGE_OPTIONS as string[]).includes(v)
    )),
    answer_shape: readField(record, "answer_shape", (v): v is AnswerShape => (
      typeof v === "string" && (ANSWER_SHAPE_OPTIONS as string[]).includes(v)
    )),
    answer_detail: readField(record, "answer_detail", (v): v is AnswerDetail => (
      typeof v === "string" && (ANSWER_DETAIL_OPTIONS as string[]).includes(v)
    )),
    domain_terms: readField(record, "domain_terms", isValidDomainTerms),
  };
}

// --------------------------------------------------------------------------
// 草稿 → PATCH 请求体：三态构造。
// --------------------------------------------------------------------------

/**
 * 一个字段的编辑草稿。三态互斥：
 *  - "unset"：用户没碰过这个字段，PATCH 请求体里必须完全不出现这个 key
 *    （对应后端 `model_fields_set` 里「没传」——维持原值不动）。
 *  - "set"：用户显式选了一个值，**包含 "auto"**——"auto" 对
 *    answer_language/answer_shape/answer_detail 是合法值，用户显式选它与「没碰
 *    过这个字段」是两件不同的事（见 search_profile.py 模块 docstring「Clearing
 *    is deletion, not a stored auto」：显式 "auto" 会冻结该字段，此后 T7 归纳
 *    job 不会再碰它）。
 *  - "clear"：用户点了「恢复自动」，PATCH 请求体里该 key 显式为 `null`（对应
 *    后端「传了 null」——删除该字段条目，交还给 job 重新归纳）。
 */
export type FieldDraft<T> =
  | { kind: "unset" }
  | { kind: "set"; value: T }
  | { kind: "clear" };

export type SearchProfileDrafts = {
  answer_language: FieldDraft<AnswerLanguage>;
  answer_shape: FieldDraft<AnswerShape>;
  answer_detail: FieldDraft<AnswerDetail>;
  domain_terms: FieldDraft<string[]>;
};

export function emptyDrafts(): SearchProfileDrafts {
  return {
    answer_language: { kind: "unset" },
    answer_shape: { kind: "unset" },
    answer_detail: { kind: "unset" },
    domain_terms: { kind: "unset" },
  };
}

export type SearchProfilePatchBody = {
  answer_language?: AnswerLanguage | null;
  answer_shape?: AnswerShape | null;
  answer_detail?: AnswerDetail | null;
  domain_terms?: string[] | null;
};

const SEARCH_PROFILE_FIELD_NAMES: SearchProfileFieldName[] = [
  "answer_language", "answer_shape", "answer_detail", "domain_terms",
];

/**
 * 把草稿折成请求体。"unset" 字段整体不写进返回对象——调用方经 `JSON.stringify`
 * 序列化时，缺失的 key 会被丢弃（JSON.stringify 对 `undefined` 属性值的既有
 * 行为），与后端 `payload.model_fields_set` 判定「没传」严格对应；"clear" 显式
 * 写 `null`；"set" 写具体值（含 "auto"）。⚠ 不能用「值是不是等于默认值」这类
 * 隐式判断代替显式的 `kind`——那会让「用户明确选了跟随提问」与「用户压根没碰过
 * 这个下拉框」在请求体里长得一样，而它们对后端是两种不同的效果。
 */
export function buildSearchProfilePatch(drafts: SearchProfileDrafts): SearchProfilePatchBody {
  const body: SearchProfilePatchBody = {};
  for (const field of SEARCH_PROFILE_FIELD_NAMES) {
    const draft = drafts[field];
    if (draft.kind === "unset") continue;
    if (draft.kind === "clear") {
      (body as Record<string, unknown>)[field] = null;
      continue;
    }
    (body as Record<string, unknown>)[field] = draft.value;
  }
  return body;
}

export function hasPendingChanges(drafts: SearchProfileDrafts): boolean {
  return SEARCH_PROFILE_FIELD_NAMES.some((field) => drafts[field].kind !== "unset");
}

// --------------------------------------------------------------------------
// 术语标签输入校验——纯函数，供 search-profile-modal.tsx 的 chip 输入框调用。
// --------------------------------------------------------------------------

export type DomainTermRejection = "empty" | "too_long" | "duplicate" | "limit_reached";

export type DomainTermValidation =
  | { ok: true; term: string }
  | { ok: false; reason: DomainTermRejection };

/** 校验一条待添加的术语；`existing` 是当前已有的术语列表（去重按 trim 后逐字相同）。 */
export function validateNewDomainTerm(existing: string[], candidateRaw: string): DomainTermValidation {
  const term = candidateRaw.trim();
  if (!term) return { ok: false, reason: "empty" };
  if (term.length > DOMAIN_TERM_MAX_CHARS) return { ok: false, reason: "too_long" };
  if (existing.includes(term)) return { ok: false, reason: "duplicate" };
  if (existing.length >= DOMAIN_TERMS_MAX) return { ok: false, reason: "limit_reached" };
  return { ok: true, term };
}

const DOMAIN_TERM_REJECTION_MESSAGES: Record<string, string> = {
  empty: "请输入术语内容",
  too_long: `单条术语最多 ${DOMAIN_TERM_MAX_CHARS} 个字符`,
  duplicate: "这条术语已经添加过了",
  limit_reached: `最多添加 ${DOMAIN_TERMS_MAX} 条常用术语`,
};

export function domainTermRejectionMessage(reason: DomainTermRejection): string {
  return label(DOMAIN_TERM_REJECTION_MESSAGES, reason, "无法添加这条术语");
}
