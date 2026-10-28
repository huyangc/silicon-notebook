# T7 真模型验收记录（论文卡片 / find_sources，standard 档）

日期：2026-10-28
分支：`claude/notebook-paper-query-tools-29d755`，被测 HEAD `b09e94a6f`
计划：`2026-10-28-paper-cards-find-sources-plan_zh.md` T7；规格：`../specs/2026-10-28-paper-cards-find-sources-design_zh.md` §7.3、§8 第 3 层

## 结论

- 6 题全部跑完，每题一次，**全部走到 `answer_layout=paper_table`、渲染出论文表**。
- **表内数字人工核对：6 张表共 39 行、187 个数字（含指标名里的数字，如 `ImageNet 256`、`CIFAR-10`），0 处违规**——每个数字都能在它的 `[kN]` 所指元素原文里找到。
- 召回（强相关+相关，表格行或「其余相关论文（未展开）」清单中出现即算）：6 题全部 100%（42/42）。**表格行里没有误报**；误报全部在「未展开」清单里（来自 `related` 档，阈值尚未校准）。
- chat 模型调用合计 **86 次**（84 次逻辑调用 + 2 次传输层重试），上限 220；入库阶段 0 次。
- 发现 1 个确定性缺陷（`find_sources.terms` 超 6 个整次拒绝，prompt 未告知上限）与若干质量问题（基线数值在表里不标出处行等），见下文。按要求未改代码。

## 环境与做法

- 模型：本机 deepseek-flash（`general` 服务，模型 id `deepseek-flash`）；嵌入 `text-embedding-v4`（dashscope）；重排 `qwen3-rerank`。
- 数据库：一次性 SQLite（`.local/t7_acceptance.db`），运行后已删除。
- 语料：`backend/tests/fixtures/paper_topics/papers.json` 的 40 篇合成论文，每篇转成 Markdown（标题、作者/会议行、Abstract、Method、Results 表、Conclusion），
  以 `doc_type=academic_paper` 走 `upload_sources` 同步 `process_source`（真实解析 → 元素 → 分块 → 嵌入）。结果：40 篇全部 `extracted`，
  元素 400 个（含 40 个 table 元素），分块 200 个、分块向量 200 个；嵌入调用 80 次 / 600 条。
- 入库期间**拦截了全部 chat 调用**（摘要、论文元数据、笔记本元数据走各自的 best-effort 回退，共拦下 120 次逻辑调用），
  因为用户批准的消耗只覆盖 6 个问题。随后按测试夹具写入论文元数据（`paper_title/venue/pub_year/authors/keywords`，`model="fixture"`），
  与生产「上传即抽元数据」后的状态等价。来源摘要因此是回退文案（「10 parsed text element(s). …」）。
- 问答：进程内 `repository.ask(nb, AskRequest(mode="reasoning", retrieval_effort="standard"))`，不带客户端确认的 intent
  （直接兼容路径，走 `plan()`；界面路径会多一次 `/ask/intent` 调用）。题面：
  「请列出这个笔记本中所有与{labels.json 的 query}相关的论文，包括主要技术指标和关键创新点。」
- 关闭了与验收无关的后台学习任务（`AGENT_PROFILE_ENABLED`、`USER_SEARCH_PROFILE_ENABLED`、`RETRIEVAL_EXPERIENCE_ENABLED`=false）、
  `KG_AUTO_EXTRACT=false`、`LLM_CACHE_ENABLED=false`、`OPENAI_COMPAT_MAX_RETRIES=2`、不加载插件。
- 计数：在 `app.core.llm._RequestCount.record`（每个真正发出的 provider 请求）处计数并设 220 硬上限；
  另在 `ScheduledJsonChatClient.chat_json` 按工作负载计逻辑调用。与 llm 日志核对一致（84 条 ok、attempts 合计 86）。
