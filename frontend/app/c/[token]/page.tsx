"use client";

// 公开分享的问答会话页 —— 与 `app/r/[token]/page.tsx`（报告分享页）同构的姊妹，
// 唯一不需要登录的第二个界面。
//
// 它刻意不 import 主应用的**产品层**：没有 notebook 上下文、没有 session、没有引用
// 跳转到原文。渲染的就是后端白名单投影里的多轮问答正文与引用出处。
//
// 但**渲染管线**必须与站内共用，不能各写一份（红线「报告公开分享」逐条继承）：
//   * `remarkCitations` 是 `[k]` / 【k】标记链接化的唯一实现；
//   * `remarkGfmPlugin` 而非裸 `remarkGfm`——单个 `~`（`7~5nm`）不能被当删除线；
//   * `katex/dist/katex.min.css` 少了它，rehype-katex 产出的 `.katex-mathml` 不会
//     被裁掉，MathML 与 HTML 两份同时上屏，公式逐字符渲染两遍；
//   * 表格/代码块沿用 `.answer-table-wrap` / `.answer-code`，宽内容才在自己的内容块
//     里横向滚动，而不是把整页顶宽。

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useParams } from "next/navigation";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";

import { remarkCitations } from "../../answer-citations";
import { remarkGfmPlugin } from "../../markdown-gfm";
import { normalizeMathMarkdown } from "../../math-markdown";
import { ImagePreviewModal } from "../../image-preview-modal";
import {
  CITATION_IMAGE_SLOT_ATTRIBUTE,
  citationImageSlotItems,
  rehypeCitationImages,
  type CitationImageIdsByKey,
} from "../../rehype-citation-images";
import {
  fetchPublicConversation,
  publicConversationCitationRefs,
  publicConversationImageUrl,
  publicConversationRefNumber,
  type PublicConversationT,
  type PublicImageT,
  type PublicTurnT,
} from "../../public-conversation";

type LoadState =
  | { kind: "loading" }
  | { kind: "missing" }
  | { kind: "error" }
  | { kind: "ready"; conversation: PublicConversationT };

type PublicImagePreview = Readonly<{
  image: PublicImageT;
  referenceLabel: string;
}>;

const EVIDENCE_LABELS: Record<string, string> = {
  grounded: "有据",
  overview: "概述",
  inferred: "推断",
};

