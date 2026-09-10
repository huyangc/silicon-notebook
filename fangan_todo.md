# silicon-notebook 待办（fangan_todo.md）

更新日期：2026-09-08
对照：`silicon_notebook_fangan.md`（产品方案）。已完成项见 `fangan_done.md`；本文件只列**尚未做完**的部分。

> 规则：完成某项后，从本文件移除并补进 `fangan_done.md`（见 `AGENTS.md` 的「Documentation Ownership」）。
>
> 本版是 2026-09-07 按当前代码逐条对账后的重写。上一版（2026-05-29）里以下条目已经落地，
> 已从本文件移除：关系图改为 id 级 `knowledge_relations` + 力导图可视化；参考文献/索引等
> backmatter 小节在切窗阶段跳过（`kg/filters.py`）；合并候选批量单事务写入；rep-pair 配对改
> hnswlib ANN；分窗改贪心打包、并发按模型服务 `max_concurrency`；抽取回传 `ev:<int>` 做
> element-id 证据锚定；`knowledge_embeddings` payload 级向量进 `score_knowledge`；账号系统、
> 会话管理、分享链接、群组共享；Knowhow 版本历史/diff/回退；逐步推理大纲协同
> （PR #407/#411/#418）；在途提问接回（PR #661/#662/#664/#665）。Article Studio 与
> schema profile 抽取已退役，相关条目一并删除。

## 状态速览

- 产品主线已上线：KG-native 抽取 + 混合检索 + Ask（chunk / reasoning，含意图澄清与大纲协同）
  + Deep Report + Memory / Knowhow + MCP + 群组分享 + 部署插件扩展点。
- 2026-08-29 起的「生产热路径修复计划」（批 0 / 1 / 2 / 3·W1–W4）全部实施项已合入（至 PR #676）。
  剩余为用户侧生产动作与各设计稿登记的残余债。
- 本文件按「近期可动手」→「产品功能」→「长期方向」排列。可直接实现的项已排进
  `docs/superpowers/plans/2026-09-07-todo-closeout.md`（PR-1～PR-5），需拍板的项留在本文件。

---

## 一、生产热路径修复：收官后剩余

### 用户侧动作（需要生产环境，仓库内无法代做）

> T-0 生产只读测量、PostgreSQL 调参 runbook、批 1 索引 `--apply` 三项已由用户在生产环境完成
> （2026-09-07 告知）。SR-1（element 搜索腿 OR→UNION）已被差分测量证伪撤销，不再重试。

- [ ] **backfill-images 放量前**：原图缺口盘点只命中 12.3%，要么找回其它 MinerU output 根，
      要么接受部分回填；先挑 1–2 个来源用 `--source-id` 试点，在问答里亲眼看到引用带图再放量。

### 登记的残余债（有真实需求再做；真源为各设计稿的「残余债 / 登记」节）

- [ ] **多 worker 部署下进程内互斥失效**（W1 #10 / W2 #1）：正解是把 relink / unifiedkg /
      conflictresolve 的 claim 提升为 durable 行，而不是给删除加锁。当前沿用「生产单 worker」契约。
- [ ] **W1**：`_delete_indexing_pipeline_stages_in_batches` 级联未分批；
      `agent_access_tokens.default_notebook_id ON DELETE CASCADE` 语义登记不改；
      守卫若在 `scripts/` 扫出新的可见性谓词站点，逐条处置。
- [ ] **W2**：双代窗口时长无硬上界告警；`rebuild_canonical_relations` / `mention_bridge` 若仍是
      DELETE+INSERT 需并入代际协议；「翻转后 finish 前崩溃 ⇒ 下次全量重算 + 状态计数陈旧」
      是已知格。
- [ ] **旧索引退役债**：`idx_chunks_source`、`idx_clusters_nb_canonical`、0043 / 0007 原形索引
      均已被新索引前缀覆盖，登记为可回退的写放大冗余，尚未下线。
- [ ] **W3**：大库切换索引管线目前只是按活跃对象数阈值显式禁用（切回内建豁免）；
      「保存索引管线」发布事务的真正重构排到有真实需求时。
- [ ] **W4**：未回填老库（`source_index_backfilled=0`）的 legacy evidence 分支单语句可撞 30s，
      前置是既有离线 backfill；来源页签搜索改 UNION 后 `paper_meta_for_sources` 水合腿未跟进同口径。
- [ ] **PG 打开路径计数缓存的刻意延后项**：checkup H4/H5 缺向量 anti-join COUNT 仍是 30s TTL memo；
      SQLite 侧 pending memo 的全局 epoch 有跨库误伤弱点。

