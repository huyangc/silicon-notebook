"use client";

import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  BookmarkPlus,
  Check,
  ChevronDown,
  ChevronRight,
  Copy,
  ListChecks,
  Share2,
  Sparkles,
  Table2,
  ThumbsDown,
  ThumbsUp,
} from "lucide-react";

import {
  buildAnswerReferences,
  computeSourceTierCounts,
  referenceByCitationKey,
  renderTextWithReferenceNumbers,
  type AnswerReference,
} from "./answer-formatting";
import { AnswerMarkdown } from "./answer-markdown";
import { answerBodyWithoutCompletenessNotice } from "./answer-completeness";
import type { CitationImageOrder, CitationImageSlotItem } from "./rehype-citation-images";
import { ExternalEvidenceSection, GapSuggestionsPanel } from "./answer-gap-suggestions";
import { AuthedImage } from "./authed-image";
import { API_BASE } from "./api-config";
import { type ReasoningTraceStep } from "./ask-stream";
// 引用小卡片(`.cite-popover` + `.cite-detail-card`)住在 ./citation-card：全局问答
// 与笔记本内问答共用同一份实现。`LatexText` 跟着它一起搬(卡片正文要渲染行内公式),
// 这里原样再导出，既有调用方的 `from "./answer-panel"` 一个字都不用改。
import { CitationPopover, LatexText } from "./citation-card";
import { copyTextSafely } from "./copy-text";
import { FormulaView } from "./formula-view";
import { useImportRowController } from "./import-row-state";
import {
  InlineCitationImages,
  referenceImages,
  resolveCitationImageRows,
} from "./inline-citation-images";
import {
  buildImageGallery,
  imagePreviewRequest,
  type AnswerImagePreviewItem,
  type AnswerImagePreviewRequest,
} from "./image-preview";
import { KgTypeMark } from "./kg-type-mark";
import {
  modelFailureText,
  sanitizeModelServiceId,
  sanitizeModelSupportId,
  type ModelServiceStatusItem,
} from "./model-services.ts";
import {
  formatDuration,
  getPluginActionArguments,
  getReasoningTraceSummary,
  getTraceStepDetail,
  getTraceStepLabel,
} from "./reasoning-trace";
import {
  STRUCTURED_ENUMERATION_LIMITS,
} from "./ask-retrieval-effort";
import { shouldShowIndexRequiredBanner, type ScaleIndexStatus } from "./scale-index";
import { assetNotebookId, sourceImageAssetUrl } from "./source-image";
import { retrievalScopeSummary } from "./source-scope";
import type {
  AskResponse,
  Citation,
  KnowhowBatchCoverage,
  KnowhowResultSet,
  SpreadsheetAnalysisResult,
  TypedCollectionCoverage,
  TypedCollectionItem,
  TypedCollectionResult,
} from "./workspace-model";
import { SupportIdCopy } from "./support-id-copy";
import { label, MODEL_SERVICE_STATUS_ERROR, TIER } from "./vocabulary";

export { LatexText };


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


const SPREADSHEET_OPERATION_LABELS: Record<string, string> = {
  profile: "数据概况",
  aggregate: "分组汇总",
  top: "排序分析",
  filter: "条件筛选",
};

