import { type KeyboardEvent, type ReactNode } from "react";
import { Square } from "lucide-react";


type AskComposerProps = {
  value: string;
  placeholder: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  onAbort: () => void;
  running: boolean;
  children?: ReactNode;
};


export function AskComposer({
  value,
  placeholder,
  onChange,
  onSubmit,
  onAbort,
  running,
  children,
}: AskComposerProps) {
  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (
      event.key === "Enter"
      && !event.shiftKey
      && !event.nativeEvent.isComposing
    ) {
      event.preventDefault();
      if (!running && value.trim()) {
        onSubmit();
      }
    }
  }

  return (
    <div className="chat-input-bar">
      <textarea
        aria-label="提问"
        className="chat-input"
        rows={1}
        placeholder={placeholder}
        value={value}
        disabled={running}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={handleKeyDown}
      />
      {children}
      <button
        className={`send-button ${running ? "stop" : ""}`}
        type="button"
        aria-label={running ? "中断生成" : "发送"}
        title={running ? "中断生成" : "发送"}
        disabled={!running && !value.trim()}
        onClick={running ? onAbort : onSubmit}
      >
        {running ? <Square size={16} strokeWidth={2.5} /> : "→"}
      </button>
    </div>
  );
}