---

## 二、产品功能待办

### 架构与清理

- [ ] **前端 `page.tsx` 继续减负（阶段 5 三片已交付后的后续）**：三片合入后 `frontend/app/page.tsx`
      约 8000 行，剩余大块为问答区、知识库浏览器与工作区壳；按同一纪律（零行为变化、守卫重指向、
      组件测试）再分片，有需要时立项。

### Ask / Deep Report

- [ ] **问答方法归一（路线，共四步，第一步已实施，第二步 ✅ 已合入 PR #693）**：① KG 可选的
      reasoning——`docs/superpowers/specs/2026-09-07-reasoning-kg-optional-design_zh.md`
      （T1–T5），原文段落检索一等动作 `search_chunks` + 无图首轮播种、按图存在收缩动作
      空间、早退收窄为零源、注册表与前端闸放行，已落地；② 无图首轮并入关键词臂 + 首轮后
      模型判定直接作答的成本契约（规格 `docs/superpowers/specs/2026-09-07-reasoning-chunk-parity-keyword-arm-design_zh.md`），
      ✅ 已合入 PR #693；③ 自动模式灰度——前提有二：其一，reasoning 直接作答的调用数 =
      chunk + 1 次 reflect，自动模式下 4 比 3；其二，**界面路径的 chunk 向量检索已修复**
      （`docs/superpowers/specs/2026-09-07-scoped-chunk-vector-lane-design_zh.md`：冻结来源
      范围曾让所有未建 scale 索引的库在 UI 路径上只走 FTS 词法候选，灰度前必须先把这条
      基线拉回来，否则两臂对比测的是一个坏掉的对照组）；④ 退役 chunk 流水线。③–④ 各自
      待立规格。
      v1（固定「直答」档位）与 v2（先合成、不足再查）均已撤回：用户裁决不设固定直答选项、
      问题理解与子问题检索不能省、检索效果优先于省一次调用；v2 的先合成本可把首轮后的
      reflect 并进合成再省一次调用，但代价是拆 `run()` 的重构与判定质量的不确定，按效果
      优先撤回。放量前两条待办：
      (a) 无图首轮播种目前逐子查询串行调用 `search_chunks`，改用多查询合并召回
      （`RetrievalService.retrieve_chunk_candidates_multi`）之前，需先在大库上实测
      并发度 N=8 时的耗时；(b) 该规格「验收」一节给出的人工抽问（点名子部件 /
      周期性 / 方向三句式 + 一句对比题，无图库与有图库各跑一遍）未做。
- [ ] **问答纠偏规则 12（限定词保真）人工 A/B**：仓库无问答质量评测台，放量前用「点名子部件 /
      周期性 / 方向」三句式各问一次验证；ledger 喂摘要未做。
- [ ] **Prompt 三层化后的 per-notebook 定制与 self-evo**：接缝只有 `fragment_text()`；L1 片段分
      两类（A 类离线 GEPA + 人审，B 类只改示例槽位），尚未拍板开放。
