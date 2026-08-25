"use client";

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  BookmarkPlus,
  Check,
  ChevronDown,
  ChevronRight,
  Copy,
  ExternalLink,
  ListChecks,
  Share2,
  Sparkles,
  Table2,
  ThumbsDown,
  ThumbsUp,
} from "lucide-react";
import katex from "katex";

import {
  buildAnswerReferences,
  computeSourceTierCounts,
  referenceByCitationKey,
  renderTextWithReferenceNumbers,
  splitInlineLatex,
  type AnswerReference,
  type CitationImageLike,
} from "./answer-formatting";
import { AnswerMarkdown } from "./answer-markdown";
import type { CitationImageOrder, CitationImageSlotItem } from "./rehype-citation-images";
import { GapSuggestionsPanel } from "./answer-gap-suggestions";
import { AuthedImage } from "./authed-image";
import { API_BASE } from "./api-config";
import { type ReasoningTraceStep } from "./ask-stream";
import { placeCitationPopover } from "./citation-popover";
import { copyTextSafely } from "./copy-text";
import { FormulaView } from "./formula-view";
import {
  buildImageGallery,
  imagePreviewRequest,
  type AnswerImagePreviewItem,
  type AnswerImagePreviewRequest,
} from "./image-preview";
import { mapCitationKnowhowRef } from "./knowhow-model.ts";
import { unwrapStandaloneLatex } from "./math-markdown";
import { KgTypeMark, kgTypeLabel } from "./kg-type-mark";
import {
  modelFailureText,
  sanitizeModelServiceId,
  sanitizeModelSupportId,
  type ModelServiceStatusItem,
} from "./model-services.ts";
import {
  formatDuration,
  getReasoningTraceSummary,
  getTraceStepDetail,
  TRACE_STEP_LABELS,
} from "./reasoning-trace";
import {
  STRUCTURED_ENUMERATION_LIMITS,
} from "./ask-retrieval-effort";
import { shouldShowIndexRequiredBanner, type ScaleIndexStatus } from "./scale-index";
import { sourceImageAssetUrl } from "./source-image";
import { retrievalScopeSummary } from "./source-scope";
import type {
  AskResponse,
  Citation,
  KnowhowBatchCoverage,
  KnowhowResultSet,
  TypedCollectionCoverage,
  TypedCollectionItem,
  TypedCollectionResult,
} from "./workspace-model";
import { SupportIdCopy } from "./support-id-copy";
import { label, MODEL_SERVICE_STATUS_ERROR, TIER } from "./vocabulary";


// truncated_reason 的中文映射,Knowhow 卡与类型化清单卡共用(两条覆盖率分别来自
// backend/app/services/structured_retrieval.py 与 collection_enumeration.py,
// 取值集合不完全相同,但都是内部代号,不能原样吐给用户)。未知值兜底显示泛化的
// "部分结果",不吐英文 token(同 label() 签名强制传 fallback 的既有惯例)。
// concurrent_change 在类型化清单卡里有专属的终态整句(见 collectionCoverageStatus),
// 走不到这张表;但 Knowhow 覆盖率没有那条特判分支,遇到它时仍会落到这张表,所以
// 这里也给它一个人话翻译,不能省。
const TRUNCATED_REASON_LABELS: Record<string, string> = {
  row_limit: "已达本轮可读取的行数上限",
  table_limit: "已达本轮可读取的表数上限",
  payload_limit: "本轮内容量已达上限",
  budget: "已达本轮枚举上限",
  payload: "本轮内容量已达上限",
  concurrent_change: "资料在读取期间有变动",
};

