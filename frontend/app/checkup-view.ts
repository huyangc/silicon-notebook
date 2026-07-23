// 流水线体检(P2)结果 → 看板展示模型的纯装配逻辑。单测于 checkup-view.test.mjs。
//
// 后端 /checkup 返回内部代号(H2..H8)+ 修复动作枚举(fix);这里把「源级」体检项
// (H2–H6)装配成看板「来源状态」块要渲染的行,界面词经 vocabulary.ts 映射。
// H7/H8 是索引级(检索索引过期/损坏),不在这里——它们接进「索引与构建」块,由
// page.tsx 直接读 checkup 判定。

// 值导入须带 .ts 后缀:本模块被 node --test 直接加载(见 scale-index.ts 同款)。
import { CHECKUP_ISSUE } from "./vocabulary.ts";
import type { CheckupItem, CheckupResponse } from "./workspace-model.ts";

// 「来源状态」块要展示的源级体检项,及其展示顺序。H7/H8 刻意排除(索引级)。
const SOURCE_LEVEL_CODES = ["H2", "H3", "H4", "H5", "H6"] as const;

// 每个源级项的计数单位:H2/H3/H6 数的是来源(篇),H4/H5 数的是缺向量的条目(项)。
const SOURCE_CHECK_UNIT: Record<string, string> = {
  H2: "篇",
  H3: "篇",
  H4: "项",
  H5: "项",
  H6: "篇",
};

export type SourceHealthGroup = {
  // 分组键:同一 label 的多个 code 合并后,取首个 code 作稳定 key(H4/H5→H4)。
  key: string;
  // 界面词标签(CHECKUP_ISSUE 映射)。
  label: string;
  // 合并后的命中计数。
  count: number;
  // 计数单位(篇/项)。
  unit: string;
  // 修复动作枚举(reparse|backfill_vectors|extract_kg);同一 label 内一致。
  fix: string;
  // 命中的 source_id 样本(仅 H2/H3 有;H4–H6 为空)。用于取标题 + reparse 带参。
  sample: string[];
};

/**
 * 把体检结果里的源级项(H2–H6,count>0)装配成展示分组。
 *
 * 合并规则:按界面词 label 合并——H4(缺 chunk 向量)与 H5(缺 element 向量)对用户
 * 是同一件事(「检索向量缺失」)、同一个修复(补齐向量),合并成一行避免重复行;
 * 计数相加、样本相接(H4/H5 样本本就为空)。其余各 code 各自成行。
 *
 * 顺序:按 SOURCE_LEVEL_CODES 首次出现的顺序稳定排列。
 */
export function sourceHealthGroups(checkup: CheckupResponse | null): SourceHealthGroup[] {
  if (!checkup) return [];
  const byCode = new Map<string, CheckupItem>();
  for (const c of checkup.checks) byCode.set(c.code, c);
  const byLabel = new Map<string, SourceHealthGroup>();
  for (const code of SOURCE_LEVEL_CODES) {
    const item = byCode.get(code);
    if (!item || item.count <= 0) continue;
    const label = CHECKUP_ISSUE[code] ?? "需要处理";
    const existing = byLabel.get(label);
    if (existing) {
      existing.count += item.count;
      existing.sample = [...existing.sample, ...item.sample];
    } else {
      byLabel.set(label, {
        key: code,
        label,
        count: item.count,
        unit: SOURCE_CHECK_UNIT[code] ?? "项",
        fix: item.fix,
        sample: [...item.sample],
      });
    }
  }
  return [...byLabel.values()];
}

/** 某个体检项的命中计数(未命中/无该项时 0)。H7/H8 由 page.tsx 用它判定索引可信度。 */
export function checkupCount(checkup: CheckupResponse | null, code: string): number {
  if (!checkup) return 0;
  const item = checkup.checks.find((c) => c.code === code);
  return item ? item.count : 0;
}

/**
 * 铃铛聚合提醒的签名:notebook + 当前命中的体检代号集合。内容变化(新问题出现/
 * 旧问题修好)时签名变,铃铛据此重新提示;健康时返回 null(不提示)。
 */
export function checkupAlertSignature(checkup: CheckupResponse | null): string | null {
  if (!checkup || checkup.healthy) return null;
  const hit = checkup.checks
    .filter((c) => c.count > 0)
    .map((c) => c.code)
    .sort();
  if (hit.length === 0) return null;
  return `${checkup.notebook_id}:${hit.join(",")}`;
}