- [ ] **Agentic Memory 注入开闸与 A/B**：P1–P4 已合入，注入默认关闭，开闸是独立决定。
- [ ] **reflect v2 开闸前待办**（特性 T1–T4 见 `fangan_done.md` §32，2026-09-08/09 的三条
      鲁棒性合同见 §32.1；总闸 `REASONING_REFLECT_V2_ENABLED` 默认关，设计真源
      `docs/superpowers/specs/2026-09-07-retrieval-reflect-final-design_zh.md`）：
      (a) **T0 脚本已交付，基线报告待跑**——`scripts/export_reasoning_traces.py`（只读导出
      → 闭集投影 JSONL）、`scripts/analyze_reasoning_trace.py`（零 DB / 零模型聚合）、
      `scripts/reflect_shadow_rig.py`（一次性测试库上的影子 run，带 `--dry-run`）与题集
      `backend/app/eval/reflect_t0/questions.json` 已随
      `docs/superpowers/specs/2026-09-08-reflect-t0-trace-analysis-design_zh.md` 实施；
      **仍未做的是拿它们跑出首份基线与对照报告**（要真实模型与网络，不进 CI），以及
      §9.2 的真实模型 A/B 通道。仓库仍没有问答质量评测台，所以本期只宣称结构性与可
      观测性交付，**开闸必须在此之后单独决定**；A/B 与 T0 使用同一题集。
      A/B 的 rig 通道（T-AB2，设计真源
      `docs/superpowers/specs/2026-09-09-reflect-ab-design_zh.md`）已交付：
      `scripts/reflect_shadow_rig.py ab` 在 `seed` 建出来的一次性测试库上**进程内**跑
      完整 Ask（意图契约 → 检索 → 合成 → 引用绑定），同题同档的 legacy/v2 背靠背、
      臂序随机，逐 run 出 `backend/app/eval/reflect_ab.py` 的闭集投影（`ab-runs.jsonl`；
      答案正文、引用与冻结契约只落 `.local`）。用法：

      ```bash
      python scripts/reflect_shadow_rig.py --dry-run --limit 2 --round 1 \
          --database-url postgresql://127.0.0.1:5432/silicon_notebook_t0_test \
          --source-db-url postgresql://127.0.0.1:5432/<主库> \
          --out-dir .local/ab ab
      ```

      `--database-url` 必填且必须以 `_test` 结尾（这条路会往库里写 conversation 与
      answer 行）；`--source-db-url` 是主库连接，只用于跑前跑后的「主库零接触」断言，
      不能与 `--database-url` 指同一个库。`ab` 不接受 `--no-intent`（两臂必须引用同一条
      冻结契约）。`--concurrency > 1` 时成本三键（`prompt_tokens`/`completion_tokens`/
      `model_calls`）强制写成 unknown——它们靠 LLM 日志的时间窗切片归因，并发下切不干净。
      **仍未做的是 T-AB1 的 gold 正文（34 题的 `gold_facts`、B 侧 `gold_sources`）、
      T-AB3 的 `ab-judge`/`ab-report`，以及 T-AB4 的首份报告。**

      T-AB2 的**已知限制**（codex 评审 P3，知情接受，不改代码；首份报告要照抄）：

      * **成本三键只覆盖 chat 通道**：`app/core/llm.py` 只在唯一一处 chat 调用点写
        LLM 交互日志（`kind="chat"`），embedding 调用根本不进日志，所以这三个键从来
        不含 embed 成本——报告里不得写成「含 embed」。
      * **跨午夜的 run 会少算成本**：`EventLogger._maybe_archive_prev_day` 会把前一天的
        日志 gzip，而 rig 只 glob 明文 `llm-*.jsonl`。跨午夜那一个 run 因此**少算**而
        不是记 unknown。跑批尽量不跨零点，或事后按 `latency_ms_total` 复核那一行。
      * **`invalid_tool_calls` 是臂不对称量**：它按子串吃 T0 的 `skip_reasons`，而
        `unavailable_action:*` / `invalid_assessment:*` 这两类原因码是 v2 独有的。首份
        报告必须写明这一列不能直接做臂间差值。
      * **`invalid_assessment:*` 的语义在 2026-09-09（T-BF7 及其评审修复）前后变过两次，
        旧数据重投影也补不回来，A/B 与 T0 取样不得跨越这个日期**：(1) T-BF7 之前它一律
        表示「整轮作废、工具一次都没打出去」，之后逐方面被拒的那一族表示「工具照常执行了，
        只是一条自评没被采纳」；(2) T-BF7 评审修复之前逐方面拒绝是**一个方面一条** skip 步，
        之后是**一轮一条**（条数在那条步 detail 的 `count` 里）。因此 `skip_reasons` 里这
        几项在三段数据上分别数的是「作废的轮」「被拒的方面」「被拒过的轮」，量纲都不同。
        `assessment_rejections` 按 `count` 累加，在后两段之间可比（旧行无 `count` 按 1 计
        恰好等价），但在 T-BF7 之前这一列根本不存在。重投影只能重算，改不了轨迹里当初
        记了几条 skip 步，所以跨段的对照只能**分批**做，不能靠归一。
      * **`model_contract` 短码缺 `prompt_version`**：仓库里没有这个常量，短码由
        provider/model/fingerprint/top_p/thinking_mode 压成。它是「跨批次混用当场可见」
        的辅助信号、不是判据，等真有 prompt 版本号了再补。
      * **`early_stop` 与判分模型/人工键一起延后到 T-AB3**：键集已经闭上，值恒 `None`。
      * **纯逻辑仍住在 rig 里**：`_ab_round_units` / `_ab_call_estimate` /
        `ab_contract_digest` 零 I/O、零模型，可以移进
        `backend/app/eval/reflect_ab.py`（`_ab_corpus_signature` 自 codex #703 R1 起
        要查测试库的 sources/chunks/source_elements/unified_kg_state 才能指纹语料身份，
        不再是纯逻辑，留在 rig）；`_resolve_item_scope` 每个单元重查一次
        `sources`，而 `fact["source_rows"]` 里已经有那份表。两条都是纯搬运，与
        T-AB3 的改动一起做更省一轮评审。
      (b) 报告侧 admitted 复核的簇折叠表仍是保守口径——同下条独立待办。
      (c) 灰名单字符串「本笔记本尚未构建知识图谱…」的界面词表违规——同下下条独立待办。
      (d) **`reasoning_retrieval.py` 的「纳入 N 个同社区实体」上屏文案含界面词表的「社区」**：
      它是 f-string 而不是字面量，而轨迹摘要守卫（`scripts/check_ui_vocabulary.py` 第三条通道）
      按设计只扫字面量，所以扫不到它。改文案与放宽守卫是两件事，都要独立立项：守卫改成能读
      f-string 会把大量拼接文案一起拉进扫描面。
      (e) **v2 每轮 prompt 的成本尚未计入 A/B**：服务端状态块与证据卡内容有重叠，方面块每轮
      重新渲染一遍（量级约 20KB）。这不是正确性问题（各块都有自己的具名边界），但放量前的
      A/B 必须把它算进 token 成本，否则测的是「质量提升」而不是「质量/成本比」。§32.1 又加了
      两项要一起算进去的成本：`REASONING_MAX_TOKENS` 把输出预算从 8192 抬到 16384（且打满时
      同轮再翻倍重试一次），以及「收尾必须自评」每次退回多花一轮反思调用 + 一步预算。
      (g) **§32.1 的三条合同只有本机 16 个 run 的证据**：收尾自评的追问命中率（追问之后模型
      真的补上自评的比例）、集合完整性键真的让目录方面拿到 supported 的比例、单轮失败降级
      之后 run 完成的比例，这三个数都要在 A/B 同一题集上量出来——追问不命中就只是白花一轮，
      而现在没有任何数据能证明它命中。
      (h) **集合完整性键没有 epoch**：「先 complete、后同 run 内资料变化」这个窗口是知情接受的
      （最危险的一半已被 `conflict` 从不签发键挡住）。要不要加版本号，等实测证据说它伤人再定；
      加的话要在续跑键、方面账、展示三处各带一个，属于独立立项。
      (f) **设计稿 §11 四条延后路线的重新进入条件**（本期明确不做，不得顺手实现）：
      `read_evidence` 要先由逐题分析证实「找到证据但摘要不足、反复搜索」仍是主要失败类型；
      多子查询批量要先证实串行模型往返是主要延迟来源，且规格必须按实际动作数扣预算、
      确定性合并、保留逐动作归因并处理局部失败/取消；跨库原文按既有待办单独解决候选配额、
      scope、引用与资产解析，不得用移除 notebook-local 过滤冒充联邦化；终态独立 critic
      必须先用本期数据证明收益，不能因为循环里已有 assessment 就再加一次固定模型评审。
      (i) **PR-1 公共基线修复已落地**（计划真源
      `docs/superpowers/specs/2026-09-09-reflect-v2-baseline-fixes_zh.md`），四项：
      1. 枚举侧（T-BF1/2/3）：清单规模守卫按集合泛化、`truncated_reason` 新增
         `oversize_sample`、v2 能力投影补齐「计数远大于本轮额度就别翻页」半句；
      2. 参数与原因码（T-BF4/T-BF5）：`exact_lookup` 形状判据前移到解析层并写进参数说明；
         v2 的 `enumerate` 摘掉 `source_id`，范围不符改报可自修的原因码；
      3. 收尾计时归位（T-BF6）：收尾重排抽成 `_closing_rerank` 并记新 `rerank` 步；
      4. 自评解耦（T-BF7）：assessment 按方面拒绝、不再吞掉同轮的检索动作，投影新增
         `assessment_rejections`。

      **已知限制（知情接受，首份报告要照抄）**：

      * **T-BF5 的修法建立在一条尚未证实的生产假设上**：生产那 12 条
        `enumeration_rejected` 被当作「模型猜 `source_id`」处理（本地无复现样本）。待生产
        raw `detail.error` 字符串确认；若实为 memory 合成源的 `not enumerable`，修法要改
        成解析器侧过滤，届时这一批数据的原因码分布不能与修复后混用。
      * **规模守卫只对 v2 生效**（拍板 3，legacy 逐字节不变）：所以枚举侧的臂间差里
        **含守卫本身**，不能读成「v2 的模型更会收窄范围」。
      * **`trace_steps` 在 v2 臂恒 +1**：`rerank` 是 v2-only 的非动作步，而
        `reasoning_trace_stats` 的 `trace_steps = len(normalized)` 数的是全部轨迹步，
        `scripts/analyze_reasoning_trace.py` 的同名指标照单全收。首份 A/B 报告必须写明这一
        列的臂间差里有一个恒定 +1，或者把分析脚本改成只数动作步（本 PR 只登记，不改）。
      * **`invalid_assessment:*` 的语义在 2026-09-09 前后变过两次**：详见上面 T-AB2 已知
        限制那一条，PR-1 收尾时已复核仍然成立——A/B 与 T0 取样**不得跨越这个日期**。
      * **T-BF6 的守卫用 monkeypatch 打进程全局 `time.perf_counter`**（`rr.time` 就是
        stdlib，不是模块别名）：并发 lane 下曾一次性红过 3 条、随后 7 次全绿。**不要**把实现
        里的 `time.perf_counter()` 换成模块别名——`scripts/generate_repository_contract_fixtures.py`
        的 `fixed_perf` 正是靠全局 patch 才能得到确定性耗时，换别名会让 golden 漂移。若再
        复现，改用注入时钟（把取时函数做成 `_closing_rerank` 的可选形参）。
      (j) **PR-2「T0 测量 + `prefix_snapshot`」已落地**（计划真源
      `docs/superpowers/specs/2026-09-09-reflect-prefix-snapshot-plan_zh.md`，上游设计
      `docs/superpowers/specs/2026-09-09-reflect-prefix-cache-final-design_zh.md`）：
      `REASONING_REFLECT_OPTIMIZATION`（默认 `off`，本期只放开 `off`/`prefix_snapshot`）与
      正交的 `REASONING_REFLECT_MEASURE_CONTEXT`（默认 false）、S/C/K/D/T 布局、同源静态
      工具目录、单次调用观测（`call_wall_ms`/`status`/`attempts`/`response_chars`/`usage`）、
      reflect 步的稀疏测量键与闭集投影新增九列、rig 的 `EVENT_LOG_DIR` 隔离 / `run_wall_ms` /
      `(policy, optimization)` 二维臂 / per-call 表。**仍未做的是拿它跑对照实验**：真实模型
      的 E1/E2/E3 与首份报告不进 CI，`prefix_snapshot` 的实际收益**一个数都还没量**（文档只
      宣称结构与可观测性交付）；**开闸仍是此之后的独立决定**。

      PR-2 的**已知限制**（知情接受，不改代码；首份报告要照抄）：

      * **`search` 子命令的投影 `optimization` 恒 unknown**：第二维只接在 `ab` 臂上
        （`_settings_by_arm` 逐臂设 `REASONING_REFLECT_OPTIMIZATION` 并核对实际运行值），
        而 `project_search_run` 的 `rig_tags` 不带这一维。想让 `search` 也分格，要在 domain
        侧单独加一格，属于独立立项。
      * **`AskCancelled` 的 run 不产投影行**：`search` 与 `ab` 两条路上它都是**整批收摊的
        信号**并按设计原样上抛，所以「取消的 run 也有 `run_wall_ms`」这条只对「取消事件已
        置位、run 抛的却是非 `AskCancelled` 异常」那一路成立（走失败行、带墙钟）。真要给
        取消的 run 落一行，需要先决定收摊语义。
      * **`ab` 的 `--only-policy` 与 `--arms` 互斥，`--arms` 一次只收一对臂**（三臂批次
        与空写法都在预检响亮拒绝；`paired` 的门槛因此是常量 2）：既有一维用法逐字不变。要把
        `--only-policy` 也二维化（如 `--only-arm`）、或让一批跑多对臂，都是一次 CLI 决定，没做。
      * **`EVENT_LOG_DIR` 与 `LLM_LOG_PATH` 目录不对齐会打一行启动告警**：rig 按计划把
        事件落 `<out-dir>/events`、llm 日志落 `<out-dir>/llm`，`repository_runtime` 的对齐
        检查因此每次真跑报一行。对 rig 无功能影响（两串日志各按自己的 glob 读），消掉它要
        把两者并到同一目录，是对计划字面的偏离。
      * **`prefix_snapshot` 相对 `off` 每轮 +1–3KB 输入**（缓存未命中时）：目录恒为超集，
        所以 `off` 的 system 段随额度收缩变短、这条臂不会。按设计接受，见部署文档那两条。
      * **PR-2 收尾时 `prefix_delta` / `prefix_delta_lean` 都由启动期校验器响亮拒绝**
        ——**已被 PR-3 部分解除**（见下面 (k)）：`prefix_delta` 现在能起来并有自己的布局，
        `REASONING_REFLECT_RECENT_OBSERVATIONS` 在它上面已经是「重建 K 时保留几条近期详细
        观察」的当前行为（不再是预告）；只剩 `prefix_delta_lean` 仍被拒绝，等 PR-4。PR-5 是
        生产策略的实验标记接缝（E1），仍刻意不做。
      * **A/B 采用与开闸的拍板点仍在用户手上**（设计 §13）：默认 `off`，选定策略后逐步
        验证、由用户决定开启；任一质量或稳定性回归立即回 `off`，不做数据迁移。E1 的机制
        结果只能支持「稳定前缀的时间收益」，**不得**报告成估算命中率。

      (k) **PR-3「`prefix_delta` 增量上下文」已落地**（计划真源
      `docs/superpowers/specs/2026-09-10-reflect-prefix-delta-plan_zh.md`，上游设计同 (j)）：
      两个新配置（`REASONING_REFLECT_DELTA_CARDS_BY_EFFORT` / `_COMPACTION_TARGET_RATIO`，
      只在这条臂下被消费、每个字段恰一个读点）、`REFLECT_OPTIMIZATION_IMPLEMENTED` 放开第三格、
      run 级渲染缓存 `ReflectDeltaState`（冻结卡表 / 当前可见集 / 两笔累计账 / 重建迟滞位 /
      补充卡轮转游标）、`_reflect_delta_context` 七步（`run()` 零改动）、历史折算纯函数
      `fold_observation_counts`、增量块与补充卡（同 key 逐字不变 + 版本标记）、S 的四句 delta
      读法规则、三个新 detail 键与投影新增两列、rig 第二维第四格。**仍未做的是拿它跑对照
      实验**：真实模型的 E1/E2/E3 与首份报告不进 CI，`prefix_delta` 的实际收益**一个数都
      还没量**（文档只宣称结构与可观测性交付，字节账两侧哪一侧更大取决于题型与轮数）；
      **开闸仍是此之后的独立决定**，默认 `off` 不变。

      PR-3 的**已知限制**（知情接受，不改代码；首份报告要照抄）：

      * **回退不可逆**（拍板 Q4）：一个 run 里一旦装不下最小有效新证据，剩余轮全部走
        `prefix_snapshot` 的有界证据选择，不会再回到增量装配。统计上必须按 `context_fallback`
        分开看，**不能**把这样的 run 混算成「始终在用 D」的结果（设计 §13）。回退之后每一轮的 K 按
        「**档位证据池 − 保留 D 已占的证据半**」构造，并把保留 D 里已可见的键排除在 K 之外
        （`exclude_keys`，对 `build_evidence_block` 的**全部三档**生效——第三档的键序由它自己
        从池子里算，只滤前两档挡不住它，codex #707 R1 P2 已修；近期观察窗同理先减去保留 D 占
        的历史半），所以一条消息的证据合计仍然 ≤ 档位证据池；代价是保留 D 把池子吃得多时回退
        之后的 K 可以是空的。两笔累计账回退时**收缩成保留 D 那一半**，此后唯一的读者就是上面
        那两条减法。
      * **`search` 子命令的投影 `optimization` 仍恒 unknown**：同 (j) 那一条，第二维只接在
        `ab` 臂上，本期一格未动。
      * ~~**只带观察行、无卡片的 D 块仍按块头计进证据池**~~ **已修（codex #707 R1 P2）**：准入
        改成先算**完整待追加块**的两笔费用（块头 + 卡片节 → 证据池；观察行 + 已接受方面 note →
        历史池，常量与落账同一处 `_delta_pending_charges`），任一顶破 ⇒ 走重建。**留一条越界例外**：
        没有候选新卡、而块头/note 在重建之后仍装不下时那一块照发（观察行是本轮那次动作在上下文里
        的唯一记录），记账如实超出、下一轮必重建——这是唯一允许的越界形态。
      * **`prefix_delta_lean` 仍由启动期校验器响亮拒绝**：`REFLECT_OPTIMIZATION_PLANNED` 收窄
        成一格，PR-4 放开它（轻量 assessment 是**另一条**实验臂，不能把收益归到缓存上）。
        PR-5 是生产策略的实验标记接缝（E1），本期同样刻意不做。
      * **增量块的观察节不带 `HISTORY_NOTE`**：「目的是模型当时写下的判断、不是原文」这句
        免责由块头里「观察行的含义同上方观察账」一次性接过去，K 的观察账里那一份仍在。这是
        为省下每块上百字节的重复；两处**措辞同源**的要求由用例对账，不靠每块各付一份。
      * **`delta_blocks` 只在 reflect 步 detail 里，不出顶层投影列**：它不是累计量（重建会
        清空已发出的块），一个 run 一格装不下它。要按轮看增量块数就读那几步的 detail。
