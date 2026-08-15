/**
 * markdown-gfm.ts
 *
 * 渲染**模型产出文本**（Ask 回答、Memory 卡片、深度报告、报告公开分享页）的
 * GFM 插件真源。所有这些面共用同一个元组，绝不再各自写裸 `remarkGfm`。
 *
 * 为什么必须显式关掉 `singleTilde`：remark-gfm 4.0.1 的删除线扩展默认
 * `singleTilde: true`（github.com 网页版行为，GFM 规范本身并不允许），于是**单个**
 * `~` 就是删除线定界符。中文技术答案里 `7~5nm`、`80~90%`、`0.8~1.1V`、`2~3 周`
 * 这类区间写法和 `~5W`、`~3GHz` 这类约等于写法密度极高，同一段里出现两个词内
 * `~` 就会被配成一对，中间整段正文被 `<del>` 划掉。真机实测（remark-gfm 4.0.1）：
 *
 *   「工艺节点为 7~5nm，良率约 80~90%，因此需要评估。」→ 划掉「~5nm，良率约 80~」
 *   「频率 ~3GHz 时，电压范围 0.8~1.1V。」          → 划掉「~3GHz 时，电压范围 0.8~」
 *
 * 关掉之后单个 `~` 恢复成字面量，而 GFM 规范的 `~~删除线~~` 逐字不变——模型真想
 * 要删除线时仍然拿得到，只是必须按规范写两个 `~`。
 *
 * 刻意不覆盖 knowhow 格子预览（`knowhow-cell-editor.tsx` 仍用裸 `remarkGfm`）：
 * 它与后端 `backend/app/services/knowhow/md_normalize.py` 是一对孪生实现，两侧的
 * 跨行强调配对都按「单 `~` 是删除线」在判定「规整会不会拆断渲染」。单方面改渲染器
 * 会让那套护栏与实际渲染口径分叉（方向是变保守、不会损坏数据，但注释与论证会失真），
 * 属于要连孪生逻辑一起改的独立一件事。见 `markdown-single-tilde-guard.test.mjs`。
 */
import remarkGfm, { type Options as RemarkGfmOptions } from "remark-gfm";

/** 直接展开进 `remarkPlugins` 的元组——调用方不得再传裸 `remarkGfm`。 */
export const remarkGfmPlugin: [typeof remarkGfm, RemarkGfmOptions] = [
  remarkGfm,
  { singleTilde: false },
];
