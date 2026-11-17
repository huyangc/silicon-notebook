"use client";

import { Fragment, useEffect, useMemo } from "react";
import { visit } from "unist-util-visit";
import type { Root } from "mdast";
import type { PluggableList } from "unified";
import { AnswerMarkdown } from "./answer-markdown";
import { FormulaView } from "./formula-view";
import { AuthedImage } from "./authed-image";
import { API_BASE } from "./api-config";
import { ImportedShareText } from "./imported-share-text";
import { importedShareReferences } from "./imported-share-references";
import { sourceElementDomId } from "./source-detail-state";
import { descriptionBlocks, elementHeadingLevel, elementLocationNote, elementTypeTag, withSectionDividers } from "./source-element-display";
import { composeTablePartsHtml, groupTableParts, sanitizeTableHtml } from "./table-part-groups";
import { sourceImageAssetUrl } from "./source-image";
import type { SourceElement } from "./workspace-model";

type SourceMarkdownShape = { headingLevel?: 3 | 4 | 5; orderedStart?: number };
function remarkSourceShape({ headingLevel, orderedStart }: SourceMarkdownShape) {
  return (tree: Root) => {
    if (headingLevel !== undefined) visit(tree, "heading", (node) => { node.depth = headingLevel; });
    let numbered = false;
    if (orderedStart !== undefined) visit(tree, "list", (node) => {
      if (node.ordered && !numbered) { node.start = orderedStart; numbered = true; }
    });
  };
}

function SourceMarkdown({ answer, headingLevel, orderedStart }: { answer: string } & SourceMarkdownShape) {
  const plugins = useMemo<PluggableList>(() => [[remarkSourceShape, { headingLevel, orderedStart }]], [headingLevel, orderedStart]);
  return <AnswerMarkdown answer={answer} allowImages={false} extraRemarkPlugins={plugins} onReferenceClick={() => undefined} />;
}

export function SourcePreview({ elements, notebookId, highlightedElementId = "" }: {
  elements: SourceElement[];
  notebookId: string;
  highlightedElementId?: string;
}) {
  return <>{groupTableParts(withSectionDividers(elements)).map((group) => (
    <Fragment key={group.kind === "single" ? group.element.id : group.elements[0].id}>
      {group.divider && <div className="source-element-divider"><span>{group.divider}</span></div>}
      {group.kind === "single"
        ? <SourceElementCard element={group.element} highlighted={group.element.id === highlightedElementId} notebookId={notebookId} />
        : <TablePartGroupCard elements={group.elements} highlightedElementId={highlightedElementId} />}
    </Fragment>
  ))}</>;
}

function SourceElementCard({ element, highlighted, notebookId }: {
  element: SourceElement;
  highlighted: boolean;
  notebookId: string;
}) {
  const note = elementLocationNote(element);
  const tag = elementTypeTag(element.element_type);
  return (
    <article
      className={`item source-element-card${highlighted ? " source-element-card--highlighted" : ""}`}
      id={sourceElementDomId(element.id)}
    >
      {(note || tag) && (
        <div className="element-head">
          {note && <span className="element-location-note">{note}</span>}
          {tag && <span className="tag element-type-tag">{tag}</span>}
        </div>
      )}
      <ElementBody element={element} notebookId={notebookId} />
    </article>
  );
}