- [ ] **深度报告一侧的方面送达复核补上簇折叠表**：Ask 侧 `_answer_context` 已经把
      `knowledge_context` 的 `fold_sink`（同 canonical 簇被折叠掉的成员 → 代表）折进
      `admitted_evidence_keys`；报告侧 `_draft_section` 走 `knowledge_context_with_outline`，
      给它加 sink 参数会改端口签名，而 `test_report_outline_integration.py` /
      `test_report_engine_ports.py` 的测试替身逐字钉住了那个形状。补齐要连同那些替身一起
      同步。眼下报告侧是**保守口径**（可能多报「未送达」，不会漏报）。
- [ ] **无图披露步文案「构建知识图谱」→「整理知识图谱」**：`reasoning_retrieval.py` 那条
      `kg_unavailable` 披露步的 `summary` 违反界面词汇表，但它被 `docs/product-and-api*.md`
      逐字冻结（文档明写「含其中的半角逗号」）、并被 `tests/fixtures/repository_contract/
      ask_responses.json` 与 `test_reasoning_retrieval.py` 钉住。眼下登记在
      `scripts/check_ui_vocabulary.py::GRANDFATHERED_TRACE_SUMMARIES`（逐字全串例外，改一个字
      就重新违规）；改它要同改文案、两份文档、既有用例与黄金 fixture，是独立的一次改动。
