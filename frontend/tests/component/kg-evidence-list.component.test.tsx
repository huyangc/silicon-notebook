import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { KgEvidenceList } from "../../app/kg-evidence-list";
import type { EvidenceItem } from "../../app/workspace-model";

function evidenceRow(i: number): EvidenceItem {
  return {
    source_id: `src-${i}`,
    source_title: `来源 ${i}`,
    element_id: `el-${i}`,
    element_type: "paragraph",
    location_label: `位置 ${i}`,
    quoted_span: `片段 ${i}`,
    confidence: 0.9,
  };
}

describe("KgEvidenceList (codex PR #639 R1 P2: progressive disclosure)", () => {
  it("shows the empty hint when there is no evidence", () => {
    render(<KgEvidenceList evidence={[]} resetKey="concept-a" />);
    expect(screen.getByText("无")).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("renders only the first 20 rows and no button under the page size", () => {
    const evidence = Array.from({ length: 15 }, (_, i) => evidenceRow(i));
    render(<KgEvidenceList evidence={evidence} resetKey="concept-a" />);
    expect(screen.getAllByText(/^来源 /)).toHaveLength(15);
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("reveals 20 more rows per click and updates the counter, until everything is shown", () => {
    const evidence = Array.from({ length: 45 }, (_, i) => evidenceRow(i));
    render(<KgEvidenceList evidence={evidence} resetKey="concept-a" />);

    expect(screen.getAllByText(/^来源 /)).toHaveLength(20);
    const button = screen.getByRole("button", { name: "显示更多出处（已显示 20/共 45）" });

    fireEvent.click(button);
    expect(screen.getAllByText(/^来源 /)).toHaveLength(40);
    expect(screen.getByRole("button", { name: "显示更多出处（已显示 40/共 45）" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "显示更多出处（已显示 40/共 45）" }));
    expect(screen.getAllByText(/^来源 /)).toHaveLength(45);
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("resets to the first page when resetKey changes (concept switch / home reload), but not when the same concept's evidence grows (load-more-members)", () => {
    const evidenceA = Array.from({ length: 45 }, (_, i) => evidenceRow(i));
    const { rerender } = render(<KgEvidenceList evidence={evidenceA} resetKey="concept-a" />);

    fireEvent.click(screen.getByRole("button", { name: "显示更多出处（已显示 20/共 45）" }));
    expect(screen.getAllByText(/^来源 /)).toHaveLength(40);

    // Same concept, evidence grew (e.g. "load more members" merged in more
    // rows) — the user's already-expanded progress must survive.
    const evidenceAGrown = [...evidenceA, evidenceRow(45), evidenceRow(46)];
    rerender(<KgEvidenceList evidence={evidenceAGrown} resetKey="concept-a" />);
    expect(screen.getAllByText(/^来源 /)).toHaveLength(40);
    expect(screen.getByRole("button", { name: "显示更多出处（已显示 40/共 47）" })).toBeInTheDocument();

    // A different concept (or a home reload landing a fresh first page) —
    // resetKey changes, so the reveal count falls back to 20.
    const evidenceB = Array.from({ length: 30 }, (_, i) => evidenceRow(i));
    rerender(<KgEvidenceList evidence={evidenceB} resetKey="concept-b" />);
    expect(screen.getAllByText(/^来源 /)).toHaveLength(20);
    expect(screen.getByRole("button", { name: "显示更多出处（已显示 20/共 30）" })).toBeInTheDocument();
  });
});
