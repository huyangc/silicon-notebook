"use client";

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FocusEvent as ReactFocusEvent,
  type PointerEvent as ReactPointerEvent,
  type RefObject,
} from "react";

import {
  activeTurnFromTops,
  chatTurnDomId,
  magnifyScales,
  nearestIndex,
} from "./chat-turn-nav-model";

export { activeTurnFromTops, chatTurnDomId } from "./chat-turn-nav-model";

type ChatTurnNavProps = {
  /** 当前会话里用户的每次提问，顺序与对话区一致（一轮一条）。 */
  questions: string[];
  /** 对话滚动容器（chat-body）的 ref。 */
  scrollRef: RefObject<HTMLDivElement | null>;
};

/** 浮出的提问卡：定位到聚焦刻度的中心。 */
type Loupe = { i: number; top: number };

/**
 * 「本次会话提问导航」侧栏：右缘一列安静的圆点，一点 = 一次提问。指针沿导轨移动时，
 * 越靠近指针的点越大（Dock 式鱼眼放大镜），正对指针的那条浮出提问卡；点击平滑滚动到
 * 对应轮次并高亮气泡；随阅读位置自动高亮当前轮。纯前端——数据取自内存里的对话轮次。
 */
export function ChatTurnNav({ questions, scrollRef }: ChatTurnNavProps) {
  const count = questions.length;
  const [active, setActive] = useState(0);
  const [loupe, setLoupe] = useState<Loupe | null>(null);

  const navRef = useRef<HTMLElement | null>(null);
  const rafRef = useRef(0);
  const reduceRef = useRef(false);
  // 点击跳转后短暂锁定，避免平滑滚动途中的 scroll 事件把高亮从目标抢走。
  const lockRef = useRef(false);
  const lockTimer = useRef<number | null>(null);

  useEffect(() => {
    reduceRef.current = !!window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
  }, []);

  // --- 滚动跟随：高亮当前正在读的那一轮 ---
  const recompute = useCallback(() => {
    if (lockRef.current) return;
    const root = scrollRef.current;
    if (!root || count === 0) return;
    // 贴到底部时，最后一条提问即当前阅读位置——直接选它，规避短答案下触发线的误差。
    if (root.scrollTop + root.clientHeight >= root.scrollHeight - 4) {
      setActive(count - 1);
      return;
    }
    const rootTop = root.getBoundingClientRect().top;
    const tops: number[] = [];
    for (let i = 0; i < count; i++) {
      const el = document.getElementById(chatTurnDomId(i));
      tops.push(el ? el.getBoundingClientRect().top - rootTop : Number.POSITIVE_INFINITY);
    }
    setActive(activeTurnFromTops(tops));
  }, [count, scrollRef]);

  useEffect(() => {
    const root = scrollRef.current;
    if (!root) return;
    let frame = 0;
    const onScroll = () => {
      if (frame) return;
      frame = window.requestAnimationFrame(() => {
        frame = 0;
        recompute();
      });
    };
    root.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onScroll);
    recompute();
    return () => {
      root.removeEventListener("scroll", onScroll);
      window.removeEventListener("resize", onScroll);
      if (frame) window.cancelAnimationFrame(frame);
    };
  }, [recompute, scrollRef]);

  // --- 放大镜 ---
  const dots = (): HTMLElement[] => {
    const nav = navRef.current;
    return nav ? Array.from(nav.querySelectorAll<HTMLElement>(".chat-turn-nav-dot")) : [];
  };

  // 刻度中心 y（相对 nav）。缩放对称、不移中心，故此中心稳定。
  const centersOf = (els: HTMLElement[]): number[] => {
    const nav = navRef.current;
    if (!nav) return [];
    const navTop = nav.getBoundingClientRect().top;
    return els.map((d) => {
      const r = d.getBoundingClientRect();
      return r.top + r.height / 2 - navTop;
    });
  };

  const onPointerMove = (event: ReactPointerEvent<HTMLElement>) => {
    const nav = navRef.current;
    if (!nav) return;
    const y = event.clientY - nav.getBoundingClientRect().top;
    if (rafRef.current) return;
    rafRef.current = window.requestAnimationFrame(() => {
      rafRef.current = 0;
      const els = dots();
      const centers = centersOf(els);
      const scales = reduceRef.current ? centers.map(() => 1) : magnifyScales(centers, y);
      els.forEach((d, i) => {
        d.style.transition = "none";
        d.style.transform = `scale(${scales[i].toFixed(3)})`;
      });
      const i = nearestIndex(centers, y);
      if (i >= 0) {
        const top = centers[i];
        // 停在同一颗点上不触发重渲染；仅换颗时更新浮卡。
        setLoupe((prev) => (prev && prev.i === i && Math.abs(prev.top - top) < 1 ? prev : { i, top }));
      }
    });
  };

  const releaseDots = () => {
    for (const d of dots()) {
      d.style.transition = "transform .24s cubic-bezier(.22,.61,.36,1)";
      d.style.transform = "";
    }
  };

  const onPointerLeave = () => {
    if (rafRef.current) {
      window.cancelAnimationFrame(rafRef.current);
      rafRef.current = 0;
    }
    releaseDots();
    setLoupe(null);
  };

  // 键盘：聚焦某条时浮出它的提问（不缩放，靠高亮环给足对比）。
  const onFocusIn = (event: ReactFocusEvent<HTMLElement>) => {
    const nav = navRef.current;
    const idx = (event.target as HTMLElement).getAttribute?.("data-index");
    if (!nav || idx == null) return;
    const i = Number(idx);
    const centers = centersOf(dots());
    if (i >= 0 && i < centers.length) setLoupe({ i, top: centers[i] });
  };

  const onFocusOut = (event: ReactFocusEvent<HTMLElement>) => {
    const nav = navRef.current;
    const next = event.relatedTarget as Node | null;
    if (nav && next && nav.contains(next)) return; // 焦点仍在导航内
    setLoupe(null);
  };

  const jump = useCallback((index: number) => {
    const el = document.getElementById(chatTurnDomId(index));
    if (!el) return;
    setActive(index);
    lockRef.current = true;
    if (lockTimer.current) window.clearTimeout(lockTimer.current);
    lockTimer.current = window.setTimeout(() => {
      lockRef.current = false;
    }, 700);
    el.scrollIntoView({ behavior: "smooth", block: "start" });
    const bubble = el.querySelector(".chat-user");
    if (bubble instanceof HTMLElement) {
      bubble.classList.remove("chat-user-flash");
      void bubble.offsetWidth; // 触发重排，让高亮动画可重复播放
      bubble.classList.add("chat-user-flash");
      window.setTimeout(() => bubble.classList.remove("chat-user-flash"), 1400);
    }
  }, []);

  useEffect(
    () => () => {
      if (lockTimer.current) window.clearTimeout(lockTimer.current);
      if (rafRef.current) window.cancelAnimationFrame(rafRef.current);
    },
    [],
  );

  // 只有一条提问时无需导航。
  if (count < 2) return null;

  const focusIndex = loupe?.i ?? -1;

  return (
    <nav
      className="chat-turn-nav"
      aria-label="本次会话的提问"
      ref={navRef}
      onPointerMove={onPointerMove}
      onPointerLeave={onPointerLeave}
      onFocus={onFocusIn}
      onBlur={onFocusOut}
    >
      <div className="chat-turn-nav-rail">
        {questions.map((question, index) => (
          <button
            key={index}
            type="button"
            className={
              "chat-turn-nav-dotbtn"
              + (index === active ? " active" : "")
              + (index === focusIndex ? " focus" : "")
            }
            aria-label={question}
            aria-current={index === active ? "true" : undefined}
            data-index={index}
            onClick={() => jump(index)}
          >
            <span className="chat-turn-nav-dot" aria-hidden="true" />
          </button>
        ))}
      </div>
      {loupe && (
        <div className="chat-turn-nav-loupe" style={{ top: loupe.top }} aria-hidden="true">
          <span className="chat-turn-nav-loupe-num">{loupe.i + 1}</span>
          <span className="chat-turn-nav-loupe-text">{questions[loupe.i]}</span>
        </div>
      )}
    </nav>
  );
}
