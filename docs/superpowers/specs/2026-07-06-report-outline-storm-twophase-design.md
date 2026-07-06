# 深度报告大纲重做:STORM 预写作 + 充分性判定 + 两阶段确认 设计 Spec

日期:2026-07-06 · 状态:待用户评审 · 前置讨论:report_engine 盲规划问题 + 用户引 STORM/GPT-Researcher/S2G-RAG/多智能体 愿景

## 0. 目标与约束

**目标**:把深度报告的大纲从"单次盲规划(只看问题、不看语料 → 流于表面/像摘要)"升级为**先侦察语料 → 多视角预写作推导 → 充分性判定 → 用户确认/微调 → 再生成长报告**。

**用户已拍板**:
1. **做两阶段确认 UX**(先出大纲给用户看/改,再生成全文)+ 同时把大纲质量做上去。
2. 视角**动态生成为主**,同时默认带 3-4 个通用视角(领域专家/一线实践者/风险怀疑者),动态优先。
3. 充分性用一个 **LLM Judge 打分**(不只零 LLM 探针)。
4. **不内置商业框架库**(PESTEL/波特对 IC/EDA 错配);规划器按材料自选结构。

**硬约束(运行效率一等,见 [[efficiency-first-mandate]])**:大纲阶段 LLM 调用严格算账——目标 **2 次 LLM(规划 + Judge)**,其余走零 LLM 的现成检索原语(亚秒)。绝不做朴素多智能体(5-10 次调用)。

**复用现状(不重造)**:expand_query 分解、报告引擎节间并行、每节 reasoning 深挖、知识缺口外显、每节 grounded、federated_retrieve/_ppr_retrieve 检索原语——均已在。

## 1. 架构总览:报告生命周期改为两阶段

```
旧:POST /reports → [后台一次成:大纲(盲)→逐节深挖→汇总] → done
新:
  阶段1 规划  POST /reports          → 后台跑 Stage A(秒级)→ status=outline_ready
              GET  /reports/{id}     → 拿到 draft outline(带视角/张力/充分性)
              PATCH/reports/{id}/outline → 用户增删改排 + 缺证据节的处置
  阶段2 生成  POST /reports/{id}/generate → 后台跑 Stage B/C/D(几分钟)→ done
```

`reports.status` 取值扩展:`planning → outline_ready → generating → done`(+ `failed/cancelled`)。用户可在 `outline_ready` 停留、编辑;不编辑直接点生成也行。

## 2. Stage A 大纲生成(阶段1,秒级,净增 2 次 LLM)

### A1. Corpus map(0 LLM,亚秒)
汇总"库里实际有什么",喂给规划器接地:
- **来源清单**:active 的 `sources.title`(SQL,≤20 条)+ base 若干(标 [base]/[personal])。
- **KG 命中**:`federated_retrieve(question)` top-N(默认 12)对象的 `name/type/tier`。
- **原文命中**:`_ppr_retrieve(question)` top-N(默认 8)chunk 的 `source_title · section_path`(**只路径不要正文**,省 token)。
- 拼成 ~1.5k 字紧凑 map。**成本**:1 embed + 索引查询 + 1 PPR(scale 索引路径,亚秒)。

### A2. STORM 规划(1 次 LLM,替换现盲规划调用)
一个提示词内完成整套 pre-writing(不拆多调用):
- **视角生成**:先按问题+corpus map**动态生成 2-3 个贴题专家视角**(优先),再补默认通用视角池里选 1-2 个(领域专家/一线实践者/风险怀疑者);
- **多视角提问**:每视角对问题提 2-3 个深问;
- **聚类成节**:去重 + 按主题聚类 → 章节(H2);
- **保留张力**:显式标注视角间的**矛盾/分歧**(洞察来源,别抹平成赞美);
- **MECE**:章节互斥、无重叠(prompt 指令,不单开 pass);
- **接地**:子查询**照抄 corpus map 里的真实词汇/实体名**(治"凭空造检索式");
- **不硬砍**:问题明确要求但 map 里没有的方面**仍保留该节**(撰写层用 [通识] 桥接;map 只是采样,不据此判"无支撑")。

输出每节:`{title, scope, sub_queries[≤4], perspectives[], tensions[]}`(tensions = 与哪节/哪视角有分歧)。

### A3. 充分性探针(0 LLM)
对每节的 sub_queries 各跑一次 `federated_retrieve`,统计命中数(base/personal 拆分)——客观信号(即诊断 §5 那套)。

### A4. 充分性 Judge(1 次 LLM,喂 A3 探针结果)
一次调用判所有节(不逐节调):输入 = 大纲 + 每节探针命中数 + corpus map;输出每节 `{sufficiency: 充足|薄弱|缺失, gap_note(一句), suggested_action: keep|supplement|external}`。**探针给客观量、Judge 给解释与建议,合一次调用。**

**Stage A 成本合计**:2 次 LLM(规划 + Judge)+ 亚秒检索。对比旧的 1 次盲调用——质量跃升(接地+多视角+MECE+充分性旗标),成本仅 +1 次 LLM,秒级返回给用户。