- [ ] 自动模式对含「刚才 / 这个 / 那个」的订正句落 chunk+standard：登记为已知行为不修。

### 知识图谱

- [ ] **节点属性 attrs 形态未定**：`Node` 仍只有 name / section_path / evidence / mentions /
      steps / validity_scope；决定前不要再往节点加字段（`scripts/kg_strip_attrs.py` 头注释同此）。
      候选：Concept `aliases[]`/`kind`/`definition`、Claim `quantitative_values{}`/`polarity`、
      Formula `variables{}`/`role`。决策牵动抽取 prompt、`models.Node`、canonicalize、评测维度。
- [ ] **gold 人工策展**：`fangan/testcases_kg/` 仍在 `.gitignore`，未有策展后的权威 gold 入库。
- [ ] **跨文档概念合并的真模型质量验证**：Embedder 现只有 openai / dashscope 协议 + FakeEmbedder
      （本地 BGE 路线已随 model_registry 退役），真模型下灰区候选量未做正式 smoke。
- [ ] **推理分层**：边类型已有 supports / contrasts_with，无 extends、无 Level 0–4 分层、无
      Hypothesis 对象。
- [ ] schema 归纳只提议新类型，不对既有类型提议新字段。
- [ ] KG refine 自我修正只有总开关 `KG_REFINE_ENABLED`，无抽样 / 比率控制。