function SpreadsheetResultCard({
  resultSet,
  onOpenSource,
}: {
  resultSet: SpreadsheetAnalysisResult;
  onOpenSource?: (sourceId: string, elementId?: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const visibleLimit = STRUCTURED_ENUMERATION_LIMITS.initialVisibleRows;
  const rows = expanded ? resultSet.rows : resultSet.rows.slice(0, visibleLimit);
  const coverage = resultSet.coverage;
  const firstCitation = resultSet.rows.find((row) => row.citation)?.citation;
  return (
    <section className="answer-knowhow-result" aria-label={`Excel 分析：${resultSet.source_title || resultSet.sheet}`}>
      <div className="answer-knowhow-result-heading">
        <span className={`tag ${coverage.complete ? "answer-grounded" : "answer-overview"}`}>
          {coverage.complete ? "结果完整" : "部分预览"} {coverage.returned_rows}/{coverage.total_rows}
        </span>
        <strong><Table2 size={14} aria-hidden="true" /> Excel 分析 · {resultSet.source_title || "未命名来源"}</strong>
        <span>{resultSet.sheet}!{resultSet.range} · {label(SPREADSHEET_OPERATION_LABELS, resultSet.operation, "专业分析")}</span>
        {onOpenSource && resultSet.source_id && (
          <button
            className="answer-knowhow-open"
            type="button"
            onClick={() => onOpenSource(resultSet.source_id, firstCitation?.element_id)}
          >
            查看来源
          </button>
        )}
      </div>
      <p className="answer-knowhow-coverage-note">
        已扫描 {coverage.scanned_rows} 行；公式单元格 {resultSet.formula_cells} 个
        {resultSet.unresolved_formula_cells > 0
          ? `，其中 ${resultSet.unresolved_formula_cells} 个没有可用缓存值`
          : ""}。
      </p>
      <div className="answer-table-wrap">
        <table className="answer-table answer-knowhow-table">
          <thead>
            <tr>
              <th scope="col">行</th>
              {resultSet.columns.map((column) => <th key={column.id} scope="col">{column.name}</th>)}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.position}>
                <th scope="row">{row.position}</th>
                {resultSet.columns.map((column) => <td key={column.id}>{row.cells[column.id] || "—"}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {resultSet.rows.length > visibleLimit && (
        <button
          type="button"
          className="answer-knowhow-expand"
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? "收起已加载行" : `展开全部已加载的 ${resultSet.rows.length} 行`}
        </button>
      )}
      {resultSet.warnings?.map((warning) => (
        <p className="answer-knowhow-coverage-note" key={warning}>{warning}</p>
      ))}
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

// 收窄过的来源清单的标题后缀。逐字镜像后端
// `collection_enumeration.LOCAL_ONLY_SCOPE_SUFFIX`——同一件事已经在上屏轨迹摘要、
// 回喂模型的枚举账目、合成证据块的分区标题上用这个词说过三遍,结果卡是第四处。
// 四处必须同词:同一轮里用户可能同时看到轨迹里的「(仅当前笔记本)」和卡片标题,
// 换个说法就成了两件事。
//
// 它**不进** SOURCE_LIST_LABELS 那张表:那张表是 scripts/check_enumeration_list_labels_contract.py
// 逐字比对的「后端标签 + 清单」映射(键是 collection),后缀不是标签、也不按
// collection 取键,塞进去会让那条守卫比一张它读不懂的表。
const LOCAL_ONLY_SCOPE_SUFFIX = "（仅当前笔记本）";

function collectionResultTitle(resultSet: TypedCollectionResult): string {
  if (resultSet.collection === "elements") {
    return label(ELEMENT_KIND_LIST_LABELS, resultSet.element_kind ?? "", "条目清单");
  }
  // sources 必须先判:它的 element_kind/object_type 都是空串,落到下面的知识对象
  // 分支只会拿到兜底词「知识对象清单」——一份文档清单被叫成知识对象清单。
  if (resultSet.collection === "sources") {
    // 范围进标题而不是另起一行:一次 run 可以同时列出两个范围,两张卡的条数本就
    // 不同,标题若逐字相同,读者只能看到「两份都叫来源清单、数字却对不上」。
    // 判据是等于 "current_notebook":字段可缺席(旧后端/历史回答)且是开放字符串,
    // 缺席与未知值都按 "all" 读,绝不给没收窄的清单贴标签。
    const scopeSuffix = resultSet.scope === "current_notebook" ? LOCAL_ONLY_SCOPE_SUFFIX : "";
    return `${label(SOURCE_LIST_LABELS, resultSet.collection, "来源清单")}${scopeSuffix}`;
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
  // 取图归属由 assetNotebookId 单点裁定:**有 active 就恒用 active**(后端按资产自己
  // 声明的所属库在 active 的参与集内解析,跨库图片因此也能取到;绝不拿 item.notebook_id
  // 去直连另一个库——挂载的参考库用户未必是成员,那是越权猜测),没有 active(全局问答)
  // 才用条目自己的库(本轮范围里用户自己有读权的库)。完整论证见 source-image.ts。
  const imageUrl = kind === "image" && item.asset_id
    ? sourceImageAssetUrl(API_BASE, assetNotebookId(notebookId, itemNotebookId), item.asset_id)
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
  resultSets: (KnowhowResultSet | TypedCollectionResult | SpreadsheetAnalysisResult)[] | undefined;
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
            // key 必须并入 scope:两条来源清单链(仅当前笔记本 / 全部)是同一轮里
            // 两张真卡片,而 sources 的 element_kind/object_type/source_id 全是空串
            // ——不带 scope 时两者的 key 逐字相同(`collection-sources--`)。重复 key
            // 下 React 无法把每张卡认回它自己,展开/收起这类 useState 会串到另一张
            // 卡上。scope 进 key 的判据与它进续跑键的判据是同一条:范围是清单身份的
            // 一部分。缺席时读成 "all",所以带不带这个字段的同一张卡拿到同一个 key
            // (历史回答重开不会因为少一个键就换成另一张卡)。
            <CollectionResultCard
              key={`collection-${resultSet.collection}-${resultSet.element_kind || resultSet.object_type}-${resultSet.source_id || ""}-${resultSet.scope || "all"}`}
              resultSet={resultSet}
              notebookId={notebookId}
              notebookNames={notebookNames}
              onOpenSource={onOpenSource}
            />
          );
        }
        if (resultSet.kind === "spreadsheet") {
          return (
            <SpreadsheetResultCard
              key={`spreadsheet-${resultSet.source_id}-${resultSet.sheet}-${resultSet.operation}`}
              resultSet={resultSet}
              onOpenSource={onOpenSource}
            />
          );
        }
        return null;
      })}
    </div>
  );
}