- 配置副本的两处补齐（只改 worktree 的一次性副本，已删除）：主 checkout 的 `.local/model-services.toml` 在当前代码上**启动即失败**——
  ① `general` 缺 `context_window_tokens` / `max_output_tokens`（当前代码强制要求）；② 缺 `report_context_compress` 绑定。
  验收副本填了 `context_window_tokens=131072`、`max_output_tokens=16384`（16384 与代码里 `answer_max_tokens` / `reasoning_max_tokens`
  的默认值一致，本次提示词远小于窗口，不会触发容量判断；**这不是核实过的服务端限值**，主配置的真实值需用户按服务实际限制填写），
  并把 `report_context_compress` 绑到 `general`。
- 原始产物（每题最终回答 markdown、响应 JSON、逐格核对清单、汇总 JSON、harness 脚本）在会话 scratchpad 的 `t7/` 目录，不入库。

## 每题指标

| 主题 | 墙钟 | chat 请求 | reflect 轮数 | 动作顺序 | find_sources（强/相关/列出；capped；lexical_complete） | 卡片 生成/就绪/失败/推迟 | 抽取 | 版式 | 表格行 | 召回 | 误报（均在未展开清单） |
|---|---|---|---|---|---|---|---|---|---|---|---|
| diffusion | 68.4 s | 16 | 5 | find → cards → enumerate → extract → answer | 6/3/9；否；是 | 6/0/0/0 | 1 篇 4 字段，得 3 值 | paper_table | 7 | 7/7 | det-04、rag-01 |
| molgraph | 57.6 s | 16 | 6 | find(拒) → enumerate → find → cards → find → answer | 5/1/6；6/1/7；否；是 | 6/0/0/0 | — | paper_table | 6 | 6/6 | det-05 |
| asr | 71.2 s | 16 | 6 | find → cards → extract → enumerate → find → answer | 6/6/12 ×2；否；是 | 5/0/1/0 | 1 篇 4 字段，得 3 值 | paper_table | 6 | 7/7 | rag-01、rag-02、rag-04、rag-05、x-ragmol |
| rag | 63.3 s | 13 | 4 | find → cards → enumerate → answer | 7/2/9；否；是 | 6/1/0/0 | — | paper_table | 7 | 8/8 | asr-01 |
| robotrl | 42.7 s | 13 | 5 | enumerate → find(拒) → find → cards → answer | 6/1/7；否；是 | 5/1/0/0 | — | paper_table | 6 | 7/7 | — |
| detection | 33.7 s | 12 | 3 | find → cards → answer | 7/8/15；否；是 | 6/1/0/0 | — | paper_table | 7 | 7/7 | asr-01、dif-03、mol-01…05、o-splat |

- 「抽取」次数即抽取模型调用数；「卡片 就绪」是同一笔记本里前一题已生成、本题直接命中缓存的卡片（跨题复用，例如 x-detdif、x-robdif、x-ragmol）。
- 每题的「extraction calls」：diffusion 1、asr 1、其余 0。卡片的 `generate_limit` 每题都是 6（standard 档），没有 `deferred`。
- 「(拒)」= 该轮 `find_sources` 被判参数不合法而跳过，见缺陷 1；被拒的那次没有占用 2 次额度。
- chat 请求按工作负载合计：`reasoning_agent` 35（plan + reflect）、`paper_metadata` 37（卡片生成 34 + 抽取 2 + 1 次失败卡片；卡片复用该绑定）、
  `evidence_refine` 6、`ask_answer` 6；另有 2 次传输层重试。嵌入查询 40 次、重排 18 次（不计入 220）。

## 人工核对：表内数字

方法：对每张表每个单元格（年份/会议列来自论文元数据、无标记，除外），取单元格里每段的 `[kN]` → 响应 `anchors[kN].element_id`
→ 库内元素原文，逐个数字对照（脚本先列出、再逐行人工阅读）。

