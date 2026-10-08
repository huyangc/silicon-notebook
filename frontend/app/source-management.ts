/**
 * 隐藏合成源（记忆 / Knowhow 表格的同步行）不能用通用的「重新解析」「删除」「命令目录」
 * 来管理：后端对这两类来源的 `POST /sources/{id}/parse`、`DELETE /sources/{id}` 与七个
 * 命令目录端点一律回 404（记忆经记忆页删除、由记忆服务重新整理，Knowhow 行随表格同步）。
 * 来源详情窗可以从引用卡的「查看原文」打开这两类来源，所以这些入口必须在那里就不出现，
 * 而不是点了才报错。与后端 `source_routes._HIDDEN_SOURCE_TYPES` 是同一份集合。
 */
const HIDDEN_SOURCE_TYPES: ReadonlySet<string> = new Set(["memory", "knowhow"]);

/**
 * 公共知识库里由「贡献到公共知识库」生成的来源（后端
 * `app.domain.promotion_provenance.PROMOTION_SOURCE_TYPE`）。它可见、可删除（删除即删去
 * 它支撑的已收录知识条目），但没有文件可解析：后端对它的重新解析回 409。
 */
export const PROMOTION_SOURCE_TYPE = "promotion";

export function sourceDetailManageable(sourceType: string): boolean {
  return !HIDDEN_SOURCE_TYPES.has(sourceType);
}

/** 「重新解析」入口：可管理的来源里再去掉收录来源。 */
export function sourceDetailReparsable(sourceType: string): boolean {
  return sourceDetailManageable(sourceType) && sourceType !== PROMOTION_SOURCE_TYPE;
}

/**
 * 删除来源的确认文案。收录来源不生成知识，它是已收录知识条目的证据：删除后这些条目
 * 失去这份证据，只靠它支撑的条目一起删除，还有其他证据的条目保留（后端
 * `GovernanceStore.detach_promotion_sources_on`）。
 */
export function sourceDeleteMessage(sourceType: string, title: string): string {
  if (sourceType === PROMOTION_SOURCE_TYPE) {
    return `确定删除“${title}”吗？它支撑的已收录知识条目会失去这份证据：只靠它支撑的条目会一起删除，还有其他证据的条目会保留。`;
  }
  return `确定删除“${title}”吗？它的解析元素、候选知识和由该来源生成的已批准知识也会一起移除。`;
}

/** 来源类型标签：收录来源给中文名，其余沿用原来的类型串。 */
export function sourceTypeTag(sourceType: string): string | null {
  return sourceType === PROMOTION_SOURCE_TYPE ? "收录" : null;
}