### 检索

- [ ] **「中文问句检索英文语料首轮空」的三条次因（此前未登记）**。主因——冻结来源范围
      关掉 chunk 向量通道——已修（`docs/superpowers/specs/2026-09-07-scoped-chunk-vector-lane-design_zh.md`）；
      排查过程中另外过了三条，逐条登记如下：
      - ~~相关度地板对跨语言查询过高，把语义命中滤掉~~：**已用离线对照集证伪**。同一库
        同一题，无范围时 134 命中照常返回，地板没有在跨语言查询上多滤掉任何东西；零命中
        完全由「向量臂根本没跑」造成。不要再把这条当成因。
      - **检索查询携带整段问题契约文本**：服务生成的已确认意图查询把完整问题契约保留给
        候选生成与语义 embedding（见 `docs/product-and-api_zh.md` 混合检索一节），只有
        KG 关键词/RRF 打分使用契约分隔符之前的检索方向。整段契约文本进 embedding 会把
        查询向量拉向「契约模板」而不是「这道题」，跨语言时尤其伤。待办：先量化契约前缀
        对查询向量的偏移（同一道题的裸问句 vs 带契约版本，比命中集与 top-k 相似度），
        再决定要不要在 embedding 一侧也只取分隔符之前的方向。
      - **高级界面确认合同后 `plan()` 整个不执行 → 关键词臂不跑**：正式 UI 路径永远带着
        已确认意图（`reviewed_queries` 非空），`ReasoningRetriever` 因此刻意跳过
        `self.plan()`（`backend/app/services/reasoning_retrieval.py` 约 3637 行的注释写明
        了这条纪律），而无图首轮的词法臂用的正是 `plan()` 里 `expand_query` 产出的高/低层
        关键词。结果：走高级界面的 run 只剩语义臂，词法臂在最需要它的跨语言场景缺席。
        与下方「词法臂关键词双语化」是同一处的两个问题（一个是不跑、一个是跑了但语言对
        不上），做的时候一起定。
