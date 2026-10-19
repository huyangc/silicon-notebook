/**
 * global-deleted-sources.ts
 *
 * 全局问答里「参与笔记本已被删除」的那几轮的**呈现口径**——唯一出处。
 *
 * 产品裁决（2026-10-19）：全局会话的某个参与笔记本被删后，会话对所有者仍可打开，
 * 问题与回答正文照常保留；来自已删笔记本的引用显示「来源已删除」且**不可点开**，
 * 留存期与笔记本内问答的删库留存相同（到期后这一轮自动清除）。
 *
 * 后端在所有者读到这一轮时附上 `deleted_notebook_ids`（这一轮提到的、已经不存在的
 * 笔记本）与 `retained_until`（这一轮的清除时刻）。这里在**读入口**（`global-ask-api.ts`
 * 的 `normalizeGlobalJob`）把它们落到 anchors / citations 上：
 *  · 属于已删笔记本的引用标 `source_deleted: true`，并去掉它名下的附图——正文行内
 *    图片、引用卡附图都从这一份数据出，去掉之后**一张都不会去取**；
 *  · 清单结果（枚举出的元素/知识对象）里属于已删笔记本的图片条目去掉 `asset_id`，
 *    同样不发任何取图请求。
 * 正文行内的标记（`answer-markdown.tsx`）对带标记的引用渲染成不可点的「来源已删除」，
 * 引用卡因此根本打不开，不会再向已删笔记本发出任何读取。
 *
 * 只认 `deleted_notebook_ids`：前端不自己猜「这个库是不是删了」（读不到名字的库也
 * 可能只是没有出现在当前列表里），判据只有后端那一份。没有已删笔记本的轮次原样返回
 * （同一个对象），下游按引用比较的缓存不受影响。
 */

/** 已删笔记本里的引用在界面上的说法。 */
export const DELETED_SOURCE_LABEL = "来源已删除";

type Markable = { notebook_id?: string; images?: unknown[]; source_deleted?: boolean };

type ReferenceLike = { anchor?: { source_deleted?: boolean }; citation?: { source_deleted?: boolean } };

/** 一条引用（anchor 优先、citation 兜底，与卡片其余字段同序）是否来自已删笔记本。 */
export function referenceSourceDeleted(reference: ReferenceLike): boolean {
  return Boolean(reference.anchor?.source_deleted || reference.citation?.source_deleted);
}

function mark<T extends Markable>(item: T, gone: ReadonlySet<string>): T {
  if (!item.notebook_id || !gone.has(item.notebook_id)) return item;
  const { images: _images, ...rest } = item;
  return { ...rest, source_deleted: true } as T;
}

function markAll<T extends Markable>(items: T[] | undefined, gone: ReadonlySet<string>): T[] | undefined {
  if (!items) return items;
  return items.map((item) => mark(item, gone));
}

type CollectionItemLike = { notebook_id?: string; asset_id?: string; citation?: Markable };
type RowLike = { citation?: Markable };
type ResultSetLike = { kind?: string };

function markResultSet<T extends ResultSetLike>(resultSet: T, gone: ReadonlySet<string>): T {
  const collection = resultSet as T & { items?: CollectionItemLike[] };
  if (resultSet.kind === "collection" && Array.isArray(collection.items)) {
    return {
      ...resultSet,
      items: collection.items.map((item) => item.notebook_id && gone.has(item.notebook_id)
        ? { ...item, asset_id: "", citation: item.citation ? mark(item.citation, gone) : item.citation }
        : item),
    };
  }
  const spreadsheet = resultSet as T & { rows?: RowLike[] };
  if (resultSet.kind === "spreadsheet" && Array.isArray(spreadsheet.rows)) {
    return {
      ...resultSet,
      rows: spreadsheet.rows.map((row) => row.citation ? { ...row, citation: mark(row.citation, gone) } : row),
    };
  }
  return resultSet;
}

type AnswerLike = {
  anchors?: Markable[];
  citations?: Markable[];
  result_sets?: ResultSetLike[];
};

function markAnswer<T extends AnswerLike>(answer: T, gone: ReadonlySet<string>): T {
  return {
    ...answer,
    anchors: markAll(answer.anchors, gone),
    citations: markAll(answer.citations, gone),
    ...(answer.result_sets ? { result_sets: answer.result_sets.map((set) => markResultSet(set, gone)) } : {}),
  };
}

type JobLike = {
  deleted_notebook_ids?: string[];
  answer?: AnswerLike | null;
  response?: AnswerLike | null;
};

/** 把一轮作业的 `deleted_notebook_ids` 落到它的引用上；没有已删笔记本时原样返回。 */
export function withDeletedSources<T extends JobLike>(job: T): T {
  const deleted = job.deleted_notebook_ids ?? [];
  if (!deleted.length) return job;
  const gone = new Set(deleted);
  return {
    ...job,
    answer: job.answer ? markAnswer(job.answer, gone) : job.answer,
    response: job.response ? markAnswer(job.response, gone) : job.response,
  };
}
