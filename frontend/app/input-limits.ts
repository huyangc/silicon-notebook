/**
 * 用户输入长度护栏的共用度量。
 *
 * 这里只放**尺子**，不放任何具体上限——每个上限归它自己那个 `*-api.ts`
 * （`REPORT_INPUT_LIMITS` / `ASK_INPUT_LIMITS` / `GROUP_INPUT_LIMITS`），
 * 与各自的后端常量成对维护。把尺子抽出来，是因为报告与问答两处都要按**同一种
 * 单位**与后端对账，而让 `ask-api` 去 import `report-api` 只是为了借一个纯函数，
 * 会凭空造出一条「问答依赖报告」的模块边界。
 */

/**
 * 一段文本的 **Unicode 码点**数——与后端 Pydantic `max_length` 数的是同一种单位。
 *
 * 刻意不用 `value.length`（也就是 `<textarea maxLength>` 用的那把尺）：那数的是
 * **UTF-16 code unit**，含 emoji 等非 BMP 字符时一个字符占两个，于是 4,000 的护栏
 * 会在 2,000 个 emoji 处就停手，而 API 其实收 4,000 个——两边号称「同一护栏」却对
 * 不上（codex #525 R2）。中文全在 BMP 内（1 码点 = 1 code unit），所以对绝大多数
 * 输入两者逐字相同；这里只是把「绝大多数」变成「全部」。
 *
 * 仓库里 `GROUP_INPUT_LIMITS` 仍用 `maxLength`，它不喂任何匿名投影，这处更严格是
 * 刻意的、不是不一致。
 */
export const countCodePoints = (value: string): number => Array.from(value).length;
