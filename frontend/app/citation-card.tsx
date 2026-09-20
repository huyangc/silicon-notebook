"use client";

/**
 * citation-card.tsx
 *
 * 引用小卡片——点答案里的行内引用标记后，在标记旁弹出的那张浮层。
 *
 * 原先是 `answer-panel.tsx` 的模块内函数，只有笔记本内问答用得到。全局问答的引用
 * 呈现按产品裁决与笔记本内**保持一致**（同一个组件、同一套 `.cite-popover` /
 * `.cite-detail-*` 样式、同一套定位与关闭逻辑），于是整体抽到这里由两侧 import：
 * 复制一份近似实现只会让两个面各自漂移一半。
 *
 * ⚠ 抽取是**纯移动**：`LatexText` / `InlineFormula` 一起搬（卡片正文用它渲染行内
 * 公式），`answer-panel.tsx` 原样再导出 `LatexText`，既有调用方
 * （page.tsx / kg-graph-view.tsx / duplicate-group-list.tsx）的 import 路径不变。
 *
 * ⚠ 这张卡片是**就地渲染**（不是 portal）。全局问答的全屏形态用原生
 * `<dialog>.showModal()`，dialog 在 top layer：渲染在 dialog 子树之外的 fixed
 * 元素会被它整个盖住且不可交互。所以调用方必须把它挂在自己的子树里，
 * `placeCitationPopover` 用的又是视口坐标，两条合起来才让同一张卡片在普通页面、
 * 小窗、全屏三种形态下都落在标记旁边。
 */

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { BookOpen, ExternalLink, Table2 } from "lucide-react";
import katex from "katex";

import { splitInlineLatex, type AnswerReference } from "./answer-formatting";
import { API_BASE } from "./api-config";
import { AuthedImage } from "./authed-image";
import { placeCitationPopover } from "./citation-popover";
import { type AnswerImagePreviewItem } from "./image-preview";
import { ImportRowButton, type ImportRowController } from "./import-row-state";
import { referenceImages } from "./inline-citation-images";
import { KgTypeMark, kgTypeLabel } from "./kg-type-mark";
import { mapCitationKnowhowRef } from "./knowhow-model.ts";
import { unwrapStandaloneLatex } from "./math-markdown";
import { assetNotebookId, sourceImageAssetUrl } from "./source-image";
import { label, TIER } from "./vocabulary";


/**
 * 此刻有几张引用卡正在接管 Esc。
 *
 * 卡片在 **window 捕获期**拦 Esc（`preventDefault` + `stopPropagation` + 收起自己），
 * 所以冒泡期的任何宿主监听（React 合成事件在内）都拿不到这次按键——一次 Esc 只收
 * 一层，卡片先走。剩下的一个缺口是原生 `<dialog>` 的 close request：捕获期
 * `preventDefault()` 能否掐掉它各浏览器不完全一致，宿主（全局问答浮窗）因此在
 * `oncancel` 上读这个计数兜底：有卡片接管就不关窗。
 *
 * 计数只在监听真的装上时才加（`dismissSuspended` 期间卡片让位给页面级图片预览，
 * 那一刻 Esc 本来就该归预览弹窗），所以「计数 > 0」与「这次 Esc 会被卡片吃掉」是
 * 同一件事，不是两份可能漂移的判断。
 */
let escapeHolders = 0;
export function citationPopoverHoldsEscape(): boolean {
  return escapeHolders > 0;
}


function InlineFormula({ latex }: { latex: string }) {
  let html = "";
  const normalized = unwrapStandaloneLatex(latex);
  try {
    html = katex.renderToString(normalized, {
      throwOnError: true,
      strict: "ignore",
      displayMode: false,
    });
  } catch {
    html = "";
  }
  if (!html) return <code className="answer-inline-code math-render-fallback">{latex}</code>;
  return <span className="answer-inline-formula" dangerouslySetInnerHTML={{ __html: html }} />;
}