**产出**(存 reports.outline_json,section dict 扩展):
```json
{"title","scope","sub_queries":[],
 "perspectives":["风险怀疑者"],"tensions":["与'方案可行性'节结论相反"],
 "sufficiency":"薄弱","gap_note":"库内缺 X 的实测数据","action":"supplement"}
```

## 3. Stage B/C/D 生成(阶段2,复用现有)
用户确认后的 outline → 现有 `_run_sections`(逐节完整 reasoning 深挖,节间并行)→ `_draft_section`(三层证据)→ `_assemble`(执行摘要+参考+局限)。**几乎不动**;唯一接线:`action='external'` 的节(远期)触发外部检索,v1 先当普通节按 [通识] 处理并在报告标注。

## 4. 数据模型 & API

`reports` 复用现表;`outline_json` 存扩展后的 section dict(含 perspectives/tensions/sufficiency/action)。`status` 增 `planning`/`outline_ready`/`generating`。

- `POST /notebooks/{id}/reports` {question, depth?, history?} → 起阶段1 后台 job,返回 {report_id}。**行为变**:跑到 `outline_ready` 即停(不再一路到底)。
- `GET /notebooks/{id}/reports/{rid}` → 含 outline(带视角/充分性)+ status。
- `PATCH /notebooks/{id}/reports/{rid}/outline` {sections:[...]} → 覆盖用户编辑后的大纲(校验:至少 1 节;title/sub_queries 非空)。仅 `outline_ready` 态可改。
- `POST /notebooks/{id}/reports/{rid}/generate` {depth?} → 从 `outline_ready` → 起阶段2 后台 job(逐节深挖)。
- cancel/delete/list/export 不变。
- 守卫:规划/生成/编辑走 `require_notebook_write`;读走 `require_notebook_read`。

**向后兼容**:若产品想保留"一键直出"(不看大纲),可给 `POST /reports` 加 `auto_generate=true` 让阶段1完直接进阶段2。默认 false(两阶段)。

## 5. 前端(与后端同 PR co-design,见 [[frontend-backend-co-design]])

报告流程改两屏:
1. **发起**:问题输入 + 深度滑块 + 「生成大纲」按钮 → 起阶段1 → 进度"规划中"(秒级)。
2. **大纲编辑器**(新,status=outline_ready 时):
   - 章节卡片列表:title/scope 可编辑;拖拽排序;增/删节;
   - 每节徽章:**视角标签**(哪个专家视角挖出的)、**张力标记**(与哪节分歧,点亮关联)、**充分性徽章**(充足/薄弱/缺失 + gap_note);
   - 缺证据节的处置:保留(标将用 [通识])/ 删除 /(远期)待补充资料;
   - 「生成完整报告」按钮 → PATCH outline + POST generate → 进现有的逐节进度 UI(section_status)。
3. **查看/下载/删除**:复用现有(react-markdown 渲染、批量下载、删除按钮)。
- UI 达 [[ui-polish-bar]];视角/张力可视化要克制(标签+连线,别堆砌)。

## 6. Config(尽量不新增 env,见 [[env-var-reduction-state]])
复用 `report_max_sections` / `report_section_top_n`。新增仅在必要时用 validation_alias:
- `REPORT_SCOUT_KG_TOP_N`(默认 12)、`REPORT_SCOUT_CHUNK_TOP_N`(默认 8)——corpus map 采样量;能用现有 retrieval_top_n 就不新增。
- 通用视角池、动态视角数等写常量,不进 env。

## 7. 测试 & 验收
- 单测:corpus map 组装(有/无 base、空库)、STORM 规划解析+回退(坏 JSON → 退现行盲规划,字节保留)、探针命中统计、Judge 解析、两阶段 API(planning→outline_ready→generating→done 状态机 + 权限 + PATCH 校验)。
- smoke:stub LLM 走通两阶段(起→outline_ready→generate→done)。
- 真机:同一 Bandgap/serdes 题,对照旧盲规划,看大纲是否①接地(子查询用库内词汇)②多视角(有风险/实践视角的节)③标出充分性;用户改一节后生成正常。

## 8. 分期
- **v1(本 spec)**:Stage A 新管线(map+STORM+探针+Judge)+ 两阶段 API + 大纲编辑器前端。
- **v2**:Reviewer agent(门控重规划)、`action=external` 外部检索填补缺口、框架模板(若需要)。

## 9. 效率账(一等约束交代)
- 阶段1:**2 次 LLM**(规划 pro + Judge 可用 flash 省钱)+ 亚秒检索;秒级返回,用户等得起。
- 阶段2:与现状同(逐节深挖是大头,本就几分钟)。
- 净增:每份报告 +1 次 LLM(Judge)相对旧流程;换来接地+多视角+充分性+用户可控。Judge 建议走 flash(`REWRITE_LLM_MODEL`)而非 pro,进一步压成本。

## 10. 未决/风险
- **视角发散**:动态视角可能跑偏离题 → prompt 约束"视角必须服务于回答用户问题,不为多样而多样"。
- **两阶段中断**:用户拿到大纲后不点生成 → 报告停在 outline_ready(列表要能显示该态 + 可续/可删)。
- **张力可视化**过度 → v1 先做文字标签,连线可视化 v2。
- Judge 与探针**冲突**(探针 0 命中但 Judge 说充足)→ 以探针客观量为准、Judge 只解释,prompt 明确。
