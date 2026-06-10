# SA — Schema-Aligned Extraction + A/B Calibration (设计)

- **日期**: 2026-06-10
- **状态**: 设计待审
- **范围**: SA-1(抽取对齐到 schema v1.0.0)+ SA-2(A/B 干净切片标定)。SA-3(重抽 36k 底库)deferred。
- **关联**: `docs/superpowers/plans/2026-06-09-unified-kg-scale-roadmap.md` §157(SA);`schema/kg-schema.yaml` v1.0.0(locked)。

## 1. 动机与实测差距

Schema v1.0.0 已锁定 4 原子节点类型 + `validity_scope` + 放宽推理边,但**当前抽取从未对齐**。线上底库 `nb-012fb94249`(36.9k 对象 / 46.4k 边)实测:

| 维度 | 现状(实测) | 目标 | 卡住的能力 |
|---|---|---|---|
| `validity_scope` | **0 / 26901**(claim+formula 全无) | 结构化条件填充 | 严格推理防幻觉(T2)无依据 |
| 稀疏推理边 `depends_on`/`contrasts_with`/`prerequisite_of` | **731 / 549 / 68** | 显著提升 | 深推理「权衡/前置/矛盾」链无边可走 |
| `about` 占比 | 27689 = 全库 60% | **保留**(schema 设计的结构骨架) | —(杠杆是抬推理边,不是压 about) |
| claim 原子性 | **~36% 疑似复合** | 拆原子命题 | `[k]` 指向多命题→grounding 模糊 |

**关键事实**:这些全是抽取时属性,改 prompt 对现存 36k **零影响**——只有重新抽取才生效。故 SA 拆为:SA-1(对齐代码,本 spec)+ SA-2(小切片 A/B 标定,本 spec)+ SA-3(重抽底库,deferred、effect-driven)。

## 2. Schema 已锁定的约束(不在本设计重议)

- `validity_scope` 结构(schema 109-114):`{region: [str], assumptions: [str], approximation: str, range: str}`,全子字段可选;仅 claim/formula。
- 12 类边为锁定词表;推理边 source/target **已放宽**(`derived_from`: claim/formula→claim/formula;`contrasts_with`/`depends_on`: claim/formula/concept 互连;`prerequisite_of`: concept/claim)。
- 抽取指令(schema 204-214):原子性=拆连接词/条件;validity_scope=结构化非散文;reasoning_edges=显式猎稀疏边、claim&formula 间连、**推理边召回是第一质量杠杆**;base_quality_filter=base 丢 meta、personal 不过滤;evidence_required。

## 3. 关键设计决策(brainstorm 已定)

1. **原子性**:激进——「拆分并连边」。`A because/therefore B` → 两原子 claim + typed 推理边(local_id 连)。**护栏**:仅当每片独立可判真伪才拆;单一命题再长不拆。
2. **标定基线**:A/B 同切片(old vs new prompt 跑同一批 window)。
3. **切片规模**:小切片(1-2 章)。
4. **prompt 架构**:方案 A(单一增强主 prompt)先行;方案 C(定向「边 gleaning」二遍)作为**标定-gated 后备**——A/B 显示稀疏边仍不足时再加。

## 4. SA-1 实现(组件与数据流)

数据流(现状,已核实):
`extract_window`(产 `Node`)→ `canonicalize`(合并)→ `_run_extraction`(Node→obj dict,装 payload)→ `store_kg`(sqlite_repository.py:1979)→ `knowledge_objects.payload`(TEXT JSON)。

**改动点**:

1. **`extract.py:_prompt` + `_KG_SCHEMA_HINT`(核心)** — 保留现有 Concept 选择性 / claim-meta 过滤 / Formula·Procedure 穷尽 / steps 数组;新增 §3 的 4 条指令:
   - 原子 claim + 连边(带护栏);
   - `validity_scope` 结构化抽取(仅在源文陈述条件时填,**不编造、不写散文、不另起悬空 claim**);
   - 推理边在 Claim/Formula/Concept 间放宽连接 + **显式猎** `depends_on`/`contrasts_with`/`prerequisite_of`;
   - **`base_filter: bool` 参数**:为 True 时丢教学旁白/习题提示/工具UI/导航(base 抽取用);False 时不过滤(personal)。
   - `_KG_SCHEMA_HINT` 的 node 形状增可选 `validity_scope`。
