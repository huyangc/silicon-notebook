"use client";

import { useEffect, useRef, useState } from "react";

import { fetchInternalAssetBlob } from "./source-api";

// 图片来源资产需鉴权(GET /assets/{id} 走 require_notebook_read),裸 <img src> 会 401;
// 走 fetch→blob→objectURL(与报告归档下载同款鉴权 fetch 惯用法),卸载时 revoke 避免泄漏。
//
// 原本只内联在 page.tsx 的来源详情里；PR-2 T6 把它拆成共享组件，供 Ask 清单结果卡
// （answer-panel.tsx 的 CollectionResultCard，图片元素条目）复用同一套鉴权取图逻辑,
// 不重新发明一份 fetch→blob→objectURL。
//
// 双评审 P1-3:懒加载。exhaustive 档的图片清单一次「展开全部」最多可以同时挂载
// 几百个 AuthedImage(结果卡按初始 20/全量两态渲染,不做虚拟列表分页)——若每个都
// 立刻发鉴权请求,等于瞬间打出几百个并发的同步授权查询,这条成本落在效率红线上
// (新增请求先问代价)。用 IntersectionObserver 把 fetch 推迟到元素真正进入可视
// 区域(含 rootMargin 预取一屏),而不是挂载即发。
//
// 环境不支持 IntersectionObserver(如 jsdom 测试环境)时退化成"立即加载"——这不是
// 妥协,是显式的降级路径:宁可在没有懒加载能力的地方付出（罕见的）全量请求成本,
// 也不能让功能整体不可用/测试挂起。
export function AuthedImage({ url, alt }: { url: string; alt: string }) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [visible, setVisible] = useState(
    () => typeof IntersectionObserver === "undefined",
  );
  const [src, setSrc] = useState<string>("");
  const [failed, setFailed] = useState(false);

  // url 换了就把上一张的状态丢干净。放在**渲染期**而不是 effect 里，是 React 文档
  // 「props 变化时调整 state」的写法：effect 要等这一帧画完才跑，中间会实打实画出
  // 一帧上一张图——而那一帧里的 `src` 已经是下面 cleanup 即将 revoke（或刚刚
  // revoke）的 objectURL，画出来的是一个坏图，不只是「旧图片」。
  //
  // 承重的是第二条：`failed` 不清就会永久粘住。图片放大预览可以左右切换同一个
  // AuthedImage 实例的 url（codex #599 R2 P2），一张图失败之后，后面每一张都会停在
  // 「图片加载失败」，哪怕它自己取得好好的。
  const [loadedUrl, setLoadedUrl] = useState(url);
  if (loadedUrl !== url) {
    setLoadedUrl(url);
    setSrc("");
    setFailed(false);
  }

  // 可见性观察:只在尚不可见、且当前环境真的有 IntersectionObserver 时才装。
  // 一旦触发过一次可见就不再需要继续观察(visible 变 true 后这个 effect 提前
  // return,不重新创建 observer);组件卸载时无条件 disconnect,不留下悬挂的
  // observer 回调。
  useEffect(() => {
    if (visible) return;
    const node = containerRef.current;
    if (!node) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) setVisible(true);
      },
      { rootMargin: "200px" },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [visible]);

  useEffect(() => {
    if (!visible || !url) return;
    let revoked = "";
    let alive = true;
    fetchInternalAssetBlob(url)
      .then((blob) => {
        if (!alive) return;
        revoked = URL.createObjectURL(blob);
        setSrc(revoked);
      })
      .catch(() => alive && setFailed(true));
    return () => { alive = false; if (revoked) URL.revokeObjectURL(revoked); };
  }, [visible, url]);

  // 外层 div 全程保持挂载、ref 不随状态切换而改变目标节点——IntersectionObserver
  // 需要一个贯穿"未可见→已可见→已加载/失败"三态的稳定观察目标。
  return (
    <div ref={containerRef} className="authed-image-frame">
      {failed
        ? <p className="tool-hint">图片加载失败</p>
        : (!visible || !src)
          ? <p className="tool-hint">图片加载中…</p>
          : <img className="element-image" src={src} alt={alt} loading="lazy" />}
    </div>
  );
}
