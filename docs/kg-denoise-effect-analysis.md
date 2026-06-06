# nb-012 去噪重抽 vs pre-denoise 差异分析

- 日期：2026-06-06
- 对比：`silicon_notebook.db`（去噪重抽后）vs `backups/snapshot_pre_denoise_20260606_103747.db`（pre-denoise）
- notebook：`nb-012fb94249` Analog CMOS IC Design（5 本教材）
- 方法：对两库统一用 `is_noise_concept`（master 版）+ 模式正则计数；脚本 `scripts/compare_kg_dbs.py`

## 数据规模

| 类型 | 旧(pre-denoise) | 新(去噪重抽) | 变化 |
|---|---|---|---|
| concept | 7955 | 7985 | ≈持平 |
| claim | 11088 | **16370** | +48% |
| formula | 9032 | 10005 | +11% |
| procedure | 1707 | 1491 | −13% |
| 合计 | 29782 | 35851 | +20% |

## 一、生效的优化 ✅

**1. 概念结构性噪声——几乎清零（去噪的核心目标，达成）**

| 噪声类 | 旧 | 新 |
|---|---|---|
| 图表引用 Fig/Table/Eq | 41 | **0** |
| 章节号 `N.N` 开头 | 271 | **4** |
| 实例标号 `Q1/M5/p8` | 44 | **0** |
| ≤2 字符 | 92 | **2** |
| `is_noise_concept` 判噪 | 634 (8.0%) | 0 (0.0%) |

`prompt` 收紧 + `is_noise_concept` 后置过滤双管齐下，符号/图号/章节号/实例号噪声基本消除。

**2. 符号规则精炼正确**：含 `_`/`^` 的概念 755→369，剩余的全是**合法多词概念**（`Oxide Capacitance (C_ox)`、`Gate oxide thickness (t_ox)`、`V_BE-referenced current source`）——只铲裸符号、保留带符号的真概念，符合预期。

**3. 窗口过滤（textbook）**：5 本中 3 本以 `doc_type=textbook` 重抽（Design/CMOS/Allen-Holberg），习题/索引窗口被跳过。

**4. SQLite 写锁**：重抽全部 `completed`，无 `database is locked`（写优化生效）。

## 二、未生效 / 问题 ⚠️

**1. ★ 过度合并依然存在（最严重，且是最初问题）**
`rebuild_unified_kg` 的 0.90 自动合并阈值对"语义相邻但不同"的真概念仍然错并。实测垃圾簇：
- `[Channel Length] ⇐ drain, source, gate, bulk, diffusion length, minority carrier concentration…`
- `[voltage-voltage feedback] ⇐ 四种反馈拓扑 + loading 变体`
- `[Oxide capacitance] ⇐ threshold voltage, strong inversion, interface charge…`
- `[double-balanced mixer] ⇐ single-balanced mixer…`

去噪只清掉了"裸符号大杂烩"，但 **drain/source/gate、串并/并并反馈、单/双平衡** 这类**真概念**仍在 0.90 下被并。**有界 top-k 只改了候选数量，没改自动合并质量；LLM 预审没作用到 ≥0.90 的自动合并上。**

**2. doc_type 不一致**：Gray(11320) + RF(9161) 仍是 academic（习题没跳），是早期失败重抽的残留，从未用 textbook 重做 → 这 2 本占了 35851 里的 20481，是总量上涨的主因。

**3. claim 爆炸 +48%（11088→16370）**：claim 完全没有去噪/去重。concept 总数持平的真相是"去掉 ~600 噪声概念，新抽取又补了 ~600 真概念"，而 claim 这种无过滤的类型直接膨胀。

**4. 聚类数据陈旧**：concept_clusters 仅 6868 成员 < 7985 概念（1117 个未入簇）→ 最近一次 textbook 重抽后没重跑 rebuild，unified KG 是 dirty 的。

## 三、剩余优化动作（按价值排序）

1. **★ 修过度合并（最高价值，治本）**：①自动合并阈值 0.90→0.94+；②对 ≥0.90 自动合并也走 LLM 预审（而非只审 0.82–0.90 候选）；③加"判别 token 护栏"——只差一个区分词（single/double、low/high、series/shunt、drain/source）的不自动并。
2. **Gray + RF 用 textbook 重抽**：消除 academic 残留、跳习题、去 ~万级冗余。
3. **重跑 rebuild_unified_kg**：聚类已陈旧，需基于当前去噪后概念重建。
4. **claim/formula 去噪 + 去重**：claim +48% 无过滤是新的噪声前沿；可加 claim 级噪声/近重复过滤（去噪本轮只做了 concept）。
5. **formula 平凡式过滤**：10005 个 formula 仍含大量平凡/实例式（本轮未做）。

## 结论

去噪在**概念结构性噪声**上完全达成（图号/章节号/实例号/超短清零，符号精准）。但 **KG 质量的最大短板——过度合并——未解决**，且暴露出 **claim 无过滤膨胀** 与 **doc_type/rebuild 一致性** 两个新问题。下一步最该做的是**修合并质量**，其次统一 doc_type 重抽 + rebuild。