// plugin_action(ask.reflect_action)展开态的**完整**参数披露:每个参数一行
// 「参数名: 值」,值一个字都不夹,长值换行铺开。
//
// 折叠行那份摘要夹到 120 字符(reasoning-trace.ts 的
// PLUGIN_ACTION_ARGUMENTS_MAX_CHARS),而后端单个参数最长放行 300 字符
// (REFLECT_ACTION_ARGUMENT_MAX_CHARS)。两态若共用同一个夹过的格式化器,被夹掉的
// 那一截在整个界面上就无处可看,而 docs/product-and-api.md「Reflect plugin actions」
// 的 What leaves the deployment 段承诺的正是相反的事:提问的人始终看得见替他发出去
// 的原文——那是这个特性代替内容过滤器的**全部**依仗。所以夹断留给折叠态,展开态
// 逐项铺开。零参数(全空串或畸形 payload)不渲染任何容器,不给一个空框。
function PluginActionArguments({ step }: { step: ReasoningTraceStep }) {
  const argumentRows = getPluginActionArguments(step);
  if (argumentRows.length === 0) return null;
  return (
    <div className="reasoning-trace-arguments">
      <div className="reasoning-trace-arguments-title">替你发出的参数</div>
      {argumentRows.map(({ name, value }) => (
        <div className="reasoning-trace-argument" key={name}>
          {/* ⚠ 这两个必须是 div 不能是 span:`.reasoning-trace-list li span` 是个
              后代选择器,它把步骤那一列的圆角徽章样式(22px 定高、nowrap、
              overflow:hidden)加给 li 里的**每一个** span——参数值套进去会被截成一
              行看不全,正好废掉这一块存在的理由。 */}
          <div className="reasoning-trace-argument-name">{name}:</div>
          <div className="reasoning-trace-argument-value">{value}</div>
        </div>
      ))}
    </div>
  );
}