export default function PublicConversationPage() {
  const params = useParams<{ token: string }>();
  const token = String(params?.token || "");
  const [state, setState] = useState<LoadState>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    setState({ kind: "loading" });
    fetchPublicConversation(token)
      .then((conversation) => {
        if (cancelled) return;
        setState(conversation ? { kind: "ready", conversation } : { kind: "missing" });
      })
      .catch(() => {
        if (!cancelled) setState({ kind: "error" });
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  if (state.kind === "loading") {
    return <main className="public-report"><p className="public-report-note">正在加载…</p></main>;
  }
  if (state.kind === "missing") {
    return (
      <main className="public-report">
        <h1>链接不可用</h1>
        <p className="public-report-note">
          这个分享链接不存在，或者已被创建者撤销。
        </p>
      </main>
    );
  }
  if (state.kind === "error") {
    return (
      <main className="public-report">
        <h1>暂时打不开</h1>
        <p className="public-report-note">请稍后重试。</p>
      </main>
    );
  }

  const { conversation } = state;

  return (
    <main className="public-report public-conversation">
      <header className="public-report-head">
        <p className="public-report-eyebrow">问答会话 · 只读分享</p>
        <h1>{conversation.title || "问答会话"}</h1>
        <p className="public-report-meta">
          <span>这是只读快照</span>
          {conversation.shared_at && (
            <span>
              内容截至 <PublicTime value={conversation.shared_at} />
            </span>
          )}
          {conversation.turns.length > 0 && <span>{conversation.turns.length} 轮问答</span>}
        </p>
      </header>

      {conversation.turns.map((turn, index) => (
        <PublicTurnView key={index} turn={turn} index={index} token={token} />
      ))}

      {conversation.truncated_turns && (
        <p className="public-report-note">会话过长，此处只展示前 {conversation.turns.length} 轮。</p>
      )}

      <footer className="public-report-foot">
        <p className="public-report-note">本页是只读分享副本，引用可核对但不可打开原始资料。</p>
      </footer>
    </main>
  );
}

/** 一轮问答：问题 + 答案(经渲染管线) + 附图 + 引用出处。每轮自成一个 `[k]` 号段。 */
function PublicTurnView({
  turn,
  index,
  token,
}: {
  turn: PublicTurnT;
  index: number;
  token: string;
}) {
  // 正文标记点开的那条引用：清单里高亮它。号段是**每轮独立**的，所以高亮态与
  // DOM id 都按本轮索引隔离，避免跨轮 k1 撞车。
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [previewImage, setPreviewImage] = useState<PublicImagePreview | null>(null);
  const previewReturnFocusRef = useRef<string | null>(null);
  const citationRefs = useMemo(
    () => publicConversationCitationRefs(turn.references),
    [turn.references],
  );
  const imagesByAlias = useMemo(
    () => new Map(turn.images.map((image) => [image.alias, image])),
    [turn.images],
  );
  const imageIdsByCitationKey: CitationImageIdsByKey = useMemo(() => {
    const rows: Record<string, string[]> = {};
    for (const image of turn.images) {
      for (const key of image.reference_keys ?? []) {
        (rows[key] ??= []).push(image.alias);
      }
    }
    return rows;
  }, [turn.images]);
  const markdownCitationRefs = useMemo(() => {
    const rows = { ...citationRefs };
    for (const key of Object.keys(imageIdsByCitationKey)) {
      if (rows[key]) continue;
      const number = key.match(/^k?(\d+)$/)?.[1];
      rows[key] = { id: `image:${key}`, displayLabel: number ? `[${number}]` : key };
    }
    return rows;
  }, [citationRefs, imageIdsByCitationKey]);
  const refDomId = (key: string) => `ref-t${index}-${key}`;

  useLayoutEffect(() => {
    if (previewImage) return;
    const targetKey = previewReturnFocusRef.current;
    previewReturnFocusRef.current = null;
    if (!targetKey) return;
    const target = document.querySelector<HTMLElement>(
      `[data-answer-image-preview-return="${targetKey}"]`,
    );
    if (target?.isConnected) target.focus({ preventScroll: true });
  }, [previewImage]);

  const openReference = (key: string) => {
    setSelectedKey(key);
    const target = document.getElementById(refDomId(key));
    if (!target) return;
    target.scrollIntoView({ behavior: "smooth", block: "center" });
    // 只滚动不移焦，键盘/读屏用户点完仍停在正文那个按钮上。
    target.focus({ preventScroll: true });
  };

  const components = {
    a({ href, children }: { href?: string; children?: React.ReactNode }) {
      if (href?.startsWith("cite:")) {
        const key = href.slice(5);
        if (citationRefs[key]) {
          return (
            <button
              type="button"
              className={`cite-chip${selectedKey === key ? " active" : ""}`}
              onClick={() => openReference(key)}
            >
              {children}
            </button>
          );
        }
        return <span>{children}</span>;
      }
      return (
        <a href={href} target="_blank" rel="noreferrer">
          {children}
        </a>
      );
    },
    pre({ children }: { children?: React.ReactNode }) {
      return <pre className="answer-code">{children}</pre>;
    },
    table({ children }: { children?: React.ReactNode }) {
      return (
        <div className="answer-table-wrap">
          <table className="answer-table">{children}</table>
        </div>
      );
    },
    aside({ node, children }: { node?: { properties?: Record<string, unknown> }; children?: React.ReactNode }) {
      const items = citationImageSlotItems(node?.properties?.[CITATION_IMAGE_SLOT_ATTRIBUTE]);
      if (items.length === 0) return <aside>{children}</aside>;
      const rows = items.flatMap(({ citationKey, imageId }) => {
        const image = imagesByAlias.get(imageId);
        return image ? [{ citationKey, image }] : [];
      });
      if (rows.length === 0) return null;
      const labels = [...new Set(rows
        .map(({ citationKey }) => citationRefs[citationKey]?.displayLabel)
        .filter((value): value is string => Boolean(value)))];
      return (
        <aside className="answer-inline-images" aria-label="本段附图">
          <div className="answer-inline-images-heading">
            <span>本段附图</span>
            {labels.length > 0 && <span>{labels.join("、")}</span>}
            <small>模型未直接读取图片</small>
          </div>
          <ul className="answer-inline-image-list">
            {rows.map(({ citationKey, image }) => (
              <li key={image.alias} className="answer-inline-image-item">
                <img
                  className="element-image"
                  src={publicConversationImageUrl(token, image.alias)}
                  alt={image.caption || `${citationRefs[citationKey]?.displayLabel || "引用"}的附图`}
                  loading="lazy"
                />
                <button
                  type="button"
                  className="answer-inline-image-open"
                  aria-label="放大查看本段附图"
                  data-answer-image-preview-return={`${index}:${image.alias}`}
                  onClick={(event) => {
                    previewReturnFocusRef.current = event.currentTarget.dataset.answerImagePreviewReturn || null;
                    setPreviewImage({
                      image,
                      referenceLabel: citationRefs[citationKey]?.displayLabel || "",
                    });
                  }}
                />
              </li>
            ))}
          </ul>
        </aside>
      );
    },
  } as Parameters<typeof ReactMarkdown>[0]["components"];

  const evidenceLabel = EVIDENCE_LABELS[turn.evidence_level];

  return (
    <section className="public-turn">
      <div className="public-turn-question">
        <span className="public-turn-role">提问</span>
        <div className="public-turn-qbody">
          <p>{turn.question}</p>
          {turn.asked_at && (
            <span className="public-turn-time">
              <PublicTime value={turn.asked_at} />
            </span>
          )}
        </div>
      </div>

      <article className="report-markdown answer-markdown public-turn-answer">
        {evidenceLabel && (
          <span className={`public-turn-evidence evidence-${turn.evidence_level}`}>{evidenceLabel}</span>
        )}
        <ReactMarkdown
          remarkPlugins={[
            remarkGfmPlugin,
            remarkMath,
            [remarkCitations, markdownCitationRefs] as [typeof remarkCitations, typeof markdownCitationRefs],
          ]}
          rehypePlugins={[
            rehypeKatex,
            [rehypeCitationImages, imageIdsByCitationKey] as [typeof rehypeCitationImages, CitationImageIdsByKey],
          ]}
          // 默认 urlTransform 会清掉 cite: 协议 → 引用编号丢失;放行 cite:,其余仍走
          // 默认清洗(防 javascript: 等不安全协议)。
          urlTransform={(url) => (url.startsWith("cite:") ? url : defaultUrlTransform(url))}
          components={components}
        >
          {normalizeMathMarkdown(turn.answer_md)}
        </ReactMarkdown>
      </article>

      {/* C-1：清单卡不进 v1，但绝不静默丢弃——留一句可见说明在原本的位置。 */}
      {turn.omitted_result_sets > 0 && (
        <p className="public-report-note public-turn-omitted">
          此回答还包含未公开的清单内容（{turn.omitted_result_sets} 项）。
        </p>
      )}

      {previewImage && (
        <ImagePreviewModal
          referenceLabel={previewImage.referenceLabel}
          onClose={() => setPreviewImage(null)}
        >
          <img
            src={publicConversationImageUrl(token, previewImage.image.alias)}
            alt={previewImage.image.caption || "本段附图"}
          />
        </ImagePreviewModal>
      )}

      {turn.references.length > 0 && (
        <section className="public-report-references" aria-label="引用出处">
          <h2>引用出处</h2>
          <ol>
            {turn.references.map((reference, refIndex) => (
              <li
                key={reference.key || refIndex}
                id={refDomId(reference.key)}
                tabIndex={-1}
                className={selectedKey === reference.key ? "active" : undefined}
              >
                <span className="public-report-refnum" aria-hidden="true">
                  {publicConversationRefNumber(reference, refIndex)}
                </span>
                <div className="public-report-refbody">
                  <strong>{reference.title || reference.file_name || "(未命名资料)"}</strong>
                  {reference.title_truncated && (
                    <small className="public-report-truncated">（标题过长，已截断）</small>
                  )}
                  {reference.location && <span className="public-report-locus">{reference.location}</span>}
                  {reference.file_name && reference.file_name !== reference.title && (
                    <small>原始文件：{reference.file_name}</small>
                  )}
                  {reference.file_name_truncated && (
                    <small className="public-report-truncated">（原始文件名过长，已截断）</small>
                  )}
                  {reference.snippet && <blockquote>{reference.snippet}</blockquote>}
                  {reference.snippet_truncated && (
                    <small className="public-report-truncated">（摘录过长，已截断）</small>
                  )}
                </div>
              </li>
            ))}
          </ol>
          {turn.truncated_references && (
            <p className="public-report-note">
              引用出处过多，此处只展示前 {turn.references.length} 条。
            </p>
          )}
        </section>
      )}
    </section>
  );
}

/** 浏览器本地时区渲染；服务端渲染时先留空，避免 hydration 前后不一致。 */
function PublicTime({ value }: { value: string }) {
  const [text, setText] = useState("");
  useEffect(() => {
    const parsed = new Date(value);
    setText(Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString());
  }, [value]);
  return <>{text}</>;
}