/** Render a formula headline or inline LaTeX segments inside prose. */
export function LatexText({ text, isFormula = false }: { text: string; isFormula?: boolean }) {
  if (!text) return null;
  if (isFormula) return <InlineFormula latex={text} />;
  const segments = splitInlineLatex(text);
  if (segments.length === 1 && segments[0].type === "text") return <>{segments[0].value}</>;
  return (
    <>
      {segments.map((segment, index) =>
        segment.type === "math"
          ? <InlineFormula latex={segment.value} key={`m-${index}`} />
          : <span key={`t-${index}`}>{segment.value}</span>
      )}
    </>
  );
}


// 引用的 object_type 不是 KG 类型时的界面词。两类:来源清单的**文档**行,以及
// #402 给来源元素建的引用。它们都不该摆 KG 类型标记(那个形状是给知识对象看的),
// 也都**不能**走 kgTypeLabel —— 它对未知类型是原样返回,于是内部词 `source` /
// `element` 会照字面上屏(codex R7 P2)。
//
// `Object.hasOwn` 而非 `NON_KG_REFERENCE_LABELS[type]`:后者走原型链,自定义类型名
// 恰为 "constructor"/"__proto__" 时会命中继承属性——与 kg-type-mark.tsx 里记下的
// 同一个坑,同款防护。
const NON_KG_REFERENCE_LABELS: Record<string, string> = {
  source: "来源",
  element: "原文",
  // chunk = 原文段落(chunk 模式与全局问答的锚点类型)。同一个坑:不登记就会以
  // 灰圆「CH」+ 英文 `chunk` 上屏。
  chunk: "原文",
  // external = reflect 插件动作(`ask.reflect_action`,设计文档 §6.3)带回的**库外**
  // 材料。它同样不是知识对象:没有图谱节点、没有 source_id/element_id,能做的只有
  // 打开原链接或把它导入成本笔记本的一条来源(见 SelectedReferenceDetail)。
  external: "外部",
};


function referenceTitle(reference: AnswerReference): string {
  if (reference.anchor) return reference.anchor.name || reference.anchor.label || reference.anchor.key;
  return reference.citation?.label || reference.displayLabel;
}


function referenceSnippet(reference: AnswerReference): string {
  if (reference.anchor) return reference.anchor.definition || reference.anchor.snippet || "";
  return reference.citation?.quoted_span || "";
}


function referenceSource(reference: AnswerReference): string {
  if (reference.anchor) return reference.anchor.source_title || "";
  return reference.citation?.source_file_name || "";
}


function referenceSourceFileName(reference: AnswerReference): string {
  return reference.anchor?.source_file_name || reference.citation?.source_file_name || "";
}


function referenceLocation(reference: AnswerReference): string {
  if (reference.anchor) return reference.anchor.location_label || "";
  return reference.citation?.location_label || "";
}


function directlyReferencesImageElement(reference: AnswerReference): boolean {
  // chunk 锚点的 element_id 是该 chunk 的**起始**元素。起始元素是图时同样按
  // 「证据元素本身是图片」处理:纯图 chunk 的摘录整个是解析器的图注+描述,
  // 以图开头的混合 chunk 摘录也以同一段描述起头;前端拿不到 chunk 元素数,
  // 分不开这两种,统一隐藏是既有契约(纯图 chunk 此前就这样),不另开豁免。
  const elementId = reference.anchor?.element_id || reference.citation?.element_id || "";
  return Boolean(elementId) && referenceImages(reference)
    .some((image) => image.element_id === elementId);
}


