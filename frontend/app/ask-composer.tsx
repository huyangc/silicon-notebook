import { useEffect, useRef, type KeyboardEvent, type ReactNode } from "react";
import { Square } from "lucide-react";


// 输入框自动增高的上限(px),与 globals.css .chat-input 的 max-height 一致;超过则内部滚动。
const MAX_INPUT_HEIGHT = 160;


type AskComposerProps = {
  value: string;
  placeholder: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  onAbort: () => void;
  running: boolean;
  abortLabel?: string;
  // 硬约束:笔记本无来源且无挂载参考库时为 true —— 锁死输入框与发送键
  // (判据见 ask-availability.isAskBlocked)。与 running 互斥:被锁时不可能在生成中。
  disabled?: boolean;
  // 内容本身当前不可提交(唯一来源:提问超出 ASK_INPUT_LIMITS.questionMaxChars)。
  // 与 disabled 刻意分成两个 prop:disabled 会连输入框一起锁死,而超限时输入框**必须**
  // 保持可编辑——用户正要做的就是把它改短。理由同「不替用户裁剪」:护栏是拦住提交。
  //
  // 判据留在本组件而不是只在调用方,是因为提交有两条路:发送键与 Enter。Enter 的
  // handler 就在下面,调用方够不着它,只 gate 按钮会让超限的问题从键盘照样发出去。
  submitBlocked?: boolean;
  children?: ReactNode;
};


export function AskComposer({
  value,
  placeholder,
  onChange,
  onSubmit,
  onAbort,
  running,
  abortLabel = "中断生成",
  disabled = false,
  submitBlocked = false,
  children,
}: AskComposerProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // 随内容自动增高(封顶 MAX_INPUT_HEIGHT,超出内部滚动)。替代已移除的手动 resize 手柄:
  // 长提问 / Shift+Enter 多行不再被压在一行里,又不暴露游离的拖拽把手。先置 auto 让
  // scrollHeight 反映真实内容高度,再夹到上限。
  // 依赖 [value] 覆盖输入变化;但**宽度**变化(窗口 resize、来源栏折叠/展开致主栏变宽窄)
  // 不改 value 却会改变换行数——用 ResizeObserver 补上,且仅在宽度真变时重算,忽略自身
  // 设高触发的高度变化,避免 RO 自激循环。
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    const resize = () => {
      el.style.height = "auto";
      el.style.height = `${Math.min(el.scrollHeight, MAX_INPUT_HEIGHT)}px`;
    };
    resize();
    // ResizeObserver 在浏览器才有(jsdom / SSR 无)——缺席时退化为「仅按 value 重算」,
    // 初次 resize() 已跑,不影响正确性,只是宽度变化不再自动跟。
    if (typeof ResizeObserver === "undefined") return;
    let lastWidth = el.clientWidth;
    const observer = new ResizeObserver(() => {
      if (el.clientWidth !== lastWidth) {
        lastWidth = el.clientWidth;
        resize();
      }
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [value]);

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (
      event.key === "Enter"
      && !event.shiftKey
      && !event.nativeEvent.isComposing
    ) {
      event.preventDefault();
      if (!running && !disabled && !submitBlocked && value.trim()) {
        onSubmit();
      }
    }
  }

  return (
    <div className="chat-input-bar">
      {/* 上排:输入框独占宽度 + 发送按钮。下排(.chat-input-controls):来源数 / 模式标签 /
          提示语,可换行。旧版把四者塞进同一行 grid,输入被挤窄、长 placeholder 折行被裁。 */}
      <div className="chat-input-main">
        <textarea
          ref={textareaRef}
          aria-label="提问"
          className="chat-input"
          rows={1}
          placeholder={placeholder}
          value={value}
          disabled={running || disabled}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={handleKeyDown}
        />
        <button
          className={`send-button ${running ? "stop" : ""}`}
          type="button"
          aria-label={running ? abortLabel : "发送"}
          title={running ? abortLabel : "发送"}
          disabled={!running && (disabled || submitBlocked || !value.trim())}
          onClick={running ? onAbort : onSubmit}
        >
          {running ? <Square size={16} strokeWidth={2.5} /> : "→"}
        </button>
      </div>
      {children && <div className="chat-input-controls">{children}</div>}
    </div>
  );
}
