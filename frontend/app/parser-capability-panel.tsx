import type { ParserEngineCapability } from "./system-api";


const ENGINE_LABELS: Record<ParserEngineCapability["id"], string> = {
  mineru_self_hosted: "自托管 MinerU",
  mineru_cloud: "MinerU 公共云",
  builtin: "内置解析器",
};

const EXECUTION_LABELS: Record<ParserEngineCapability["execution"], string> = {
  local: "本机处理",
  private_service: "私有服务",
  public_cloud: "会发送至公共云",
};

const CAPABILITY_LABELS: Record<ParserEngineCapability["capabilities"][number], string> = {
  structured_text: "结构化文本",
  headings: "标题",
  layout: "版面",
  tables: "表格",
  formulas: "公式",
  images: "图片",
  ocr: "OCR",
};

const UNAVAILABLE_LABELS: Record<NonNullable<ParserEngineCapability["unavailable_reason"]>, string> = {
  disabled: "部署未启用",
  missing_endpoint: "未配置私有服务地址",
  missing_credentials: "未配置云端凭证",
};


export function ParserCapabilityPanel({
  engines,
  compact = false,
}: {
  engines: ParserEngineCapability[];
  compact?: boolean;
}) {
  if (engines.length === 0) return null;
  return (
    <section
      className={`parser-capability-panel${compact ? " is-compact" : ""}`}
      aria-label="自动解析策略"
    >
      <div className="parser-capability-heading">
        <strong>自动解析策略</strong>
        <span>按格式和可用性自动选择，不会静默从自托管 MinerU 切到公共云</span>
      </div>
      <div className="parser-capability-list">
        {engines.map((engine) => (
          <div
            className={`parser-capability-row${engine.available ? " is-available" : " is-unavailable"}`}
            key={engine.id}
          >
            <div className="parser-capability-name">
              <span>{engine.priority}. {ENGINE_LABELS[engine.id]}</span>
              <small>{EXECUTION_LABELS[engine.execution]}</small>
            </div>
            <div className="parser-capability-detail">
              <span>{engine.file_extensions.map((ext) => ext.toUpperCase()).join(" / ")}</span>
              {!compact && (
                <small>{engine.capabilities.map((capability) => CAPABILITY_LABELS[capability]).join("、")}</small>
              )}
            </div>
            <span className="parser-capability-status">
              {engine.available
                ? engine.fallback ? "始终可用 · 兜底" : "可用"
                : UNAVAILABLE_LABELS[engine.unavailable_reason ?? "disabled"]}
            </span>
          </div>
        ))}
      </div>
    </section>
  );
}