2. **`kg/models.py:Node`** — 加 `validity_scope: Dict[str, Any] = {}`(或 `ValidityScope` 子模型 region[]/assumptions[]/approximation/range);仅 claim/formula 填充,其余空。
3. **`extract.py:extract_window`(218-228)** — claim/formula 节点解析 `it.get("validity_scope")` → `Node.validity_scope`(类型/形状校验,坏值降级为空 dict)。
4. **`_run_extraction` 的 Node→payload 装配** — claim/formula 的 payload 增 `validity_scope`(非空才写,保持 payload 干净)。`store_kg` 不动(已 `json.dumps(payload)`)。**无 DB 迁移**(payload 是 TEXT JSON)。
5. **`kg/canonicalize.py`** — 合并重复 claim/formula 时**保留/并集** `validity_scope`(避免合并丢条件)。
6. **`base_filter` 传参链** — `extract_window` 增 `base_filter` 形参(默认 False),由调用方按 notebook tier 传入(base notebook → True)。

**边广播零代码改**:`extract_window`(246-255)只校验 `type in EDGE_TYPES`、不校验 source/target 类型对;`by_local` 已支持任意 local_id 连边。放宽纯属 prompt。

## 5. SA-2 A/B 标定(脚本,不入产品代码)

- **切片来源**:1-2 章分析电路内容——从 prod storage 里 `nb-012fb94249` 某 source 的已解析 markdown **只读复制**一章到 scratch(无需新上传;不写回 prod);或用户提供一小篇 `.md`。按现有 `windowing.py` 切窗。
- **隔离**:临时 scratch sqlite DB(`tempfile`);**绝不碰 prod 底库**(只读取其 source md)。就地读 root `.env` 进进程(**不拷 .env 文件**),真 LLM + embedder。
- **流程**:解析切片→elements→`windowing` 切出同一批 window;每个 window 分别用**两版 prompt 文本**各调一次 LLM,用**同一节点/边解析逻辑**统计。
  - **old 版** = 脚本内内联的「改动前 `_prompt` 文本」常量(从 git 基线复制,带 `base_filter` 等价行为);
  - **new 版** = `import` 改后的 `extract._prompt(..., base_filter=True)`。
  - 两臂走同一 `client.chat_json` + `safe_json` + Node/Edge 解析,确保差异只来自 prompt 本身。
- **指标(每臂)**:逐关系边数;**稀疏边密度**(`depends_on+contrasts_with+prerequisite_of` / 千输入 token);claim **复合率**(沿用 36% 那个启发式)+ 均命题数;**validity_scope 填充率**(claim/formula 非空占比);节点/边总数;**每文档 token 成本**(prompt+completion)。
- **门槛(默认,可在审稿时调)**:new vs old 同切片——
  - 稀疏边密度 **↑,目标 ≥ 1.5×**;
  - claim 复合率 **↓,目标 < 15%**(从 ~36%);
  - `validity_scope` 填充 **> 0** 且 10 例人工抽检条件合理(无编造);
  - token 成本/文档 **≤ ~1.8× old**(原子+边+scope 必然更贵,设帽以约束 100k 外推)。
- **判定**:全过 ⇒ new prompt 达标,SA-1 可作为底库重抽(SA-3)的抽取器候选;任一不过 ⇒ 迭代 prompt,或启用后备 C(边 gleaning),重测。

## 6. 测试

确定性单测(无 LLM,node/py 既有风格):
- stub LLM JSON(claim 带 `validity_scope`)→ `extract_window` → Node.validity_scope → payload 透传正确;
- 原子拆分 fixture:多 claim 节点 + local_id 间 `depends_on`/`supports` 边能 resolve 成 DB 边;
- `base_filter` 开/关:含 meta 句的 fixture 在 True 时被丢、False 时保留;
- **向后兼容**:不含 `validity_scope` 的旧 JSON 仍正常解析(字段默认空);
- `canonicalize` 合并两个同名 claim(一个带 scope)→ scope 保留。
- 跑 `scripts/check.sh`(py_compile + smoke + node + tsc)。

## 7. 范围边界(YAGNI)

**范围外**:
- `confidence` 公共字段(归 T3 边可信);
- 重抽 36k 底库(SA-3,deferred,effect-driven);
- 方案 C 边 gleaning 二遍(标定-gated 后备,非默认实现);
- 任何 DB schema 迁移(payload JSON 不需要);
- 检索/答案/前端改动(SA 纯抽取侧)。

**不变量(必须守)**:[0,1]/tau 标定、dual-index best-of——SA 不碰检索路径,天然不受影响,但 calibration 脚本不得污染 prod DB。

## 8. 成功标准

- SA-1:`check.sh` 绿;新 prompt 在确定性单测下正确透传 validity_scope、原子拆分连边、base_filter 生效;向后兼容。
- SA-2:A/B 报告产出上述指标对比,门槛判定明确;若过,记录「new prompt 为底库重抽候选」。
- 不改 schema(v1.0.0 locked),不动 prod 底库,不拷 .env。