| 主题 | 表格行 | 数字 | 违规 |
|---|---|---|---|
| diffusion | 7 | 38 | 0 |
| molgraph | 6 | 34 | 0 |
| asr | 6 | 35 | 0 |
| rag | 7 | 29 | 0 |
| robotrl | 6 | 25 | 0 |
| detection | 7 | 26 | 0 |
| 合计 | 39 | 187 | **0** |

（脚本的自动比对报了 11 处「未命中」，全部是正则把指标名里的数字拆错，如 `Tox21` 的 `1`、`MD17` 的 `7`、`AP50` 的 `0`、`BM25` 的 `5`；
人工核对这些指标名在原文里原样出现，不是违规。）

**违规清单：无。**

## 缺陷（含复现）

### 缺陷 1：`find_sources.terms` 超过 6 个时整次调用被拒，而 reflect prompt 没写这个上限

- 现象：6 题中有 2 题（molgraph、robotrl）第一次 `find_sources` 被跳过，轨迹为「跳过文档级检索(参数不合法)」，原因码 `find_sources_invalid`，
  各多花一轮 reflect（约 3 s + 1 次 chat 调用）。两次被拒的 payload 分别给了 7 个和 8 个 `terms`；被接受的调用都是 ≤6 个。
- 根因：`app/services/source_finder.py` 的规范化在 `len(terms) > FIND_SOURCES_MAX_TERMS`（=6）时抛 `FindSourcesError("at most 6 terms")`，
  `reasoning_retrieval.py` 把整次调用作废；而 `app/services/prompts.py` 里 `find_sources` 的用法段只说「terms 写字面词、缩写和别名」，没有数量上限，
  `reasoning_paper_tools` 读参数时只按 `_LIST_READ_MAX=64` 截断。
- 复现：reflect 返回
  `{"next_action":"find_sources","find_sources":{"query":"reinforcement learning for robot manipulation","terms":["reinforcement learning","robot manipulation","dexterous manipulation","offline reinforcement learning","hierarchical reinforcement learning","reward learning","visuomotor manipulation","sim-to-real grasping"],"kind":"papers","scope":"current_notebook"}}`
  → 轨迹出现 `skip`（`find_sources_invalid`），账本回写「find_sources 参数不合法:at most 6 terms」，下一轮模型改成 6 个才执行。
- 建议（按「可接受偏差做兼容、只在关键节点严格」）：prompt 写明「terms 最多 6 个」，执行器对超出的部分截断并在账本里注明，而不是整次拒绝。

### 质量问题 2：指标单元格把基线数值与本文方法并列，却不标所属的行

- 卡片指标每格取前 3 条，其中常有表里的**基线**数值，但单元格括号里只写数据集/设置，不写是哪一行（哪个模型）。
  读者会以为是本文方法的成绩。数字本身都在原文里，所以不算违规，但语义上有误导。6 张表里约 31 处，例如：
  - `Cascaded Denoising Diffusion…`：「FID 6.95（ImageNet 256, 1 sampling step）」——6.95 是 BigGAN-deep；「FID 10.94（…250 sampling steps）」是单阶段扩散基线。
  - `Message Passing Neural Networks…`：「QM9 U0 MAE 14.0meV（QM9 U0）」——14.0 是 SchNet，且优于本文的 19.4，读起来像本文成绩。
  - `End-to-End Object Detection with Transformers…`：「COCO box AP 42.0AP（COCO, 109 epochs）」——这是 Faster R-CNN + FPN 基线。
  - `Hierarchical Reinforcement Learning…`：「Avg steps 412steps（Assembly completion (%)）」——条件字段填成了另一列的列名。
  - 做得好的例子：`Real only`、`Hand-designed reward`、`MobileNetV2-SSDLite` 这类单元格把行名写进了条件。
- 建议：卡片提示词要求指标优先取本文方法行，基线必须在 `condition` 里写出行名（或渲染时加「基线：」前缀）；可加一条接地校验：
  `condition` 里应包含 anchor 原文中与该数值同一行的行名。

### 质量问题 3：数值与单位直接拼接