function truncatedReasonLabel(reason: string): string {
  return label(TRUNCATED_REASON_LABELS, reason, "部分结果");
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


function KnowhowResultSetCard({
  resultSet,
  onOpenKnowhowRow,
}: {
  resultSet: KnowhowResultSet;
  /** 可选（同 onOpenSource/onTestModel 的既有惯例）：不传时整列跳转按钮不渲染。
   *  只读的排障视图（dev/logs 的「活动」）没有承接方，绝不能留一颗点了没反应的
   *  按钮——那正是「界面上不会出现死控件」这条断言曾经不成立的地方。 */
  onOpenKnowhowRow?: (tableId: string, rowId: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const visibleLimit = STRUCTURED_ENUMERATION_LIMITS.initialVisibleRows;
  const rows = expanded ? resultSet.rows : resultSet.rows.slice(0, visibleLimit);
  const coverage = resultSet.coverage;
  const coverageCount = Math.min(coverage.returned_rows, coverage.total_rows);
  const status = coverage.complete ? "完整" : "部分";
  const hasMoreLoadedRows = resultSet.rows.length > visibleLimit;

  return (
    <section className="answer-knowhow-result" aria-label={`表格结果：${resultSet.title}`}>
      <div className="answer-knowhow-result-heading">
        <span className={`tag ${coverage.complete ? "answer-grounded" : "answer-overview"}`}>
          {status} {coverageCount}/{coverage.total_rows}
        </span>
        <strong><Table2 size={14} aria-hidden="true" /> {resultSet.title}</strong>
        {!coverage.complete && coverage.truncated_reason && (
          <span className="answer-knowhow-partial-reason" title={coverage.overflow_semantics || "explicit_partial"}>
            已明确标注为部分结果（{truncatedReasonLabel(coverage.truncated_reason)}）
            {coverage.scanned_rows !== coverage.returned_rows
              ? `；已扫描 ${coverage.scanned_rows} 行`
              : ""}
          </span>
        )}
      </div>
      <div className="answer-table-wrap">
        <table className="answer-table answer-knowhow-table">
          <thead>
            <tr>
              <th scope="col">行</th>
              {resultSet.columns.map((column) => <th key={column.id} scope="col">{column.name}</th>)}
              {onOpenKnowhowRow && <th scope="col" aria-label="操作" />}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.row_id}>
                <th scope="row">{row.position + 1}</th>
                {resultSet.columns.map((column) => <td key={column.id}>{row.cells[column.id] || "—"}</td>)}
                {onOpenKnowhowRow && (
                  <td>
                    <button
                      className="answer-knowhow-open"
                      type="button"
                      onClick={() => onOpenKnowhowRow(resultSet.table_id, row.row_id)}
                    >
                      在表格中查看
                    </button>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {hasMoreLoadedRows && (
        <button
          type="button"
          className="answer-knowhow-expand"
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? "收起已加载行" : `展开全部已加载的 ${resultSet.rows.length} 行`}
        </button>
      )}
      {!coverage.complete && (
        <p className="answer-knowhow-coverage-note">
          已扫描 {coverage.scanned_rows} 行、返回 {coverage.returned_rows} 行；未扫描部分不会被表述为“全部”。
        </p>
      )}
    </section>
  );
}


// 元素清单的界面名。逐字镜像后端 reasoning_retrieval.py 的 _ELEMENT_KIND_LABELS +
// "清单" 后缀(trace summary 用同一份措辞),两侧改名必须同步。
const ELEMENT_KIND_LIST_LABELS: Record<string, string> = {
  formula: "公式清单",
  table: "表格清单",
  image: "图片清单",
  code_block: "代码块清单",
};

// 知识对象清单的界面名。逐字镜像后端 _KG_OBJECT_LABELS + "清单" 后缀。刻意带
// "知识对象"限定——formula 在元素侧也存在("公式清单" vs "公式知识对象清单"),
// 两套标签全域不能重名(否则模型账目回喂与本卡片会各叫一个名)。
const KG_OBJECT_LIST_LABELS: Record<string, string> = {
  concept: "概念知识对象清单",
  claim: "论断知识对象清单",
  formula: "公式知识对象清单",
  procedure: "过程知识对象清单",
};

// 来源清单的界面名。逐字镜像后端 _SOURCE_COLLECTION_LABELS + "清单" 后缀;这一张
// 按 collection 取键(库的文档清单没有子类型),而不是按 kind——与后端同形。
const SOURCE_LIST_LABELS: Record<string, string> = {
  sources: "来源清单",
};

function collectionResultTitle(resultSet: TypedCollectionResult): string {
  if (resultSet.collection === "elements") {
    return label(ELEMENT_KIND_LIST_LABELS, resultSet.element_kind ?? "", "条目清单");
  }
  // sources 必须先判:它的 element_kind/object_type 都是空串,落到下面的知识对象
  // 分支只会拿到兜底词「知识对象清单」——一份文档清单被叫成知识对象清单。
  if (resultSet.collection === "sources") {
    return label(SOURCE_LIST_LABELS, resultSet.collection, "来源清单");
  }
  return label(KG_OBJECT_LIST_LABELS, resultSet.object_type ?? "", "知识对象清单");
}

/**
 * 覆盖率状态行——四种硬性渲染规则的唯一落点(design doc §2.6):
 * 1. `truncated_reason === "concurrent_change"` 是终态,必须单独一句话,不与下面
 *    的通用「部分结果」合并(它既不能续跑也不能被当作完整)。
 * 2. `total === null` 渲染"总数未知",绝不写成 /0。
 * 3. complete 时不需要分母,只报已列出的条数。
 * 4. 其余情况分子分母都已知,直接给出比例。
 */
function collectionCoverageStatus(
  coverage: TypedCollectionCoverage,
): { text: string; cls: string } {
  if (coverage.complete) {
    return { text: `已全部列出 ${coverage.returned_total} 条`, cls: "answer-grounded" };
  }
  if (coverage.truncated_reason === "concurrent_change") {
    return {
      text: `已列 ${coverage.returned_total} 条，但资料在枚举期间有变动，无法确认完整`,
      cls: "answer-overview",
    };
  }
  if (coverage.total === null || coverage.total === undefined) {
    return { text: `已列 ${coverage.returned_total} 条（总数未知）`, cls: "answer-overview" };
  }
  return { text: `已列 ${coverage.returned_total}/${coverage.total} 条`, cls: "answer-overview" };
}


// 挂载公共参考库不等于获得该库的直接成员权限(红线):裸 `GET /sources/{id}` 是
// owner∪member 口径,前端绝不替用户猜权限去直连另一个库的资源。跨库条目的来源详情
// 与图片改走**按 active notebook 代理读取**的端点(`/notebooks/{active}/sources/{id}`
// 与 `/notebooks/{active}/assets/{assetId}`),与「问答引用定位图谱节点」红线同构:
// 浏览器始终只用当前 active notebook 过权限,后端在它的有效 participant 集内解析目标
// 并内部代理读取,挂载边一失效就当场 404。因此这里不再按跨库与否决定"能不能点/能不能
// 看图"——两种条目走同一条路径;跨库标注保留,它是有用的出处信息,不是能力降级。
//
// item.notebook_id 两种条目类型都会填(design doc §2.6/collection_enumeration.py
// 的 ElementItem/KgObjectItem——不是"只在跨库命中才非空"的 Citation.notebook_id
// 惯例),非空且不等于当前活跃 notebook 即为跨库条目。
function isCrossLibraryItem(itemNotebookId: string, activeNotebookId: string | null): boolean {
  return Boolean(itemNotebookId) && itemNotebookId !== (activeNotebookId ?? "");
}


// 跨库条目的所属库标注。复用既有 tier-badge 样式(SelectedReferenceDetail 同款
// 视觉语言),名字查得到就显示「来自参考库《名》」,查不到就退回泛化的 tier 文案
// (公共知识库/个人知识库)——绝不吐裸 notebook id。
function CrossLibraryBadge({
  itemNotebookId,
  notebookNames,
  tier,
}: {
  itemNotebookId: string;
  notebookNames: Record<string, string>;
  tier: string;
}) {
  const name = notebookNames[itemNotebookId];
  return (
    <span className={`tier-badge tier-${tier}`}>
      {name ? `来自参考库《${name}》` : label(TIER, tier, "来自参考库")}
    </span>
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
};


function CollectionItemCitation({ citation }: { citation: Citation }) {
  return (
    <details className="answer-collection-citation">
      <summary>原文引用：{citation.label || citation.location_label || "来源片段"}</summary>
      {citation.quoted_span && <p><LatexText text={citation.quoted_span} /></p>}
    </details>
  );
}


function ElementCollectionItemRow({
  item,
  notebookId,
  notebookNames,
  onOpenSource,
}: {
  item: TypedCollectionItem;
  notebookId: string | null;
  notebookNames: Record<string, string>;
  onOpenSource?: (sourceId: string, elementId?: string) => void;
}) {
  const kind = item.element_type ?? "";
  const itemNotebookId = item.notebook_id ?? "";
  const crossLibrary = isCrossLibraryItem(itemNotebookId, notebookId);
  // 局部 const 而非 item.source_id!:非空断言只是在闭包里骗过编译器,局部 const
  // 让 TS 在这个块内真的把它窄化成 string,闭包捕获的是这个已收窄的绑定。
  const sourceId = item.source_id ?? "";
  // 资产 URL 恒用**当前 active notebook**:后端按资产自己声明的所属库在参与集内解析,
  // 跨库图片因此也能取到。绝不拿 item.notebook_id 去直连另一个库(那是成员口径的越权猜测)。
  const imageUrl = kind === "image" && item.asset_id
    ? sourceImageAssetUrl(API_BASE, notebookId || "", item.asset_id)
    : "";
  return (
    <li className="answer-collection-item">
      <div className="answer-collection-item-body">
        {kind === "formula" && <FormulaView latex={item.text ?? ""} />}
        {kind === "image" && (
          imageUrl
            ? <AuthedImage url={imageUrl} alt={item.location_label || "图片"} />
            : <p className="tool-hint">{item.text || "图片不可用"}</p>
        )}
        {kind === "code_block" && (
          <pre className="answer-collection-code"><code>{item.text ?? ""}</code></pre>
        )}
        {kind !== "formula" && kind !== "image" && kind !== "code_block" && (
          <p className="answer-collection-text">{item.text ?? ""}</p>
        )}
      </div>
      <div className="answer-collection-item-meta">
        {item.location_label && <small>{item.location_label}</small>}
        {crossLibrary && (
          <CrossLibraryBadge
            itemNotebookId={itemNotebookId}
            notebookNames={notebookNames}
            tier={item.tier ?? "personal"}
          />
        )}
        {onOpenSource && sourceId && (
          <button
            type="button"
            className="answer-collection-open"
            onClick={() => onOpenSource(sourceId, item.item_id)}
          >
            {kind === "table" ? "在来源详情查看完整表格" : "查看来源"}
          </button>
        )}
      </div>
      {item.citation && <CollectionItemCitation citation={item.citation} />}
    </li>
  );
}


function KgObjectCollectionItemRow({
  item,
  objectType,
  notebookId,
  notebookNames,
  onOpenSource,
}: {
  item: TypedCollectionItem;
  objectType: string;
  notebookId: string | null;
  notebookNames: Record<string, string>;
  onOpenSource?: (sourceId: string, elementId?: string) => void;
}) {
  const itemNotebookId = item.notebook_id ?? "";
  const crossLibrary = isCrossLibraryItem(itemNotebookId, notebookId);
  return (
    <li className="answer-collection-item answer-collection-kg-item">
      <KgTypeMark type={objectType} />
      <strong><LatexText text={item.name ?? ""} isFormula={objectType === "formula"} /></strong>
      {item.section_path && <small>{item.section_path}</small>}
      {crossLibrary && (
        <CrossLibraryBadge
          itemNotebookId={itemNotebookId}
          notebookNames={notebookNames}
          tier={item.tier ?? "personal"}
        />
      )}
      {item.citation && (
        <div className="answer-collection-kg-citation">
          <CollectionItemCitation citation={item.citation} />
          {onOpenSource && (
            <button
              type="button"
              className="answer-collection-open"
              onClick={() => onOpenSource(
                item.citation!.source_id,
                item.citation!.element_id,
              )}
            >
              查看原文
            </button>
          )}
        </div>
      )}
      {!item.citation && (
        <span className="answer-collection-citation-missing">暂无可用原文出处</span>
      )}
    </li>
  );
}


function SourceCollectionItemRow({
  item,
  notebookId,
  notebookNames,
  onOpenSource,
}: {
  item: TypedCollectionItem;
  notebookId: string | null;
  notebookNames: Record<string, string>;
  onOpenSource?: (sourceId: string, elementId?: string) => void;
}) {
  const itemNotebookId = item.notebook_id ?? "";
  const crossLibrary = isCrossLibraryItem(itemNotebookId, notebookId);
  // 局部 const:同 ElementCollectionItemRow,让 TS 在块内真的收窄成 string。
  const sourceId = item.source_id ?? "";
  // 文档类型的界面词由后端给(PROFILES 是唯一真源);没有就整段不渲染,绝不显示
  // `academic_paper` 这类内部 id,也不写「未知类型」去填一个本来就不存在的事实。
  const docType = item.location_label ?? "";
  const summary = item.text ?? "";
  return (
    <li className="answer-collection-item answer-collection-source-item">
      <div className="answer-collection-source-head">
        <strong className="answer-collection-source-title">
          {item.source_title || "未命名来源"}
        </strong>
        {docType && <small className="answer-collection-source-type">{docType}</small>}
      </div>
      {summary
        ? <p className="answer-collection-source-summary">{summary}</p>
        : <p className="answer-collection-source-summary tool-hint">暂无摘要</p>}
      <div className="answer-collection-item-meta">
        {crossLibrary && (
          <CrossLibraryBadge
            itemNotebookId={itemNotebookId}
            notebookNames={notebookNames}
            tier={item.tier ?? "personal"}
          />
        )}
        {/* 跨库条目**也**给跳转,与 ElementCollectionItemRow 同口径:#398 之后
            参考库来源详情经 active notebook 维度的代理端点读取(后端在有效参与集
            内解析,不在集内 404),弹窗对参考库来源按只读渲染。所以这里不再有
            `!crossLibrary` 围栏——那会把平台已经支持的读取挡掉;库名标注由上面的
            CrossLibraryBadge 承担。 */}
        {onOpenSource && sourceId && (
          <button
            type="button"
            className="answer-collection-open"
            onClick={() => onOpenSource(sourceId)}
          >
            查看来源
          </button>
        )}
      </div>
    </li>
  );
}


// 元素清单按来源分组显示(design doc §2.6:"按来源分组"),保留原始条目顺序
// (执行器已按 source 顺位游标产出,这里只是分桶,不重排)。
function groupElementItemsBySource(
  items: TypedCollectionItem[],
): { sourceId: string; sourceTitle: string; items: TypedCollectionItem[] }[] {
  const order: string[] = [];
  const bySource = new Map<string, { sourceTitle: string; items: TypedCollectionItem[] }>();
  for (const item of items) {
    const key = item.source_id || "";
    if (!bySource.has(key)) {
      order.push(key);
      bySource.set(key, { sourceTitle: item.source_title || "未知来源", items: [] });
    }
    bySource.get(key)!.items.push(item);
  }
  return order.map((sourceId) => ({ sourceId, ...bySource.get(sourceId)! }));
}


function CollectionResultCard({
  resultSet,
  notebookId,
  notebookNames,
  onOpenSource,
}: {
  resultSet: TypedCollectionResult;
  notebookId: string | null;
  /** id→name 映射(AnswerView 已持有,一层透传),供跨库条目标注「来自参考库《名》」。 */
  notebookNames: Record<string, string>;
  onOpenSource?: (sourceId: string, elementId?: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const visibleLimit = STRUCTURED_ENUMERATION_LIMITS.initialVisibleRows;
  const items = expanded ? resultSet.items : resultSet.items.slice(0, visibleLimit);
  const hasMoreLoadedItems = resultSet.items.length > visibleLimit;
  const coverage = resultSet.coverage;
  const status = collectionCoverageStatus(coverage);
  const title = collectionResultTitle(resultSet);
  // "仅限来源"标注(design doc §2.6):取首个条目的 source_title,查不到就不显示——
  // 宁可不标注也不吐裸 source_id。
  const scopedSourceTitle = resultSet.source_id ? resultSet.items[0]?.source_title : "";
  const showPreviewNote = resultSet.synthesis_complete === false
    && resultSet.synthesis_rows < coverage.returned_total;

  return (
    <section className="answer-collection-result" aria-label={`清单结果：${title}`}>
      <div className="answer-collection-result-heading">
        <span className={`tag ${status.cls}`}>{status.text}</span>
        <strong><ListChecks size={14} aria-hidden="true" /> {title}</strong>
        {scopedSourceTitle && (
          <span className="answer-collection-scope">仅限来源：{scopedSourceTitle}</span>
        )}
        <button
          type="button"
          className="answer-collection-toggle"
          aria-expanded={open}
          onClick={() => setOpen((value) => !value)}
        >
          {open ? <ChevronDown size={15} aria-hidden="true" /> : <ChevronRight size={15} aria-hidden="true" />}
          {open ? `收起${title}` : `展开${title}`}
        </button>
      </div>
      {open && (
        <div className="answer-collection-content">
          {!coverage.complete && coverage.truncated_reason && coverage.truncated_reason !== "concurrent_change" && (
            <span
              className="answer-collection-partial-reason"
              title={coverage.overflow_semantics || "explicit_partial"}
            >
              已明确标注为部分结果（{truncatedReasonLabel(coverage.truncated_reason)}）
            </span>
          )}
          {showPreviewNote && (
            <p className="answer-collection-coverage-note">
              {resultSet.synthesis_rows === 0
                ? "本轮分析未包含该清单"
                : `本轮分析基于前 ${resultSet.synthesis_rows} 条预览`}
            </p>
          )}
          {resultSet.collection === "sources" ? (
            // 每一行本身就是一份文档,不必按来源分组(那正是这份清单的分组维度)。
            <ul className="answer-collection-items">
              {items.map((item) => (
                <SourceCollectionItemRow
                  key={item.item_id}
                  item={item}
                  notebookId={notebookId}
                  notebookNames={notebookNames}
                  onOpenSource={onOpenSource}
                />
              ))}
            </ul>
          ) : resultSet.collection === "elements" ? (
            <div className="answer-collection-groups">
              {groupElementItemsBySource(items).map((group) => (
                <div className="answer-collection-group" key={group.sourceId || "unknown"}>
                  <h4 className="answer-collection-group-title">{group.sourceTitle}</h4>
                  <ul className="answer-collection-items">
                    {group.items.map((item) => (
                      <ElementCollectionItemRow
                        key={item.item_id}
                        item={item}
                        notebookId={notebookId}
                        notebookNames={notebookNames}
                        onOpenSource={onOpenSource}
                      />
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          ) : (
            <ul className="answer-collection-items">
              {items.map((item) => (
                <KgObjectCollectionItemRow
                  key={item.item_id}
                  item={item}
                  objectType={resultSet.object_type ?? ""}
                  notebookId={notebookId}
                  notebookNames={notebookNames}
                  onOpenSource={onOpenSource}
                />
              ))}
            </ul>
          )}
          {hasMoreLoadedItems && (
            <button
              type="button"
              className="answer-collection-expand"
              onClick={() => setExpanded((value) => !value)}
            >
              {expanded ? "收起已加载内容" : `展开全部已加载的 ${resultSet.items.length} 条`}
            </button>
          )}
        </div>
      )}
    </section>
  );
}


function KnowhowResultSets({
  resultSets,
  batchCoverage,
  onOpenKnowhowRow,
  notebookId,
  notebookNames,
  onOpenSource,
}: {
  resultSets: (KnowhowResultSet | TypedCollectionResult)[] | undefined;
  batchCoverage: KnowhowBatchCoverage | undefined;
  /** 可选：不传时清单卡不渲染跳转按钮（见 KnowhowResultSetCard 上的说明）。 */
  onOpenKnowhowRow?: (tableId: string, rowId: string) => void;
  notebookId: string | null;
  notebookNames: Record<string, string>;
  onOpenSource?: (sourceId: string, elementId?: string) => void;
}) {
  if (!resultSets?.length) return null;
  return (
    <div className="answer-knowhow-results">
      {batchCoverage && (
        <section className="answer-knowhow-batch-coverage" aria-label="Knowhow 总体覆盖">
          <span className={`tag ${batchCoverage.complete ? "answer-grounded" : "answer-overview"}`}>
            总体{batchCoverage.complete ? "完整" : "部分"}
          </span>
          <span>
            表 {batchCoverage.selected_tables}/{batchCoverage.known_tables}；行 {batchCoverage.returned_rows}/{batchCoverage.known_total_rows}
          </span>
          {batchCoverage.synthesis_complete != null && (
            <span>
              分析覆盖 {batchCoverage.synthesis_rows}/{batchCoverage.known_total_rows}
              （{batchCoverage.synthesis_complete ? "完整" : "部分"}）
            </span>
          )}
          {!batchCoverage.complete && batchCoverage.truncated_reason && (
            <span className="answer-knowhow-partial-reason">
              截断原因：{truncatedReasonLabel(batchCoverage.truncated_reason)}
            </span>
          )}
        </section>
      )}
      {/* 按 kind 分派,不得按下标猜测:result_sets 是 knowhow/collection 的判别
          union。未知 kind 一律跳过(返回 null),绝不落到 knowhow 分支——那条分支
          假定 `.rows`/`.columns` 存在,一份没有这两个字段的 collection 行会在
          `.rows.slice(...)` 上抛 TypeError,炸穿整个答案面板(且答案持久化后,
          历史重开会继续炸)。这正是本函数存在的理由。 */}
      {resultSets.map((resultSet) => {
        if (resultSet.kind === "knowhow") {
          return (
            <KnowhowResultSetCard
              key={`${resultSet.table_id}-${resultSet.title}`}
              resultSet={resultSet}
              onOpenKnowhowRow={onOpenKnowhowRow}
            />
          );
        }
        if (resultSet.kind === "collection") {
          return (
            <CollectionResultCard
              key={`collection-${resultSet.collection}-${resultSet.element_kind || resultSet.object_type}-${resultSet.source_id || ""}`}
              resultSet={resultSet}
              notebookId={notebookId}
              notebookNames={notebookNames}
              onOpenSource={onOpenSource}
            />
          );
        }
        return null;
      })}
    </div>
  );
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


// 检索结果带图(T1/T2)：anchor 优先、citation 兜底,与本文件其余 reference* helper
// 的既有惯例一致(anchor/citation 二选一,由 buildAnswerReferences 全有全无保证)。
// 枚举清单行的引用(evidence_context.py collection_item_citations)不调
// attach_citation_images,故 images 恒缺席;但枚举行**锚点**走的是别的装配点,可能带
// 图——这里不做特判,读取路径对两者一视同仁,由数据形状自然决定是否渲染。
//
// F5(评审登记,checkpoint b6541f26):弹层高频开合(点引用→关闭→再点回同一条)会让
// 附图重复下载——AuthedImage 卸载即 revoke objectURL,下次挂载是全新的 fetch,这里
// 没有跨挂载的结果缓存。这是复用既有 AuthedImage 组件带来的已登记代价,不在本处
// 加一层 blob 缓存去解决它:objectURL 的生命周期一旦要跨组件实例存活,谁负责在
// 「最后一个引用者卸载」时 revoke 会显著复杂化,而附图请求本身走鉴权 fetch、体量
// 有限,权衡后维持现状。
function referenceImages(reference: AnswerReference): CitationImageLike[] {
  return reference.anchor?.images ?? reference.citation?.images ?? [];
}

function directlyReferencesImageElement(reference: AnswerReference): boolean {
  const elementId = reference.anchor?.element_id || reference.citation?.element_id || "";
  return Boolean(elementId) && referenceImages(reference)
    .some((image) => image.element_id === elementId);
}


type ResolvedCitationImage = Readonly<{
  reference: AnswerReference;
  image: CitationImageLike;
}>;


function InlineCitationImages({
  rows,
  notebookId,
  onPreviewImage,
}: {
  rows: readonly ResolvedCitationImage[];
  notebookId: string;
  /** 点开这一张附图。左右切换用的画册由 AnswerView 统一定位（见其 imageGallery）,
   *  所以这里只报「点的是哪一张」。没有承接方时图片仍显示但不可点击。 */
  onPreviewImage?: (image: AnswerImagePreviewItem) => void;
}) {
  if (rows.length === 0) return null;
  const labels = [...new Set(rows.map((row) => row.reference.displayLabel))];
  return (
    <aside className="answer-inline-images" aria-label={`引用图片 ${labels.join("、")}`}>
      <div className="answer-inline-images-heading">
        <span>引用 {labels.join("、")}</span>
        <small title="模型可能读取过图注或图片描述，但没有直接读取图片">模型未直接读取图片</small>
      </div>
      <ul className="answer-inline-image-list">
        {rows.map(({ reference, image }) => {
          const url = sourceImageAssetUrl(API_BASE, notebookId, image.asset_id);
          const alt = image.caption || `${reference.displayLabel} 的附图`;
          return (
            <li key={image.asset_id} className="answer-inline-image-item">
              {url
                ? <AuthedImage url={url} alt={alt} />
                : <p className="tool-hint">图片不可用</p>}
              {url && onPreviewImage && (
                <button
                  type="button"
                  className="answer-inline-image-open"
                  aria-label={`放大查看${reference.displayLabel}的附图`}
                  onClick={() => onPreviewImage({
                    assetId: image.asset_id,
                    alt,
                    referenceLabel: reference.displayLabel,
                  })}
                />
              )}
            </li>
          );
        })}
      </ul>
    </aside>
  );
}


function SelectedReferenceDetail({
  reference,
  notebookId,
  notebookNames,
  onOpenKnowledgeGraph,
  onOpenKnowhowRow,
  onOpenSource,
  onPreviewImage,
}: {
  reference: AnswerReference;
  /** 检索结果带图(T1/T2)：本段附图的资产 URL 恒用**当前 active notebook**——同
   *  ElementCollectionItemRow 的既有口径,后端按资产自己声明的所属库在参与集内
   *  解析,跨库图片因此也能取到。可为 null(极少数尚未选中笔记本的调用点);此时
   *  附图区不渲染(与「无图」等价——没有可用的代理端点)。 */
  notebookId: string | null;
  /** 多领域基准库(Task 14)：id→name 映射，来自 notebooks 列表 + 当前笔记本挂载的
   * 参考库(base_notebooks)合并，供引用徽章把 notebook_id 解成人类可读的库名。 */
  notebookNames: Record<string, string>;
  /** 可选：不传时「知识图谱」按钮整个不渲染（同 onOpenSource 的既有惯例）。 */
  onOpenKnowledgeGraph?: (objectId?: string, sourceNotebookId?: string) => void;
  /** Task 12（引用跳转）：命中 knowhow 格子的引用才出现「在表格中查看」按钮。
   *  可选：不传时该按钮不渲染。 */
  onOpenKnowhowRow?: (tableId: string, rowId: string) => void;
  onOpenSource?: (sourceId: string, elementId?: string) => void;
  /** 点开这一张附图。左右切换用的画册由 AnswerView 统一定位（见其 imageGallery）,
   *  所以这里只报「点的是哪一张」。没有承接方时图片仍显示但不可点击。 */
  onPreviewImage?: (image: AnswerImagePreviewItem) => void;
}) {
  const objectType = reference.anchor?.object_type || "";
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
  const canLocateInGraph = Boolean(reference.anchor?.object_id) && !isRelationReference;
  // Task 12b（引用跳转扩面）：citation 优先，anchor 兜底——两者理论上不会同时
  // 出现在同一条 reference 上（buildAnswerReferences 二选一），但顺序仍按
  // "更具体的赢"的既有惯例书写，与 knowhow-citation.test.mjs 的显式断言一致。
  const knowhowRef = mapCitationKnowhowRef(reference.citation?.knowhow ?? reference.anchor?.knowhow);
  // 检索结果带图(T1/T2)。图片与「查看原文」按钮共用同一个已解析的 sourceId——
  // 后端 attach_citation_images 只从绑定证据自己所在 chunk/元素的候选里取图,
  // 因此一条引用下的全部附图恒与该引用同源,不存在附图跨到别的来源的情况。
  const images = referenceImages(reference);
  return (
    <aside className="cite-detail-card" aria-live="polite">
      <div className="cite-detail-head">
        <strong>{reference.displayLabel}</strong>
        {objectType && (
          Object.hasOwn(NON_KG_REFERENCE_LABELS, objectType)
            ? <span>{NON_KG_REFERENCE_LABELS[objectType]}</span>
            : <span><KgTypeMark type={objectType} />{kgTypeLabel(objectType)}</span>
        )}
        {tier && (
          <span
            className={`tier-badge tier-${tier}`}
            title={
              sourceName
                ? `来自「${sourceName}」（${tier === "base" ? "公共知识库" : "个人知识库"}）`
                : (tier === "base" ? "来自公共知识库" : "来自个人知识库")
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
        {onOpenKnowledgeGraph && !isSourceElementReference && (
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
        {onOpenSource && sourceId && (
          <button
            type="button"
            onClick={() => onOpenSource(sourceId, elementId || undefined)}
            title="在来源详情中查看这段原文"
          >
            <ExternalLink size={14} />
            查看原文
          </button>
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
          让它看起来像 snippet 的一部分。notebookId 为空(极少数尚未选中笔记本的
          调用点)时没有可用的资产代理端点,整个区块不渲染,与"无附图"等价。 */}
      {images.length > 0 && notebookId && (
        <div className="cite-detail-images">
          <span className="cite-detail-images-label">本段附图</span>
          <ul className="cite-detail-image-list">
            {images.map((image) => {
              const imageUrl = sourceImageAssetUrl(API_BASE, notebookId, image.asset_id);
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


function CitationPopover({
  reference,
  notebookId,
  notebookNames,
  anchorRect,
  onClose,
  onOpenKnowledgeGraph,
  onOpenKnowhowRow,
  onOpenSource,
  onPreviewImage,
  dismissSuspended = false,
}: {
  reference: AnswerReference;
  /** 检索结果带图(T1/T2)：透传给 SelectedReferenceDetail 拼装资产 URL,见其
   *  完整注释。 */
  notebookId: string | null;
  notebookNames: Record<string, string>;
  anchorRect: DOMRect;
  onClose: () => void;
  onOpenKnowledgeGraph?: (objectId?: string, sourceNotebookId?: string) => void;
  onOpenKnowhowRow?: (tableId: string, rowId: string) => void;
  onOpenSource?: (sourceId: string, elementId?: string) => void;
  /** 点开这一张附图。左右切换用的画册由 AnswerView 统一定位（见其 imageGallery）,
   *  所以这里只报「点的是哪一张」。没有承接方时图片仍显示但不可点击。 */
  onPreviewImage?: (image: AnswerImagePreviewItem) => void;
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
      if (event.key === "Escape") onClose();
    };
    const onScroll = (event: Event) => {
      // 浮层内部滚动(查看长内容)不应关闭;只有外部页面/祖先滚动导致脱锚时才关。
      if (ref.current && ref.current.contains(event.target as Node)) return;
      onClose();
    };
    window.addEventListener("pointerdown", onDown, true);
    window.addEventListener("keydown", onKey);
    window.addEventListener("scroll", onScroll, true);
    return () => {
      window.removeEventListener("pointerdown", onDown, true);
      window.removeEventListener("keydown", onKey);
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
        onOpenKnowledgeGraph={onOpenKnowledgeGraph}
        onOpenKnowhowRow={onOpenKnowhowRow}
        onOpenSource={onOpenSource}
        onPreviewImage={onPreviewImage}
      />
    </div>
  );
}


export function ReasoningTracePanel({
  steps,
  live = false,
}: {
  steps: ReasoningTraceStep[];
  live?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const visibleSteps = steps.filter((step) => step.step_type !== "source_subgraph");
  const summary = getReasoningTraceSummary(visibleSteps, live);
  return (
    <div className={`reasoning-trace-panel ${live ? "live" : ""} ${expanded ? "expanded" : "collapsed"}`}>
      <button
        aria-expanded={expanded}
        className="reasoning-trace-summary"
        onClick={() => setExpanded((value) => !value)}
        type="button"
      >
        <Sparkles size={15} />
        <span className="reasoning-trace-title">{summary.title}</span>
        <span className={`reasoning-trace-chip ${summary.latestLabel ? "" : "empty"}`}>
          {summary.latestLabel || "空"}
        </span>
        <strong>{summary.latestSummary}</strong>
        <small>{summary.latestDetail}</small>
        <span className="reasoning-trace-count">
          {summary.stepCountLabel}{summary.totalLabel ? ` · ${summary.totalLabel}` : ""}
        </span>
        {expanded ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
      </button>
      {expanded && (
        <ol className="reasoning-trace-list">
          {visibleSteps.length === 0 ? (
            <li className="reasoning-trace-empty">等待后端事件…</li>
          ) : visibleSteps.map((step, index) => {
            const detail = getTraceStepDetail(step);
            const hasTime = typeof step.duration_ms === "number";
            return (
              <li key={`${step.step_type}-${index}`} className={index === visibleSteps.length - 1 && live ? "active" : ""}>
                <span>{label(TRACE_STEP_LABELS, step.step_type, "处理中")}</span>
                <strong>{step.summary}</strong>
                {(detail || hasTime) && (
                  <div className="reasoning-trace-meta">
                    {detail && <small>{detail}</small>}
                    {hasTime && (
                      <time className={`reasoning-trace-time ${(step.duration_ms ?? 0) >= 10000 ? "slow" : ""}`}>
                        {formatDuration(step.duration_ms ?? 0)}
                      </time>
                    )}
                  </div>
                )}
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}


type AnswerModelError = NonNullable<AskResponse["model_errors"]>[number];


function modelTestResultText(result: ModelServiceStatusItem): string {
  if (result.status === "ok") return `正常 ${result.latency_ms}ms`;
  if (result.status === "busy") return `繁忙 ${result.active} / ${result.maximum}`;
  return `失败：${label(MODEL_SERVICE_STATUS_ERROR, result.code, "连接未通过")}`;
}


function ModelErrorPanel({
  errors,
  onTestModel,
  onOpenModelStatus,
  testingModelServices = {},
  testingAllModels = false,
}: {
  errors: AnswerModelError[];
  onTestModel?: (serviceId: string) => Promise<ModelServiceStatusItem | null>;
  onOpenModelStatus?: (serviceId: string) => void;
  testingModelServices?: Record<string, boolean>;
  testingAllModels?: boolean;
}) {
  const uniqueErrors = useMemo(() => {
    const seen = new Set<string>();
    return errors.map((error) => ({
      error,
      serviceId: sanitizeModelServiceId(error.service_id),
      supportId: sanitizeModelSupportId(error.support_id),
    })).filter(({ error, serviceId, supportId }) => {
      const key = `${serviceId}\0${error.model}\0${supportId}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }, [errors]);
  const testingServices = useRef(new Set<string>());
  const [testing, setTesting] = useState<Record<string, boolean>>({});
  const [results, setResults] = useState<Record<string, string>>({});

  async function testService(serviceId: string) {
    if (!onTestModel || !serviceId || testingServices.current.has(serviceId)) return;
    testingServices.current.add(serviceId);
    setTesting((current) => ({ ...current, [serviceId]: true }));
    setResults((current) => ({ ...current, [serviceId]: "" }));
    try {
      const result = await onTestModel(serviceId);
      if (!result) return;
      setResults((current) => ({ ...current, [serviceId]: modelTestResultText(result) }));
    } catch {
      setResults((current) => ({ ...current, [serviceId]: "失败：连接未通过" }));
    } finally {
      testingServices.current.delete(serviceId);
      setTesting((current) => ({ ...current, [serviceId]: false }));
    }
  }

  return (
    <section className="answer-model-error" aria-label="模型服务异常">
      <div className="answer-model-error-heading">⚠️ 本次回答可能不完整</div>
      <ul className="answer-model-error-list">
        {uniqueErrors.map(({ error, serviceId, supportId }) => {
          const isTesting = testingAllModels
            || Boolean(testingModelServices[serviceId])
            || Boolean(testing[serviceId]);
          return (
            <li key={`${serviceId}\0${error.model}\0${supportId}`}>
              <span>{modelFailureText(error)}</span>
              <div className="answer-model-error-actions">
                {onTestModel && serviceId && (
                  <button
                    type="button"
                    disabled={isTesting}
                    onClick={() => testService(serviceId)}
                  >
                    {isTesting ? "测试中…" : "测试此模型"}
                  </button>
                )}
                {onOpenModelStatus && (
                  <button type="button" onClick={() => onOpenModelStatus(serviceId)}>
                    查看模型状态
                  </button>
                )}
                <SupportIdCopy supportId={supportId} className="answer-model-support-id" />
                {results[serviceId] && (
                  <span className="answer-model-test-result" role="status">
                    {results[serviceId]}
                  </span>
                )}
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}


export function AnswerView({
  answer,
  feedbackSent,
  onFeedback,
  onOpenKnowledgeGraph,
  onOpenKnowhowRow,
  onOpenSource,
  onPreviewImage,
  imagePreviewOpen = false,
  notebookId,
  notebookNames,
  onBuildScaleIndex,
  buildingScaleIndex,
  scaleIndexStatus,
  onSaveMemory,
  onShare,
  onImportGapSuggestion,
  importGapSuggestionDisabledReason,
  memorySaved,
  onTestModel,
  onOpenModelStatus,
  testingModelServices,
  testingAllModels,
}: {
  answer: AskResponse;
  feedbackSent: string;
  /** 交互回调一律可选，判据统一：**没有承接方就不渲染那颗按钮**（沿用
   *  onOpenSource / onTestModel / onOpenModelStatus 的既有惯例）。绝不用 CSS
   *  隐藏、也绝不传空实现——只读的排障视图（dev/logs 的「活动」）曾因为传 noop
   *  而在界面上留下三颗点了没反应的启用态按钮。
   *  回归门：frontend/app/answer-panel-readonly.component.test.tsx。 */
  onFeedback?: (rating: "useful" | "not_useful") => void;
  onOpenKnowledgeGraph?: (objectId?: string, sourceNotebookId?: string) => void;
  /** Task 12（引用跳转）：命中 knowhow 格子的引用点「在表格中查看」时调用，
   * page.tsx 据此打开 Knowhow 面板并定位到该表该行的抽屉。 */
  onOpenKnowhowRow?: (tableId: string, rowId: string) => void;
  /** PR-2 T6：类型化元素清单条目「查看来源」/「在来源详情查看完整表格」跳转时
   * 调用，page.tsx 据此打开来源详情并高亮到该元素。可选——旧调用方/测试不传时
   * 清单卡的跳转按钮不渲染（同 onTestModel/onOpenModelStatus 的可选惯例）。 */
  onOpenSource?: (sourceId: string, elementId?: string) => void;
  /** 正文/引用浮层图片的页面内放大入口。没有承接方时图片仍显示但不可点击。 */
  onPreviewImage?: (request: AnswerImagePreviewRequest) => void;
  /** True while the page-level image modal is holding the root lease. */
  imagePreviewOpen?: boolean;
  notebookId: string | null;
  /** 多领域基准库(Task 14)：id→name 映射，来自 notebooks 列表 + 当前笔记本挂载的
   * 参考库(base_notebooks)合并，逐 turn 复用同一份，供引用徽章标来源库名。 */
  notebookNames: Record<string, string>;
  onBuildScaleIndex?: (notebookId: string) => void;
  buildingScaleIndex: boolean;
  scaleIndexStatus?: Pick<ScaleIndexStatus, "exists" | "building" | "state"> | null;
  onSaveMemory?: (answerId: string) => void;
  /** 「分享到这条回答」：把这条回答**及它之前的全部问答**发布成一条免登录只读链接
   *  （page.tsx 据此打开会话分享弹窗，并把分享水位钉在这条答案上）。可选——只有承接
   *  得了「当前会话是哪一条」的调用方才传，dev/logs 的只读排障视图不传就不渲染
   *  （同 onSaveMemory / onFeedback 的既有惯例：写回服务端的动作没有回调就不出按钮）。 */
  onShare?: (answerId: string) => void;
  /** 站外来源建议的「导入」按钮（``ask.gap_consult``）：把这个 URL 当一次普通
   *  链接来源添加进当前笔记本。可选——没有承接方（只读排障视图）时导入按钮
   *  一颗都不渲染，同 onSaveMemory 的既有惯例。返回值供该条目内联展示成功/
   *  失败，绝不用 toast（长任务按钮红线：失败文案必须持久可见）。 */
  onImportGapSuggestion?: (url: string) => Promise<{ ok: boolean; message?: string }>;
  /** 非空时每一条站外来源建议的导入按钮都渲染为禁用态，`title` 提示这句话
   *  ——「可写但已达文档数量上限」（红线：确认上传前必须把批次计入上限，
   *  超额时按钮直接置灰并写明原因）。只读工作区不传 onImportGapSuggestion
   *  时这个 prop 传不传都不影响渲染（按钮压根不出现）。 */
  importGapSuggestionDisabledReason?: string;
  memorySaved: boolean;
  onTestModel?: (serviceId: string) => Promise<ModelServiceStatusItem | null>;
  onOpenModelStatus?: (serviceId: string) => void;
  testingModelServices?: Record<string, boolean>;
  testingAllModels?: boolean;
}) {
  const [copied, setCopied] = useState(false);
  const [citePopover, setCitePopover] = useState<{
    reference: AnswerReference;
    rect: DOMRect;
  } | null>(null);
  const answerText = answer.answer || answer.conclusion || "";
  const scaleIndexQueued = scaleIndexStatus?.state === "queued"
    && !scaleIndexStatus.building;
  const references = useMemo(
    () => buildAnswerReferences(answerText, answer.anchors, answer.citations),
    [answerText, answer.anchors, answer.citations]
  );
  const referencesByCitationKey = useMemo(
    () => referenceByCitationKey(references),
    [references],
  );
  // 本条回答里可以左右切换的全部附图。顺序不在这里推导——渲染管线一边把图片区块
  // 插进正文一边记账(citationImageOrder),这里读的就是那本账,所以左右切换走的必然
  // 是眼睛看到的顺序。正文图片区块与引用浮层缩略图共用同一份画册,预览打开后走遍
  // 整条回答,而不是「点进去的那一组」。
  const citationImageOrder = useRef<CitationImageOrder>({ items: [] }).current;
  // 读发生在点击那一刻(此时本次渲染早已跑完并记好账),不是渲染期读。
  const previewImage = onPreviewImage
    ? (image: AnswerImagePreviewItem) => onPreviewImage(imagePreviewRequest(
      buildImageGallery(citationImageOrder.items, (key) => {
        const reference = referencesByCitationKey[key];
        return reference
          ? { displayLabel: reference.displayLabel, images: referenceImages(reference) }
          : undefined;
      }),
      image,
    ))
    : undefined;
  const renderCitationImages = (items: CitationImageSlotItem[]) => {
    if (!notebookId) return null;
    const rows = items.flatMap(({ citationKey, imageId }) => {
      const reference = referencesByCitationKey[citationKey];
      if (!reference) return [];
      const image = referenceImages(reference).find((candidate) => candidate.asset_id === imageId);
      return image ? [{ reference, image }] : [];
    });
    return (
      <InlineCitationImages
        rows={rows}
        notebookId={notebookId}
        onPreviewImage={previewImage}
      />
    );
  };
  useEffect(() => setCitePopover(null), [answer.answer_id]);
  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 1400);
    return () => window.clearTimeout(timer);
  }, [copied]);

  async function copyAnswer() {
    if (await copyTextSafely(renderTextWithReferenceNumbers(answerText, references))) {
      setCopied(true);
    }
  }

  return (
    <div className="chat-answer">
      {answer.model_errors && answer.model_errors.length > 0 && (
        <ModelErrorPanel
          errors={answer.model_errors}
          onTestModel={onTestModel}
          onOpenModelStatus={onOpenModelStatus}
          testingModelServices={testingModelServices}
          testingAllModels={testingAllModels}
        />
      )}
      {shouldShowIndexRequiredBanner(answer.index_required, scaleIndexStatus) && (
        <div className="answer-index-degraded" title="内容较多时检索会走索引；尚未建立索引时结果会受限">
          <AlertTriangle size={14} aria-hidden="true" />
          <span>此笔记本内容较多，尚未建立索引，当前检索能力受限。</span>
          {/* 说明文字对只读视图仍然有价值（那正是要看的诊断），但没有承接方时
              绝不留下这颗按钮。 */}
          {onBuildScaleIndex && (
            <button
              type="button"
              className="mode-engine"
              style={{ marginLeft: 6 }}
              disabled={buildingScaleIndex && !scaleIndexQueued}
              onClick={() => { if (notebookId) onBuildScaleIndex(notebookId); }}
            >
              {scaleIndexQueued ? "立即构建" : buildingScaleIndex ? "构建中…" : "构建索引"}
            </button>
          )}
        </div>
      )}
      {(() => {
        const level = answer.evidence_level ?? (answer.grounded ? "grounded" : "inferred");
        const meta = level === "grounded"
          ? { cls: "answer-grounded", label: "有据" }
          : level === "overview"
            ? { cls: "answer-overview", label: "概述（仅薄证据，余为推断）" }
            : { cls: "answer-ungrounded", label: "推断（未命中笔记本依据）" };
        return <span className={`tag ${meta.cls}`}>{meta.label}</span>;
      })()}
      {(() => {
        const { personal, base } = computeSourceTierCounts(references);
        if (personal + base === 0) return null;
        return (
          <span className="tag source-dist" title="本次引用的来源分布（个人知识库 / 公共知识库）">
            来源 · 个人 {personal}
            {base > 0 && <> · <strong className="source-dist-base">公共 {base}</strong></>}
          </span>
        );
      })()}
      {/* 本轮实际获准的检索范围回执。真机事故的那一屏——勾定单篇文章提问，16 条引用
          全部来自 84 篇论文的参考库——要在答案上一眼可见，而不是等用户去数引用。
          ⚠ 判据只有「回执在不在」这一条：后端只在**确有收窄**时才下发（浏览器每次都会
          提交显式范围，全选也不例外，所以「提交了范围」不是信号）。刻意不在这里按
          回执数字重算一遍「算不算收窄」——那会变成第二份判据，而它注定与后端那份漂移：
          后端的 narrowed 用冻结那一刻的实时全集算，回执里的 total 却刻意用可能滞后的
          缓存计数（对展示是可接受的陈旧，对闸不是）。并发上传一篇，就会让一次真收窄
          在浏览器侧被算成 5/5 而整行消失。
          ⚠ 库名直接用回执里的快照，不拿当前挂载列表重新映射：回答活得比挂载边久，
          重开历史会话时那个库可能已经被卸载，重新映射恰好会丢掉最该解释它的那一行。 */}
      {answer.retrieval_scope && (() => {
        const scope = answer.retrieval_scope;
        const includedBases = scope.bases.filter((base) => base.included);
        return (
          <details className="answer-retrieval-scope">
            <summary title="勾选的来源与参考库才会参与本轮检索">
              <ChevronRight size={14} aria-hidden="true" />
              检索范围：{retrievalScopeSummary(
                scope.local,
                scope.bases.length > 0
                  ? { selected: includedBases.length, total: scope.bases.length }
                  : null,
              )}
            </summary>
            <ul>
              <li>
                <span>本笔记本来源 {scope.local.selected}/{scope.local.total}</span>
              </li>
              {scope.bases.map((base) => (
                <li key={base.notebook_id}>
                  <span className={base.included ? "" : "scope-excluded"}>
                    参考库《{base.name || "未命名"}》
                  </span>
                  <small>{base.included ? "已参与检索" : "本次未参与检索"}</small>
                </li>
              ))}
            </ul>
          </details>
        );
      })()}
      <AnswerMarkdown
        answer={answerText}
        anchors={answer.anchors}
        citations={answer.citations}
        selectedReferenceId={citePopover?.reference.id ?? null}
        onReferenceClick={(reference, event) => setCitePopover({
          reference,
          rect: event.currentTarget.getBoundingClientRect(),
        })}
        renderCitationImages={renderCitationImages}
        citationImageOrder={citationImageOrder}
      />
      <KnowhowResultSets
        resultSets={answer.result_sets}
        batchCoverage={answer.result_coverage}
        onOpenKnowhowRow={onOpenKnowhowRow}
        notebookId={notebookId}
        notebookNames={notebookNames}
        onOpenSource={onOpenSource}
      />
      <GapSuggestionsPanel
        suggestions={answer.gap_suggestions ?? []}
        onImport={onImportGapSuggestion}
        importDisabledReason={importGapSuggestionDisabledReason}
      />
      {answer.reasoning_trace && answer.reasoning_trace.length > 0 && (
        <ReasoningTracePanel steps={answer.reasoning_trace} />
      )}
      {citePopover && (
        <CitationPopover
          reference={citePopover.reference}
          notebookId={notebookId}
          notebookNames={notebookNames}
          anchorRect={citePopover.rect}
          onClose={() => setCitePopover(null)}
          // 「知识图谱」「在表格中查看」都会在本卡片之上打开一个新的全屏视图
          // （.kg-view / .knowhow-view，z-index 均为 50，低于本卡片的 60）——
          // 点击跳转后若不关闭这张卡片，它会一直浮在新打开的视图上方挡住内容
          // （真机 QA 反馈）。复用与 onClose 完全相同的收起路径
          // （setCitePopover(null)），不为此新开一套状态。
          onOpenKnowledgeGraph={onOpenKnowledgeGraph ? (objectId, sourceNotebookId) => {
            setCitePopover(null);
            onOpenKnowledgeGraph(objectId, sourceNotebookId);
          } : undefined}
          onOpenKnowhowRow={onOpenKnowhowRow ? (tableId, rowId) => {
            setCitePopover(null);
            onOpenKnowhowRow(tableId, rowId);
          } : undefined}
          onOpenSource={onOpenSource ? (sourceId, elementId) => {
            setCitePopover(null);
            onOpenSource(sourceId, elementId);
          } : undefined}
          onPreviewImage={previewImage}
          dismissSuspended={imagePreviewOpen}
        />
      )}
      {/* 复制回答是自足动作（只读 DOM + 剪贴板），任何调用方都能承接，所以它不带
          可选开关；保存记忆与反馈需要写回服务端，没有回调就不渲染。 */}
      <div className="answer-feedback">
        {onSaveMemory && (
          <button
            aria-label={memorySaved ? "已保存到记忆" : "保存到记忆"}
            className={`answer-memory-save ${memorySaved ? "is-saved" : ""}`}
            disabled={memorySaved}
            onClick={() => onSaveMemory(answer.answer_id)}
            title={memorySaved ? "已保存到记忆" : "保存到记忆"}
            type="button"
          >{memorySaved ? <Check size={15} /> : <BookmarkPlus size={15} />}<span>{memorySaved ? "已保存到记忆" : "保存到记忆"}</span></button>
        )}
        <div className="answer-feedback-actions">
          {onFeedback && (
            <button
              aria-label="有用"
              className={`answer-action ${feedbackSent === "useful" ? "selected" : ""}`}
              disabled={Boolean(feedbackSent)}
              onClick={() => onFeedback("useful")}
              title="有用"
              type="button"
            ><ThumbsUp size={16} /></button>
          )}
          {onFeedback && (
            <button
              aria-label="需改进"
              className={`answer-action ${feedbackSent === "not_useful" ? "selected" : ""}`}
              disabled={Boolean(feedbackSent)}
              onClick={() => onFeedback("not_useful")}
              title="需改进"
              type="button"
            ><ThumbsDown size={16} /></button>
          )}
          <button
            aria-label={copied ? "已复制" : "复制回答"}
            className={`answer-action ${copied ? "selected" : ""}`}
            onClick={() => copyAnswer().catch(() => undefined)}
            title={copied ? "已复制" : "复制回答"}
            type="button"
          >{copied ? <Check size={16} /> : <Copy size={16} />}</button>
          {/* 分享到这条回答为止。`answer_id` 是分享水位的锚（服务端把
              `expected_through_id` 钉在这条答案上），生成中的回答还没有答案行、
              `answer_id` 为空，此时按钮不渲染——渲染了点下去也只会拿到一句
              「这条会话还没有已完成的回答」。 */}
          {onShare && answer.answer_id && (
            <button
              aria-label="分享到这条回答"
              className="answer-action"
              onClick={() => onShare(answer.answer_id)}
              title="分享到这条回答（含此前的全部问答）"
              type="button"
            ><Share2 size={16} /></button>
          )}
        </div>
      </div>
    </div>
  );
}
