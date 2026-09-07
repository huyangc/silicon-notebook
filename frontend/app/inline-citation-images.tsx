/**
 * inline-citation-images.tsx
 *
 * 正文里那一块「引用附图」的唯一渲染实现。
 *
 * 原本内联在 answer-panel.tsx 里，只服务问答。深度报告正文接同一条内联图片管线
 * （`rehype-citation-images` 的块级落位 + 跨引用去重 + 页内预览）时，两个面必须
 * 长成同一个样子、用同一份 alt/标签取值规则——所以把它抽成共享件，而不是在
 * report-view.tsx 再写一份。多一份实现就是多一处会漂移的 aria-label 与 alt。
 */
"use client";

import { API_BASE } from "./api-config";
import type { AnswerReference, CitationImageLike } from "./answer-formatting";
import { AuthedImage } from "./authed-image";
import type { AnswerImagePreviewItem } from "./image-preview";
import type { CitationImageSlotItem } from "./rehype-citation-images";
import { sourceImageAssetUrl } from "./source-image";

// 检索结果带图(T1/T2)：anchor 优先、citation 兜底,与 answer-panel.tsx 其余
// reference* helper 的既有惯例一致(anchor/citation 二选一,由 buildAnswerReferences
// 全有全无保证)。枚举清单行的引用(evidence_context.py collection_item_citations)不调
// attach_citation_images,故 images 恒缺席;但枚举行**锚点**走的是别的装配点,可能带
// 图——这里不做特判,读取路径对两者一视同仁,由数据形状自然决定是否渲染。
//
// F5(评审登记,checkpoint b6541f26):弹层高频开合(点引用→关闭→再点回同一条)会让
// 附图重复下载——AuthedImage 卸载即 revoke objectURL,下次挂载是全新的 fetch,这里
// 没有跨挂载的结果缓存。这是复用既有 AuthedImage 组件带来的已登记代价,不在本处
// 加一层 blob 缓存去解决它:objectURL 的生命周期一旦要跨组件实例存活,谁负责在
// 「最后一个引用者卸载」时 revoke 会显著复杂化,而附图请求本身走鉴权 fetch、体量
// 有限,权衡后维持现状。
//
// 二期把正文接上同一条内联图片管线后,这个已登记代价多出一个**现在必然发生**的
// 具体案例:正文里的引用图片区块与点开这条引用后「本段附图」详情区的缩略图,
// 对同一张 asset 各是一个独立的 AuthedImage 实例(report-view.tsx 的
// ReportMarkdown 里,正文那份走 InlineCitationImages,详情区那份在
// `.cite-detail-images` 里单独渲染)——两者互不知晓对方,于是同一批图片字节会被
// 拉取两次。这正是 F5 描述的那类代价在具体路径上的落点,不是新问题,不需要新的
// 缓存层来解决。
export function referenceImages(reference: AnswerReference): CitationImageLike[] {
  return reference.anchor?.images ?? reference.citation?.images ?? [];
}

export type ResolvedCitationImage = Readonly<{
  reference: AnswerReference;
  image: CitationImageLike;
}>;

export function InlineCitationImages({
  rows,
  notebookId,
  onPreviewImage,
}: {
  rows: readonly ResolvedCitationImage[];
  notebookId: string;
  /** 点开这一张附图。左右切换用的画册由调用方统一定位（AnswerView / ReportMarkdown
   *  各自的 imageGallery）,所以这里只报「点的是哪一张」。没有承接方时图片仍显示但
   *  不可点击。 */
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

/**
 * 一次渲染插进正文的那些槽位条目 → 可渲染的 `{reference, image}` 行。
 *
 * 槽位只带 `citationKey` + `imageId`（`rehype-citation-images` 记的账），解引用规则
 * 两个面必须一致：解不出引用、或该引用下找不到这个资产的条目直接丢弃，绝不落成
 * 一张指向别处的图。
 */
export function resolveCitationImageRows(
  items: readonly CitationImageSlotItem[],
  referenceFor: (citationKey: string) => AnswerReference | undefined,
): ResolvedCitationImage[] {
  return items.flatMap(({ citationKey, imageId }) => {
    const reference = referenceFor(citationKey);
    if (!reference) return [];
    const image = referenceImages(reference).find((candidate) => candidate.asset_id === imageId);
    return image ? [{ reference, image }] : [];
  });
}