- [ ] BM25 / FTS5 / tsvector 全文索引：已评估为低 ROI、基础设施级，暂缓。
- [ ] 结构化硬过滤：软加权已够用，硬过滤有清空结果风险，暂缓。
- [ ] **无图参考库的原文段落联邦检索（`search_chunks` 联邦化）**：逐步推理的原文
      段落通道复用 chunk 模式的检索原语，而那套原语是 active-only（索引与库读都
      是 notebook-local），所以挂载的参考库只能经知识图谱与元素/知识对象/来源清单
      参与，它的原文段落进不了这条通道。PR #690（KG 可选的逐步推理）刻意**未做**：
      联邦化要连带解决跨库候选池配额、来源范围天花板在参与集上的语义、以及引用与
      资产的跨库解析，是独立特性而不是这条通道的一个开关；本 PR 只把无图早退的
      放行判据改成说真话（原文那条理由只数当前笔记本的可见来源数
      `collection_map.active_sources`，枚举那条仍按参与集口径），避免拿一个通道
      够不着的库当放行理由。
- [ ] **逐步推理词法臂的关键词按语料语言双语化（给 `plan()` 传 `corpus_langs`）**：
      无图首轮的词法臂用的是 `plan()` 里 `expand_query` 产出的高/低层关键词，而
      `plan()` 调 `expand_query` 时**不传** `corpus_langs`，拿到的是 prompt 的
      zh/en 默认语言对；chunk 通用问答那条同源的臂是按语料语言给出的。两侧对齐
      需要把 `corpus_langs` 传进 `plan()`，但 `plan()` 是有图/无图两条 run 共用的
      同一个规划入口，传参会一并改到**有图 run 的规划 prompt**，越过本次「有图
      run 一字不动」的边界，故本次只把两侧文案与 docstring 改成说真话，传参登记
      在此。做的时候要连带决定：语料语言探测（`_lexical_corpus_langs`）在
      reasoning 侧的取数时机与失败语义，以及有图 run 规划输出漂移的回归证据。