// 同一张表被解析层切成的多个 table_part 元素,合并成一张卡片、一张 <table>——
// 每段一个 tbody,domId 与单元素卡片一致,引用跳转(getElementById)与逐段高亮不用
// 区分「这元素是不是被合并过」。
function TablePartGroupCard({ elements, highlightedElementId }: {
  elements: SourceElement[];
  highlightedElementId: string;
}) {
  // 拼好的表格 HTML 只依赖元素序列本身(id + table_html),不掺高亮态:引用跳转的
  // 高亮进/出各触发一次 highlightedElementId 变化(2.6s 后自动清空),若高亮态混进
  // 这段字符串,每次翻转都要把整张合并表格重新拼一遍再整段丢进
  // dangerouslySetInnerHTML,几十上百行的表格因此整体重排、还会打断用户在表格里的
  // 文本选区。依赖数组用 id 串这个稳定量,不直接用 elements——JSX 里
  // groupTableParts(...) 每次渲染都会产出新数组。
  const partsKey = elements.map((element) => element.id).join(",");
  const html = useMemo(
    () => composeTablePartsHtml(
      elements.map((element) => ({
        domId: sourceElementDomId(element.id),
        html: typeof element.metadata?.table_html === "string" ? element.metadata.table_html : "",
      })),
      sanitizeTableHtml,
    ),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 按 partsKey 而不是 elements 重算,见上注释
    [partsKey],
  );
  // 高亮只是给已经渲染好的 <tbody> 切 class,不重建 innerHTML;被引用的那一段仍然
  // 可见高亮、可被 sourceElementDomId 定位滚动(scrollIntoView 走 use-source-library.ts
  // 里同一个 getElementById)。
  useEffect(() => {
    for (const element of elements) {
      document
        .getElementById(sourceElementDomId(element.id))
        ?.classList.toggle("table-part--highlighted", element.id === highlightedElementId);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 同上,用 partsKey 代表 elements
  }, [partsKey, highlightedElementId]);
  return (
    <article className="item source-element-card">
      <div className="element-table" dangerouslySetInnerHTML={{ __html: html }} />
    </article>
  );
}

function ElementBody({ element, notebookId }: { element: SourceElement; notebookId: string }) {
  const refs = importedShareReferences(element.metadata?.share_references);
  if (refs.length && element.element_type === "paragraph") {
    const markdown = typeof element.metadata?.preview_markdown === "string" ? element.metadata.preview_markdown : element.text;
    return <ImportedShareText text={markdown} references={element.metadata.share_references} />;
  }
  return <>
    <SourceElementBody element={element} notebookId={notebookId} />
    {refs.length > 0 && <ImportedShareText text={refs.map((ref) => `[${ref.key}]`).join(" ")} references={refs} />}
  </>;
}

function SourceElementBody({ element, notebookId }: { element: SourceElement; notebookId: string }) {
  const markdown = typeof element.metadata?.preview_markdown === "string"
    ? element.metadata.preview_markdown : "";
  if (element.element_type === "heading") {
    if (markdown) {
      return <SourceMarkdown answer={markdown} headingLevel={elementHeadingLevel(element)} />;
    }
    return <SourceMarkdown answer={`${"#".repeat(elementHeadingLevel(element))} ${element.text}`} />;
  }
  if (element.element_type === "code_block") {
    return <pre className="answer-code"><code>{element.text}</code></pre>;
  }
  if (element.element_type === "list_item") {
    if (markdown) {
      const ordinal = element.metadata?.ordered_list_ordinal;
      return <SourceMarkdown answer={markdown} orderedStart={typeof ordinal === "number" ? ordinal : undefined} />;
    }
    return <SourceMarkdown answer={/^\s*(?:[-*+] |\d+[.)] )/.test(element.text) ? element.text : `- ${element.text}`} />;
  }
  if (element.element_type === "formula") {
    return <FormulaView latex={element.text} />;
  }
  if (element.element_type === "table") {
    const html = typeof element.metadata?.table_html === "string" ? element.metadata.table_html : "";
    if (html) {
      return (
        <div
          className="element-table"
          dangerouslySetInnerHTML={{ __html: sanitizeTableHtml(html) }}
        />
      );
    }
  }
  if (element.element_type === "image") {
    const assetId = typeof element.metadata?.asset_id === "string" ? element.metadata.asset_id : "";
    const caption = typeof element.metadata?.caption === "string" ? element.metadata.caption : "";
    // markdown 的 `> **图片描述**` 引用块。与图注并列渲染而不是二选一：图注是
    // 一行标签，描述是对图的展开说明，两者都写了就都显示。
    const description = typeof element.metadata?.description === "string"
      ? element.metadata.description
      : "";
    const descriptionBlockList = descriptionBlocks(description);
    const descriptionNode = descriptionBlockList.length > 0 ? (
      <div className="element-image-description">
        {descriptionBlockList.map((block, index) =>
          block.kind === "code" ? (
            <pre key={index} className="element-image-code" data-language={block.lang || undefined}>
              <code>{block.text}</code>
            </pre>
          ) : (
            <p key={index}>{block.text}</p>
          )
        )}
      </div>
    ) : null;
    // 无 caption 时的图片 alt 只取描述里的第一段正文(paragraph),不把围栏代码块
    // (如电路网表)原样念给屏幕阅读器。
    const descriptionAlt = descriptionBlockList.find((block) => block.kind === "paragraph")?.text ?? "";
    const url = sourceImageAssetUrl(API_BASE, notebookId, assetId);
    if (url) {
      return (
        <figure className="element-image-figure">
          <AuthedImage url={url} alt={caption || descriptionAlt || "figure"} />
          {caption ? <figcaption>{caption}</figcaption> : null}
          {descriptionNode}
        </figure>
      );
    }
    if (caption || descriptionNode) {
      return (
        <figure className="element-image-figure">
          {caption ? <figcaption>{caption}</figcaption> : null}
          {descriptionNode}
        </figure>
      );
    }
    // 无可渲染图片资源(如 MINERU_RETURN_IMAGES=0 关闭图片)且既无 caption 也无
    // 图片描述时，回退到与其余元素类型一致的纯文本展示，避免空的占位边框。
    return <SourceMarkdown answer={element.text} />;
  }
  return <SourceMarkdown answer={markdown || element.text} />;
}