export function SelectedReferenceDetail({
  reference,
  notebookId,
  notebookNames,
  notebookHref,
  onOpenKnowledgeGraph,
  onOpenKnowhowRow,
  onOpenNotebook,
  onOpenSource,
  onPreviewImage,
  importController,
}: {
  reference: AnswerReference;
  /** 检索结果带图(T1/T2)：**active** notebook,没有就传 null。取图归属交给
   *  `assetNotebookId` 单点裁定——有 active 恒用 active(同 ElementCollectionItemRow
   *  的既有口径:后端按资产自己声明的所属库在 active 的参与集内解析,跨库图片因此
   *  也能取到,而挂载的参考库用户未必是成员,只能经 active 代理);没有 active(全局
   *  问答)才用这条引用自己的 notebook_id——那是本轮范围里用户自己有读权、经
   *  `can_read_many` 准入过的库,服务端每次请求仍会复核。两者皆空才不渲染附图区。 */
  notebookId: string | null;
  /** 多领域基准库(Task 14)：id→name 映射，来自 notebooks 列表 + 当前笔记本挂载的
   * 参考库(base_notebooks)合并，供引用徽章把 notebook_id 解成人类可读的库名。 */
  notebookNames: Record<string, string>;
  /** 全局问答(跨笔记本)的「打开笔记本」出口：按这条引用**自己的** notebook_id /
   *  source_id 拼出宿主的路由（卡片不知道路由形状，所以由调用方给）。
   *  可选——不传时那颗按钮整个不渲染（同 onOpenSource 的既有惯例）：笔记本内问答
   *  本来就在这个笔记本里，它不传，DOM 与既有行为逐字不变。返回空串表示这条引用
   *  定位不到笔记本，同样不渲染，绝不留一个点了没反应的控件。 */
  notebookHref?: (notebookId: string, sourceId: string) => string;
  /** 可选：不传时「知识图谱」按钮整个不渲染（同 onOpenSource 的既有惯例）。 */
  onOpenKnowledgeGraph?: (objectId?: string, sourceNotebookId?: string) => void;
  /** Task 12（引用跳转）：命中 knowhow 格子的引用才出现「在表格中查看」按钮。
   *  可选：不传时该按钮不渲染。 */
  onOpenKnowhowRow?: (tableId: string, rowId: string) => void;
  /** 「打开笔记本」按下之后的收尾：全局问答用它收起浮窗（跳转是同页 hash 路由，
   *  不收起的话浮窗会继续盖在刚跳到的笔记本上）。只在 notebookHref 也传了、
   *  且那颗按钮真渲染出来时才有意义。 */
  onOpenNotebook?: () => void;
  onOpenSource?: (sourceId: string, elementId?: string) => void;
  /** 点开这一张附图。左右切换用的画册由 AnswerView 统一定位（见其 imageGallery）,
   *  所以这里只报「点的是哪一张」。没有承接方时图片仍显示但不可点击。 */
  onPreviewImage?: (image: AnswerImagePreviewItem) => void;
  /** 外部证据（`ask.reflect_action`）的「导入为来源」逐行状态机。缺省即那颗按钮
   *  不渲染（只读工作区）。状态住在 AnswerView 而不是这张卡片里——这张卡是会被
   *  反复开合的浮层，详见 import-row-state.tsx 顶部注释。 */
  importController?: ImportRowController;
}) {
  // 外部证据(`ask.reflect_action`,设计文档 §6.3)走 citation 回退列表时没有
  // anchor,也就没有 object_type——用 tier 把它补齐,「外部」这个类型标记两条路
  // 都出得来。后端保证 object_type==="external" ⇔ tier==="external"(§九 不变量
  // 3),所以这里补出来的值不可能与真实类型冲突。
  const objectType = reference.anchor?.object_type
    || (reference.citation?.tier === "external" ? "external" : "");
  const title = referenceTitle(reference);
  // When the evidence row itself is the image element, its snippet/quoted_span
  // is parser-generated caption + image description. The image already carries
  // that caption as alt text, so repeating the blob as visible prose defeats the
  // image-only contract. Text evidence that merely has a nearby image keeps its
  // real excerpt.
  const snippet = directlyReferencesImageElement(reference) ? "" : referenceSnippet(reference);
  const source = referenceSource(reference);
  const sourceFileName = referenceSourceFileName(reference);
  const location = referenceLocation(reference);
  // citation/anchor 二选一(buildAnswerReferences 全有全无),但既有的 anchor-only
  // 写法会让「无 [k] 标记、走 citation 回退列表」的答案永远显示不出 tier 徽章——
  // 补齐 citation 分支，和上面 referenceTitle/referenceSource 等 helper 的
  // "anchor 优先、citation 兜底"惯例保持一致。
  const tier = reference.anchor?.tier ?? reference.citation?.tier ?? "";
  const sourceNotebookId = reference.citation?.notebook_id || reference.anchor?.notebook_id || "";
  const sourceId = reference.citation?.source_id || reference.anchor?.source_id || "";
  const elementId = reference.citation?.element_id || reference.anchor?.element_id || "";
  const sourceName = sourceNotebookId ? notebookNames[sourceNotebookId] : undefined;
  const isRelationReference = objectType === "relation";
  // 「不是知识对象」的引用类型:元素(#402)与**文档**(来源清单)。两者都没有可定位的
  // 图谱节点,所以都不摆那个按钮——文档尤其:它的 object_id 本来就是空的,留着按钮
  // 只会是一个永远禁用、且解释起来还得绕一圈的控件。
  const isSourceElementReference = objectType === "element" || objectType === "source";
  // 外部证据同样不是知识对象,但它**有** object_id(核心铸的 `ext:{plugin_id}:{n}`,
  // 见设计文档 §6.1),所以不能靠 canLocateInGraph 自动禁用——那个 id 在图谱里根本
  // 不存在,按钮会渲染成可点、点了定位到空。必须显式排除。同理它的 source_id 恒为
  // 空(§九 不变量 3),「查看原文」本就出不来,但仍显式排除:守住的是不变量而不是
  // 当前 payload 的形状,后端哪天多填一个字段也不该让库外条目冒出一个来源入口。
  const isExternalReference = objectType === "external" || tier === "external";
  const canLocateInGraph = Boolean(reference.anchor?.object_id) && !isRelationReference;
  // 外部证据的原文链接(§6.3)。只认 http/https,且必须是**串首**——`javascript:`
  // 一类在宿主净化期就该丢掉整条(§九 不变量 8),这里是前端这一侧的同一把闸,不
  // 依赖后端净化过。不合格就整个不渲染链接(而不是渲染一个点不动的按钮):这条
  // 引用的其余内容照常可读。
  const externalUrl = reference.anchor?.url || reference.citation?.url || "";
  const externalHref = isExternalReference && /^https?:\/\//i.test(externalUrl) ? externalUrl : "";
  // 「打开笔记本」的目标。判据与「查看原文」同构:没有承接方(不传 notebookHref)、
  // 没有所属笔记本、或调用方算不出链接(空串)时整颗不渲染。库外材料显式排除——
  // 它压根不属于任何笔记本(§九 不变量 3),守的是不变量而不是当前 payload 的形状。
  const notebookLink = notebookHref && sourceNotebookId && !isExternalReference
    ? notebookHref(sourceNotebookId, sourceId)
    : "";
  // Task 12b（引用跳转扩面）：citation 优先，anchor 兜底——两者理论上不会同时
  // 出现在同一条 reference 上（buildAnswerReferences 二选一），但顺序仍按
  // "更具体的赢"的既有惯例书写，与 knowhow-citation.test.mjs 的显式断言一致。
  const knowhowRef = mapCitationKnowhowRef(reference.citation?.knowhow ?? reference.anchor?.knowhow);
  // 检索结果带图(T1/T2)。图片与「查看原文」按钮共用同一个已解析的 sourceId——
  // 后端 attach_citation_images 只从绑定证据自己所在 chunk/元素的候选里取图,
  // 因此一条引用下的全部附图恒与该引用同源,不存在附图跨到别的来源的情况。
  const images = referenceImages(reference);
  // 取图归属:有 active 恒用 active(逐字保持既有口径),没有 active(全局问答)才用这条
  // 引用自己的所属库。两者皆空就整块不渲染。完整论证见 source-image.ts。
  const imageNotebookId = assetNotebookId(notebookId, sourceNotebookId);
  return (
    <aside className="cite-detail-card" aria-live="polite">
      <div className="cite-detail-head">
        <strong>{reference.displayLabel}</strong>
        {objectType && (
          Object.hasOwn(NON_KG_REFERENCE_LABELS, objectType)
            ? <span>{NON_KG_REFERENCE_LABELS[objectType]}</span>
            : <span><KgTypeMark type={objectType} />{kgTypeLabel(objectType)}</span>
        )}
        {/* 外部证据不再叠一枚 tier 徽章:上面那枚类型标记已经写着「外部」,两枚
            并排会读成两个不同的事实(「外部」+「外部来源」),而它们说的是同一件
            事。tier 桶本身照常统计(来源分布徽章的第三格),这里省的只是重复展示。 */}
        {tier && !isExternalReference && (
          <span
            className={`tier-badge tier-${tier}`}
            title={
              // 泛化 tier 文案统一走 TIER 词表(与下面可见文字同一份真源),不再
              // 就地写 base/personal 二选一的三元式——那个写法对第三个取值
              // (external)会拼出「来自个人知识库」这种反向错误的话。
              sourceName
                ? `来自「${sourceName}」（${label(TIER, tier, "未知来源")}）`
                : `来自${label(TIER, tier, "未知来源")}`
            }
          >
            {sourceName ? (
              // 可达性修复(codex 评审 PR#304 第 3 轮 P2 #2):库名此前只进了上面的
              // title(hover 提示),触屏/键盘用户完全看不到是哪个库。这里把库名
              // 并入可见文字——长名走 .tier-badge-source-name 的省略号截断,不撑
              // 爆卡片;title 与可见文字内容一致,悬浮仍能看到被截断的完整库名。
              <>来自「<span className="tier-badge-source-name">{sourceName}</span>」（{label(TIER, tier, "未知来源")}）</>
            ) : (
              // 查不到库名(如跨二级挂载):优雅退回原有的泛化 tier 文案,不吐 id/空白。
              label(TIER, tier, "未知来源")
            )}
          </span>
        )}
        {onOpenKnowledgeGraph && !isSourceElementReference && !isExternalReference && (
          <button
            type="button"
            onClick={() => onOpenKnowledgeGraph(
              reference.anchor?.object_id,
              sourceNotebookId || undefined,
            )}
            disabled={!canLocateInGraph}
            title={
              // 用「知识对象」而非「概念」:引用锚定的是 object_id,其 object_type 可以是
              // Concept / Claim / Formula / Procedure 或 knowhow 表带来的自定义类型。
              // 说「概念」会把后四类说成第一类,用户按图索骥时对不上。
              isRelationReference
                ? "关系引用绑定的是一条关联，不是具体的知识对象，无法在知识图谱中定位"
                : reference.anchor?.object_id
                  ? "在知识图谱中定位"
                  : "该引用没有绑定到具体的知识对象"
            }
          >
            <ExternalLink size={14} />
            {isRelationReference ? "关系证据不可定位" : "知识图谱"}
          </button>
        )}
        {onOpenKnowhowRow && knowhowRef && (
          <button
            type="button"
            onClick={() => onOpenKnowhowRow(knowhowRef.tableId, knowhowRef.rowId)}
            title="在 Knowhow 表格中查看这一行"
          >
            <Table2 size={14} />
            在表格中查看
          </button>
        )}
        {onOpenSource && sourceId && !isExternalReference && (
          <button
            type="button"
            onClick={() => onOpenSource(sourceId, elementId || undefined)}
            title="在来源详情中查看这段原文"
          >
            <ExternalLink size={14} />
            查看原文
          </button>
        )}
        {/* 跨笔记本引用的出口:跳到这条引用所属的笔记本并定位到来源。必须是真 <a>
            （中键/右键「在新标签页打开」、复制链接地址都要能用），视觉与同排按钮
            同款,按下态见 globals.css 里与「打开链接」共用的那条 :active 规则。 */}
        {notebookLink && (
          <a
            className="cite-detail-notebook-link"
            href={notebookLink}
            // 只有「在本页打开」才收尾(全局问答借此收起浮窗)。Cmd/Ctrl/Shift+点是
            // 「去新标签页看」,当前页上正在读的答案不该跟着消失——与中键(auxclick,
            // 本来就不进 onClick)保持同一个结果。
            onClick={(event) => {
              if (event.defaultPrevented || event.button !== 0) return;
              if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
              onOpenNotebook?.();
            }}
            title="打开这条引用所属的笔记本，并定位到这份来源"
          >
            <BookOpen size={14} />
            打开笔记本
          </a>
        )}
        {/* 外部证据的两个出口(设计文档 §七)。库外材料在这个笔记本里没有原文可
            「查看」——能做的只有去它自己那里看,或者把它收进来变成真正的来源。 */}
        {externalHref && (
          <a
            className="cite-detail-external-link"
            href={externalHref}
            target="_blank"
            rel="noopener noreferrer"
            title="在新标签页打开这条库外材料的原文"
          >
            <ExternalLink size={14} />
            打开链接
          </a>
        )}
        {/* 导入走的是核心 URL 来源端点(page.tsx 的 importGapSuggestion,与站外来源
            建议同一条通道),不打任何插件路由;没有链接就没有可导入的东西,所以与
            「打开链接」同一个判据。只读工作区不传 controller ⇒ 按钮不渲染。 */}
        {externalHref && (
          <ImportRowButton
            controller={importController}
            rowKey={externalHref}
            url={externalHref}
            className="cite-detail-import"
            errorClassName="cite-detail-import-error"
            idleLabel="导入为来源"
          />
        )}
      </div>
      <h4><LatexText text={title} isFormula={objectType === "formula"} /></h4>
      {snippet && <p><LatexText text={snippet} /></p>}
      {(source || location) && <small>{[source, location].filter(Boolean).join(" · ")}</small>}
      {sourceFileName && sourceFileName !== source && (
        <small className="cite-source-file" title={sourceFileName}>
          原始文件：{sourceFileName}
        </small>
      )}
      {/* 检索结果带图(T1/T2)：与上方引证内容(snippet/来源/原始文件)用独立区块 +
          分隔线区分——本段附图不是模型引用过的证据,只是证据片段附近的图,绝不能
          让它看起来像 snippet 的一部分。算出来的取图归属库为空(既没有 active、这条
          引用也没有所属库)时没有可用的资产端点,整个区块不渲染,与"无附图"等价。 */}
      {images.length > 0 && imageNotebookId && (
        <div className="cite-detail-images">
          <span className="cite-detail-images-label">本段附图</span>
          <ul className="cite-detail-image-list">
            {images.map((image) => {
              const imageUrl = sourceImageAssetUrl(API_BASE, imageNotebookId, image.asset_id);
              const thumbnail = imageUrl
                ? <AuthedImage url={imageUrl} alt={image.caption || "附图"} />
                : <p className="tool-hint">图片不可用</p>;
              return (
                <li key={image.element_id} className="cite-detail-image-item">
                  {onPreviewImage ? (
                    <button
                      type="button"
                      className="cite-detail-image-button"
                      onClick={() => onPreviewImage({
                        assetId: image.asset_id,
                        alt: image.caption || `${reference.displayLabel} 的附图`,
                        referenceLabel: reference.displayLabel,
                      })}
                      title="放大查看这张附图"
                    >
                      {thumbnail}
                    </button>
                  ) : thumbnail}
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </aside>
  );
}


export function CitationPopover({
  reference,
  notebookId,
  notebookNames,
  notebookHref,
  anchorRect,
  onClose,
  onOpenKnowledgeGraph,
  onOpenKnowhowRow,
  onOpenNotebook,
  onOpenSource,
  onPreviewImage,
  importController,
  dismissSuspended = false,
}: {
  reference: AnswerReference;
  /** 检索结果带图(T1/T2)：透传给 SelectedReferenceDetail 拼装资产 URL,见其
   *  完整注释。 */
  notebookId: string | null;
  notebookNames: Record<string, string>;
  /** 「打开笔记本」出口，原样透传给 SelectedReferenceDetail，见其完整注释。 */
  notebookHref?: (notebookId: string, sourceId: string) => string;
  anchorRect: DOMRect;
  onClose: () => void;
  onOpenKnowledgeGraph?: (objectId?: string, sourceNotebookId?: string) => void;
  onOpenKnowhowRow?: (tableId: string, rowId: string) => void;
  onOpenNotebook?: () => void;
  onOpenSource?: (sourceId: string, elementId?: string) => void;
  /** 点开这一张附图。左右切换用的画册由 AnswerView 统一定位（见其 imageGallery）,
   *  所以这里只报「点的是哪一张」。没有承接方时图片仍显示但不可点击。 */
  onPreviewImage?: (image: AnswerImagePreviewItem) => void;
  /** 外部证据「导入为来源」的逐行状态机，原样透传给 SelectedReferenceDetail。
   *  ⚠ 它由 AnswerView 持有：这张浮层随点外部/滚动/Esc 卸载，状态住在里面的话
   *  「已导入」会在关掉浮层的一瞬间蒸发（详见 import-row-state.tsx 顶部）。 */
  importController?: ImportRowController;
  /** Keep the thumbnail trigger mounted while its page-level preview is open,
   * so the modal coordinator can return focus to a live element. */
  dismissSuspended?: boolean;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [pos, setPos] = useState<{ top: number; left: number }>(
    () => ({ top: anchorRect.bottom + 6, left: anchorRect.left })
  );
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const rect = element.getBoundingClientRect();
    setPos(placeCitationPopover(
      { top: anchorRect.top, bottom: anchorRect.bottom, left: anchorRect.left },
      { width: rect.width, height: rect.height },
      { width: window.innerWidth, height: window.innerHeight },
    ));
  }, [anchorRect]);
  useEffect(() => {
    if (dismissSuspended) return;
    const onDown = (event: PointerEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) onClose();
    };
    const onKey = (event: globalThis.KeyboardEvent) => {
      // 输入法合成期的 Esc 是「取消候选」,不是「关卡片」。
      if (event.key !== "Escape" || event.isComposing) return;
      // ⚠ 捕获期 + preventDefault + stopPropagation,三件缺一不可:
      //  · **捕获期**在整条派发路径最前面。冒泡期赶不上宿主挂在祖先上的监听——
      //    React 18 把合成事件装在根容器上,全局问答浮窗那个 <dialog> 正是卡片的
      //    祖先,它的 onKeyDown 会先跑、把整个浮窗收掉(卡片只是收了个寂寞)。
      //  · `stopPropagation()` 让这次按键到不了任何冒泡期监听,于是「一次 Esc 只收
      //    一层」是结构性的,不靠每个宿主自己记得判断。
      //  · `preventDefault()` 掐掉原生 <dialog> 的 close request(Esc 的默认动作),
      //    否则全屏形态下卡片关了、窗口也跟着关。浏览器对这一条的支持不完全一致,
      //    宿主还有 `citationPopoverHoldsEscape()` 那道兜底。
      event.preventDefault();
      event.stopPropagation();
      onClose();
    };
    const onScroll = (event: Event) => {
      // 浮层内部滚动(查看长内容)不应关闭;只有外部页面/祖先滚动导致脱锚时才关。
      if (ref.current && ref.current.contains(event.target as Node)) return;
      onClose();
    };
    window.addEventListener("pointerdown", onDown, true);
    window.addEventListener("keydown", onKey, true);
    window.addEventListener("scroll", onScroll, true);
    escapeHolders += 1;
    return () => {
      escapeHolders -= 1;
      window.removeEventListener("pointerdown", onDown, true);
      window.removeEventListener("keydown", onKey, true);
      window.removeEventListener("scroll", onScroll, true);
    };
  }, [dismissSuspended, onClose]);
  return (
    <div
      ref={ref}
      className="cite-popover"
      role="dialog"
      style={{ position: "fixed", top: pos.top, left: pos.left }}
    >
      <SelectedReferenceDetail
        reference={reference}
        notebookId={notebookId}
        notebookNames={notebookNames}
        notebookHref={notebookHref}
        onOpenKnowledgeGraph={onOpenKnowledgeGraph}
        onOpenKnowhowRow={onOpenKnowhowRow}
        onOpenNotebook={onOpenNotebook}
        onOpenSource={onOpenSource}
        onPreviewImage={onPreviewImage}
        importController={importController}
      />
    </div>
  );
}