### 解析

- [ ] 扫描件本地 OCR（MinerU 之外无 OCR 路径）；DOCX / PPTX 的 OMML 公式解析。

---

## 三、长期方向（方案 v0.4 / v1.0）

- [ ] **Review Mode**：review session、场景 checklist sign-off、reviewer 评论 / action items、
      导出 review 报告、project-level workspace。均无代码。
- [ ] **企业能力**：source 级 ACL（现只有 notebook 级 capability tier + 群组角色）、结构化审计
      日志（现只有 Knowhow 的 actor 标签投影）、SSO / VPC、Connectors（Confluence / SharePoint /
      Drive / Jira / Git / Slack）、多 notebook 全局搜索。多用户 / 登录 / 管理员角色 / 分享链接 /
      群组已交付。
- [ ] **分享的 edit 权限层与近实时协作**：写权限仍 owner-only；无 presence / revision 轮询
      （管理员用户列表的「在线状态」是另一特性）。原 spec
      `docs/superpowers/specs/2026-06-04-users-sharing-cowork-design.md` 的 D2 / D3 决策已被
      账号系统取代，动手前须重写。
- [ ] **自动用户记忆**：`memory_mode` 固定 manual；Agentic Memory 已提供候选 / 巡固机制，是否
      开放自动写入待决。
- [ ] **插件化 X 系列剩余**：X6 / X7 等首个真实消费者；X10 后端热更用户明确要求「完全热更或不做」；
      niuma 插件 e2e 后的问题回流。

---

## 验证基线（每完成一项都要保持）

- `bash scripts/check.sh` 全绿（后端 pytest 全量 + 前端测试 + tsc + production build）；
  PostgreSQL 相关改动另跑 PG lane。
- 离线（无 LLM / embedding / MinerU）闭环不回退。
