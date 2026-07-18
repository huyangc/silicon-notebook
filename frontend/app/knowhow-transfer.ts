// Knowhow 表跨 notebook 传输(复制/移动)— 网络客户端(纯逻辑在 transfer-model.ts
// 单测)。自带 fetch 封装,可在 `node --test` 下运行而不 import React 页面模块。
// 镜像 notebook-share.ts 的样板。

import { authHeaders } from "./auth.ts";
import { throwHumanizedHttpError } from "./errors.ts";
import { knowhowTransferBody, parseCleanupFailure, type TransferMode } from "./transfer-model.ts";

// 409 source_cleanup_failed 的专用错误:复制已经提交、副本已在目标 notebook
// 落地,只是源表清理(拆投影/删源表)失败。调用方(C3)用 `instanceof` 分支出
// "副本已存在,别再盲目重试"的提示,并可以直接用 newTableId 跳到副本。
// 判定逻辑本身在 transfer-model.ts 的 parseCleanupFailure() 里,是纯函数、有
// 单测——这里只管拿它的结果转成一个类型化的异常。
export class KnowhowSourceCleanupError extends Error {
  readonly newTableId: string;

  constructor(newTableId: string, message: string) {
    super(message);
    this.name = "KnowhowSourceCleanupError";
    this.newTableId = newTableId;
  }
}

const API_BASE =
  (typeof process !== "undefined"
    ? process.env?.NEXT_PUBLIC_API_BASE_URL
    : undefined) ?? "http://127.0.0.1:8000/api";

async function apiFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(API_BASE + url, {
    headers: { "Content-Type": "application/json", ...authHeaders() },
    ...init,
  });
  if (!res.ok) {
    // 先在一份 clone 上探测 409 source_cleanup_failed 的结构化形状——body 只能
    // 消费一次(见 errors.ts readHttpError 头注释),原始 res 留给下面
    // throwHumanizedHttpError() 读,不能在这里抢先吃掉。非 JSON(或非该形状)
    // 一律落回通用的人话层错误,与 notebook-share.ts 的 apiFetch 行为一致。
    let parsed: unknown;
    try {
      parsed = JSON.parse(await res.clone().text());
    } catch {
      parsed = undefined;
    }
    const cleanup = parseCleanupFailure(res.status, parsed);
    if (cleanup) throw new KnowhowSourceCleanupError(cleanup.newTableId, cleanup.message);
    // 其余一律走通用人话层:原始诊断进 console,用户看按状态码泛化的中文
    // (401/403/404/409 语义保留),不再裸拼 `${status} ${text}`。
    await throwHumanizedHttpError(res, "knowhow-transfer");
  }
  if (res.status === 204) return null as T;
  return res.json() as Promise<T>;
}

// 把一张 knowhow 表复制/移动到另一个 notebook。
// - copy:源保持不动,目标新增一份独立副本。
// - move:目标新增副本后删除源;若副本已提交但源清理失败,后端答 409
//   source_cleanup_failed——上面的 apiFetch 会把它转成 KnowhowSourceCleanupError
//   抛出(而不是拍扁成字符串),调用方按需 catch。
export const transferKnowhowTable = (
  notebookId: string,
  tableId: string,
  targetNotebookId: string,
  mode: TransferMode
): Promise<{ new_table_id: string }> =>
  apiFetch(`/notebooks/${notebookId}/knowhow/${tableId}/transfer`, {
    method: "POST",
    body: JSON.stringify(knowhowTransferBody(targetNotebookId, mode)),
  });
