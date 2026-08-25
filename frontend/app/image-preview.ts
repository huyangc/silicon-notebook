/**
 * image-preview.ts
 *
 * 图片放大预览的纯数据面：一次预览打开时**冻结**的图片快照（items）加当前位置
 * （index）。冻结而不是每次渲染重算，是为了让「3 / 5」这个计数在预览期间稳定：
 * 生成中的回答随时可能再插进来一张图，重算会让计数在用户眼皮底下变。
 */

export type AnswerImagePreviewItem = Readonly<{
  assetId: string;
  alt: string;
  referenceLabel: string;
}>;

export type AnswerImagePreviewRequest = Readonly<{
  /** 本次预览可以左右切换的全部图片，按它们在回答正文里的显示顺序。 */
  items: readonly AnswerImagePreviewItem[];
  /** 当前显示的是 `items` 里的第几张。 */
  index: number;
}>;

type GalleryReference = Readonly<{
  displayLabel: string;
  images: readonly Readonly<{ asset_id: string; caption?: string | null }>[];
}>;

/**
 * 一条回答里可切换的附图清单。
 *
 * 顺序与去重规则必须与 `rehypeCitationImages` 插入图片区块的规则逐条对应——
 * 按引用**首次出现**的先后、同一 asset 只留第一次。调用方传进来的
 * `references` 就是 `buildAnswerReferences` 的输出，它本身已按正文里第一次出现
 * 该引用标记的顺序排列，所以这里只需要再做一次 asset 去重。两处若哪天分叉，
 * 左右切换的顺序会与正文里看到的顺序对不上（这正是
 * `answer-citation-images.component.test.tsx` 里那条按 DOM 顺序对账的用例钉住的）。
 */
export function buildImageGallery(
  references: readonly GalleryReference[],
): AnswerImagePreviewItem[] {
  const seen = new Set<string>();
  const items: AnswerImagePreviewItem[] = [];
  for (const reference of references) {
    for (const image of reference.images) {
      if (!image.asset_id || seen.has(image.asset_id)) continue;
      seen.add(image.asset_id);
      items.push({
        assetId: image.asset_id,
        alt: image.caption || `${reference.displayLabel} 的附图`,
        referenceLabel: reference.displayLabel,
      });
    }
  }
  return items;
}

/**
 * 点开 `current` 这张图：在清单里定位它，定位不到就退化成只有它自己的一张快照
 * （左右按键与切换按钮随之消失）。绝不因为定位不到就打开清单里的**另一张**图。
 */
export function imagePreviewRequest(
  gallery: readonly AnswerImagePreviewItem[],
  current: AnswerImagePreviewItem,
): AnswerImagePreviewRequest {
  const index = gallery.findIndex((item) => item.assetId === current.assetId);
  return index >= 0 ? { items: gallery, index } : { items: [current], index: 0 };
}

/** 当前这一张；index 越界（不应发生）时返回 null，由调用方决定不渲染。 */
export function currentPreviewImage(
  request: AnswerImagePreviewRequest | null,
): AnswerImagePreviewItem | null {
  return request?.items[request.index] ?? null;
}
