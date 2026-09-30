/**
 * 隐藏合成源（记忆 / Knowhow 表格的同步行）不能用通用的「重新解析」「删除」「命令目录」
 * 来管理：后端对这两类来源的 `POST /sources/{id}/parse`、`DELETE /sources/{id}` 与七个
 * 命令目录端点一律回 404（记忆经记忆页删除、由记忆服务重新整理，Knowhow 行随表格同步）。
 * 来源详情窗可以从引用卡的「查看原文」打开这两类来源，所以这些入口必须在那里就不出现，
 * 而不是点了才报错。与后端 `source_routes._HIDDEN_SOURCE_TYPES` 是同一份集合。
 */
const HIDDEN_SOURCE_TYPES: ReadonlySet<string> = new Set(["memory", "knowhow"]);

export function sourceDetailManageable(sourceType: string): boolean {
  return !HIDDEN_SOURCE_TYPES.has(sourceType);
}
