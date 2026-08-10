import { render, screen, within } from "@testing-library/react";
import { expect, test } from "vitest";

import { ParserCapabilityPanel } from "../../app/parser-capability-panel";
import type { ParserEngineCapability } from "../../app/system-api";


const engines: ParserEngineCapability[] = [
  {
    id: "mineru_self_hosted",
    priority: 1,
    execution: "private_service",
    file_extensions: ["pdf", "docx", "pptx"],
    capabilities: ["structured_text", "layout", "tables", "formulas", "images", "ocr"],
    supports_url: true,
    fallback: false,
    available: false,
    unavailable_reason: "missing_endpoint",
  },
  {
    id: "mineru_cloud",
    priority: 2,
    execution: "public_cloud",
    file_extensions: ["pdf", "docx", "pptx"],
    capabilities: ["structured_text", "layout", "tables", "formulas", "images", "ocr"],
    supports_url: true,
    fallback: false,
    available: true,
    unavailable_reason: null,
  },
  {
    id: "builtin",
    priority: 3,
    execution: "local",
    file_extensions: ["pdf", "md", "csv"],
    capabilities: ["structured_text", "headings", "layout", "tables"],
    supports_url: false,
    fallback: true,
    available: true,
    unavailable_reason: null,
  },
];


test("shows automatic order, data boundary, formats, and unavailable reason", () => {
  render(<ParserCapabilityPanel engines={engines} />);
  const panel = screen.getByRole("region", { name: "自动解析策略" });
  expect(panel).toHaveTextContent("不会静默从自托管 MinerU 切到公共云");
  expect(within(panel).getByText("1. 自托管 MinerU")).toBeInTheDocument();
  expect(panel).toHaveTextContent("私有服务");
  expect(panel).toHaveTextContent("未配置私有服务地址");
  expect(panel).toHaveTextContent("会发送至公共云");
  expect(panel).toHaveTextContent("PDF / DOCX / PPTX");
  expect(panel).toHaveTextContent("始终可用 · 兜底");
});


test("renders nothing when connected to an older backend without the registry", () => {
  const { container } = render(<ParserCapabilityPanel engines={[]} />);
  expect(container).toBeEmptyDOMElement();
});