- 如「21.4AP」「79GPU days」「412steps」「26FPS」「2.40USD」。渲染时 value 与 unit 之间缺空格（`%`、`meV` 等无碍）。

### 质量问题 4：抽取字段列稀疏且与固定列重复

- diffusion、asr 各对 1 篇做了 `extract_from_sources`（4 个字段），表格因此多出 4 列，只有 1 行有值，其余全是「—」；
  其中 `key innovation`、`datasets`、`word error rate (WER) results` 与「关键创新点」「主要技术指标」两列语义重复。
  模型是为补一篇没拿到卡片的论文（diffusion：卡片额度用完的 dif-05；asr：卡片生成失败的 x-asrrag）才抽取的，用法合理，但呈现上可以改：
  只有少数行有值时，把抽取值并进固定列，或把与固定列同义的字段合并。

### 观察 5：模型正文自称「只预览了 31–33 篇」，与系统覆盖说明矛盾

- 5/6 题的综述开头写「本笔记本共有 40 篇来源，本次只预览了其中 32 篇，清单可能不完整」，
  而同一回答顶部的系统覆盖说明没有「可能还有更多」（所有 `find_sources` 都是 `semantic_recall_capped=false`、`lexical_complete=true`），
  且 `enumerate_elements` 已返回 40/40。推测是合成时枚举预览被结构化侧预算截断（卡片摘要也在同一预算内），模型据此写了保守声明。
  建议：`paper_table` 时由系统覆盖说明定调，提示词里告知模型不要另写覆盖范围；或枚举预览截断时注明「其余标题已由 find_sources 覆盖」。

### 观察 6：多数运行额外调用一次 `enumerate_elements`

- 5/6 题在拿到 `find_sources` 结果后（或之前）又枚举 40 篇来源清单「确认完整候选集」，每次多一轮 reflect（约 3–14 s）。
  账本已给出 `lexical_complete`，但模型仍不放心。可在用法段写明「find_sources 未 capped 且 lexical_complete 时无需再枚举」。

### 观察 7：一次卡片生成失败（模型 JSON 结构错误）

- asr 题中 x-asrrag 的卡片返回非法 JSON（`contributions` 数组里第二个元素缺 `{`），记 `failed/malformed_response`，没有重试；
  Agent 改用 `extract_from_sources` 补上了该篇。按规格该篇进入 1h 冷却。`paper_metadata` 工作负载不在 JSON 修复白名单里，但这类缺括号本也修不了。

### 观察 8：「未展开」清单里的误报来自尚未校准的 related 线

- 当前配置是临时值（强相关 0.7、相关 0.3，规格 §8 说明待 T10 校准）。在 text-embedding-v4 下 related 档偏宽：
  detection 题的「未展开」清单含 5 篇分子 GNN 论文等 8 篇无关论文；asr 题含 4 篇 RAG 论文。表格行没有误报。T10 校准时可参考。

## 耗时与规格 §8 估算对比

- 规格估算：standard 档卡片生成最坏「分 2 波、约 30 秒」。实测每题一次 `get_paper_cards`、6 篇并发生成，耗时 **4.2–6.0 s**；
  一次抽取（1 篇）约 2 s；`find_sources` 0.16–0.87 s。论文工具本身每题合计约 5–8 s，远低于估算。
- 总墙钟 33.7–71.2 s（中位约 60 s），主要花在 reflect 轮（合计 11–40 s，3–6 轮）和最终合成（15–34 s）。
  缺陷 1 与观察 6 各多出的一轮 reflect 是可以省掉的时间。
- 第二次问到同一论文时卡片直接命中缓存（rag、robotrl、detection 各有 1 张 `ready`）。

## 模型调用合计

- 入库：0 次 chat（拦截 120 次逻辑调用），嵌入 80 次。
- 6 题：chat 请求 **86 次**（逻辑 84 + 重试 2），远低于 220 的上限；每题 12–16 次。
