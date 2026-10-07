// 公开分享请求的失败出口，报告分享与会话分享共用（只此一份）。
//
// 409 `share_disclosure_required`：作者确认的条数与服务端此刻数出来的不相等（或根本没带确认
// 而范围里有个人记忆）。数字由服务端给，前端只负责把披露行就地更新成它——所以这里不走通用
// 人话层（那只会把它压成「操作有冲突」），而是转成类型化异常，`memoryCount` 就是新的确数。
// 其余失败（含别的 409、403）照常走人话层，状态码随错误带回，由调用方按 403 / 409 分流。

import { throwHumanizedHttpError } from "./errors.ts";

export class ShareDisclosureRequired extends Error {
  readonly memoryCount: number;
  readonly newMemoryCount: number;

  constructor(memoryCount: number, newMemoryCount: number) {
    super("share_disclosure_required");
    this.name = "ShareDisclosureRequired";
    this.memoryCount = memoryCount;
    this.newMemoryCount = newMemoryCount;
  }
}

/** 从 409 正文里认出 `share_disclosure_required`;认不出返回 null(落回通用错误路径)。
 *  code 与状态码成对匹配,且 `memory_count` 必须是非负整数。结构在 `detail` 下(FastAPI 形状),
 *  也容忍根层同形状。 */
export const parseShareDisclosureRequired = (
  status: number,
  body: unknown,
): { memoryCount: number; newMemoryCount: number } | null => {
  if (status !== 409 || typeof body !== "object" || body === null) return null;
  const root = body as Record<string, unknown>;
  const detail = typeof root.detail === "object" && root.detail !== null
    ? root.detail as Record<string, unknown>
    : root;
  if (detail.code !== "share_disclosure_required") return null;
  const count = detail.memory_count;
  if (typeof count !== "number" || !Number.isInteger(count) || count < 0) return null;
  const added = detail.new_memory_count;
  const newCount = typeof added === "number" && Number.isInteger(added) && added >= 0 ? added : 0;
  // 新增是总数的子集；自相矛盾的回执不当作披露确认（落回通用错误），不猜数字。
  if (newCount > count) return null;
  return { memoryCount: count, newMemoryCount: newCount };
};

/** 公开分享请求的失败出口:409 `share_disclosure_required` 转成带确数的类型化异常;
 *  其余失败照常走人话层。 */
export async function throwShareFailure(res: Response, tag: string): Promise<never> {
  if (res.status === 409) {
    // body 只能消费一次:探测读克隆,真正的报错仍交给原始 res 走人话层。
    let parsed: unknown;
    try {
      parsed = JSON.parse(await res.clone().text());
    } catch {
      parsed = undefined;
    }
    const required = parseShareDisclosureRequired(res.status, parsed);
    if (required) throw new ShareDisclosureRequired(required.memoryCount, required.newMemoryCount);
  }
  return throwHumanizedHttpError(res, tag);
}