function ReasoningTraceDetail({ text }: { text: string }) {
  const id = useId();
  const textRef = useRef<HTMLElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [overflowing, setOverflowing] = useState(false);

  useLayoutEffect(() => {
    const element = textRef.current;
    // Keep the collapse control while reading; measure the two-line preview
    // again on collapse. Observe width changes from both window and panel resize.
    if (!element || expanded) return;
    const measure = () => setOverflowing(element.scrollHeight > element.clientHeight);
    measure();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    observer?.observe(element);
    return () => observer?.disconnect();
  }, [text, expanded]);

  return (
    <div className="reasoning-trace-detail">
      <small
        ref={textRef}
        id={id}
        className={`reasoning-trace-detail-text${expanded ? " expanded" : ""}`}
        tabIndex={expanded ? 0 : undefined}
      >
        {text}
      </small>
      {overflowing && (
        <button
          type="button"
          className="link-button reasoning-trace-detail-toggle"
          aria-expanded={expanded}
          aria-controls={id}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? "收起" : "查看完整内容"}
        </button>
      )}
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
                <span>{getTraceStepLabel(step)}</span>
                <strong>{step.summary}</strong>
                {(detail || hasTime) && (
                  <div className="reasoning-trace-meta">
                    {detail && <ReasoningTraceDetail key={detail} text={detail} />}
                    {hasTime && (
                      <time className={`reasoning-trace-time ${(step.duration_ms ?? 0) >= 10000 ? "slow" : ""}`}>
                        {formatDuration(step.duration_ms ?? 0)}
                      </time>
                    )}
                  </div>
                )}
                <PluginActionArguments step={step} />
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
    return errors.map((error) => {
      const serviceId = sanitizeModelServiceId(error.service_id);
      const supportId = sanitizeModelSupportId(error.support_id);
      // 同一服务、同一次调用(support_id)的重复报警只留一条;但同一服务在同一轮里
      // 出现两种不同现象(例如先答空、再连接失败)必须分别可见,故现象也进键。
      // 这个键同时充当 React key:去重键与渲染键必须是同一个,否则两条现象不同
      // 的行会在这里被保留、却在列表里撞 key。
      const key = `${serviceId}\0${error.model}\0${supportId}\0${error.message ?? ""}\0${error.detail ?? ""}`;
      return { error, serviceId, supportId, key };
    }).filter(({ key }) => {
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
        {uniqueErrors.map(({ error, serviceId, supportId, key }) => {
          const isTesting = testingAllModels
            || Boolean(testingModelServices[serviceId])
            || Boolean(testing[serviceId]);
          return (
            <li key={key}>
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
  notebookHref,
  onOpenNotebook,
  dismissSignal,
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
  /** 跨笔记本引用的「打开笔记本」出口，原样透传给引用小卡片（它不知道路由形状）。
   *  可选——笔记本内问答本来就在这个笔记本里，它不传，那颗按钮整个不渲染，DOM 与
   *  既有行为逐字不变（回归门：global-ask.component.test.tsx 的
   *  「the in-notebook citation card has no open-notebook link」）。 */
  notebookHref?: (notebookId: string, sourceId: string) => string;
  /** 「打开笔记本」按下之后的收尾（全局问答借此收起浮窗）。只在 notebookHref
   *  也传了、且那颗按钮真渲染出来时才有意义。 */
  onOpenNotebook?: () => void;
  /** 值一变就收起本视图里开着的引用卡。宿主借此在自己被收起/隐藏时把卡片一并收掉
   *  ——卡片是 `position: fixed` 且在 window 捕获期接管 Esc 的，留着不收会在宿主
   *  已经看不见之后继续吃掉页面的下一次 Esc（全局问答浮窗收起后本组件仍然挂载）。
   *  缺省即不参与，单库调用点不传，行为逐字不变。 */
  dismissSignal?: unknown;
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
   *  失败，绝不用 toast（长任务按钮红线：失败文案必须持久可见）。
   *  ⚠ 同一条回调也是**外部证据引用卡**「导入为来源」的通道
   *  （``ask.reflect_action``，设计文档 §七）：两处导入的都是一个库外 URL，走的
   *  都是核心 URL 来源端点，共用一个回调才不会出现「一个入口有容量单飞、另一个
   *  没有」这种半套。 */
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
  // 后端把完整性提示附在答案末尾。模型正文可能以未闭合的 Markdown 代码围栏
  // 结束；把已知的服务端提示单独渲染，避免它被吞进代码块。复制仍使用原文。
  const completenessNotice = answer.completeness_notice ?? "";
  const renderedAnswerText = answerBodyWithoutCompletenessNotice(answerText, completenessNotice);
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
  // 「把一个库外链接导入成本笔记本的一条来源」的逐行状态机。**一份，两个面共用**：
  // 站外来源建议清单（``ask.gap_consult``）与外部证据引用卡（``ask.reflect_action``，
  // 设计文档 §七）。键是 URL，所以同一个链接在两处只有一格状态——任一处导入完成，
  // 另一处立刻显示「已导入」，不会重复排入同一个链接来源（后端导入端点既无单飞、
  // 也不按 URL 去重）。
  // ⚠ 住在这里而不是引用卡里:引用卡是随点外部/滚动/Esc 卸载的浮层,状态跟着它
  // 走的话「已导入」会在关掉浮层的一瞬间蒸发,用户重新点开同一条引用看到的又是
  // 可点的「导入为来源」——于是导入第二次。完整论证与跨轮去重的登记见
  // import-row-state.tsx 顶部。
  const importController = useImportRowController(
    onImportGapSuggestion,
    importGapSuggestionDisabledReason,
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
  // 没有 active notebook 不再是「整块不渲染」的理由:归属逐行由 assetNotebookId 裁定,
  // 全局问答下每一行退到那条引用自己的所属库。真正取不到图的行(既无 active、引用也
  // 没有 notebook_id——旧答案)由 InlineCitationImages 逐行显示「图片不可用」。
  const renderCitationImages = (items: CitationImageSlotItem[]) => (
    <InlineCitationImages
      rows={resolveCitationImageRows(items, (key) => referencesByCitationKey[key])}
      notebookId={notebookId}
      onPreviewImage={previewImage}
    />
  );
  useEffect(() => setCitePopover(null), [answer.answer_id]);
  useEffect(() => setCitePopover(null), [dismissSignal]);
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
        const { personal, base, external } = computeSourceTierCounts(references);
        if (personal + base + external === 0) return null;
        return (
          <span
            className="tag source-dist"
            title={external > 0
              // 第三格只在真有库外引用时出现,文案与 title 都是:没有外部证据的
              // 回答,这枚徽章逐字等于接入前(设计文档 §九 不变量 6 的界面侧同款
              // 纪律——关闭态零差异)。
              ? "本次引用的来源分布（个人知识库 / 公共知识库 / 笔记本之外）"
              : "本次引用的来源分布（个人知识库 / 公共知识库）"}
          >
            来源 · 个人 {personal}
            {base > 0 && <> · <strong className="source-dist-base">公共 {base}</strong></>}
            {external > 0 && <> · <strong className="source-dist-external">外部 {external}</strong></>}
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
        answer={renderedAnswerText}
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
      {completenessNotice && (
        <p className="answer-completeness-notice">{completenessNotice}</p>
      )}
      <KnowhowResultSets
        resultSets={answer.result_sets}
        batchCoverage={answer.result_coverage}
        onOpenKnowhowRow={onOpenKnowhowRow}
        notebookId={notebookId}
        notebookNames={notebookNames}
        onOpenSource={onOpenSource}
      />
      <ExternalEvidenceSection
        section={answer.external_evidence}
        suggestions={answer.gap_suggestions ?? []}
      />
      <GapSuggestionsPanel
        suggestions={answer.gap_suggestions ?? []}
        egress={answer.gap_egress}
        controller={importController}
      />
      {answer.reasoning_trace && answer.reasoning_trace.length > 0 && (
        <ReasoningTracePanel steps={answer.reasoning_trace} />
      )}
      {citePopover && (
        <CitationPopover
          reference={citePopover.reference}
          notebookId={notebookId}
          notebookNames={notebookNames}
          notebookHref={notebookHref}
          // 与 onOpenKnowledgeGraph 等同一条收尾路径：跳转是同页 hash 路由，
          // 不收起的话卡片会继续浮在刚跳到的笔记本上。
          onOpenNotebook={onOpenNotebook ? () => {
            setCitePopover(null);
            onOpenNotebook();
          } : undefined}
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
          importController={importController}
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
