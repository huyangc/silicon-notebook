import type { ReasoningTraceStep } from "./ask-stream";
import { label } from "./vocabulary.ts";

export const TRACE_STEP_LABELS: Record<string, string> = {
  start: "启动",
  intent: "理解",
  memory: "记忆",
  plan: "规划",
  retrieve: "检索",
  enumerate: "枚举",
  spreadsheet: "表格分析",
  reflect: "反思",
  expand: "扩展",
  ppr: "漫游",
  exact_lookup: "精查",
  expand_community: "对比",
  follow_chain: "推导",
  fallback: "原文",
  // search_chunks = 在原文段落里检索(T1,无图 reflect 动作 + 无图首轮播种共用
  // 这一个 step_type)。⚠ 不能用 fallback: "原文"——那个词已经被上面的
  // search_elements 兜底步占了,两步同名会让轨迹里出现两条读起来一样、说的却是
  // 两回事的步(一条是「回原文里找细节」的元素兜底,一条是「按语义/关键词检索
  // 原文段落」的一等检索动作)。
  search_chunks: "段落",
  // outline = 大纲便签(update_outline reflect 动作写的那一步);仅 exhaustive 档
  // 且 REASONING_OUTLINE_ENABLED 开启时出现(设计文档 §3.1)。
  outline: "大纲",
  // profile = Agent 对这个库的已有理解被带进了这一轮的规划/反思(Agentic Memory
  // P1)。只在真的有内容可带时出现:没整理过的库不落这一步(后端刻意不记空步——
  // 它是一次亚毫秒的主键点查,不像记忆检索那样有耗时需要交代)。
  profile: "经验",
  // experience = 以往检索攒下的「打法」进了这一轮的规划/反思(Agentic Memory
  // P2)。⚠ 不能叫「经验」——那个词已经被上面的 profile 占了,两步同名会让轨迹
  // 里出现两条读起来一样、说的却是两回事的步(一条是「这个库是什么」,一条是
  // 「这类问题该怎么查」)。注入默认关闭,且没蒸出内容时后端不落这一步。
  experience: "打法",
  // consult_memory = reflect 循环里模型主动拉取的一次经验查询(Agentic Memory
  // P4):合并送达部署级「打法」经验库里这一轮新命中的条目、外加本人覆盖层
  // 「检索心得」里还没被动块带出的那句话。⚠ 不能叫「记忆」——那个词已经被
  // Memory 召回步(见上面 memory: "记忆")占用,两步同名会让轨迹里出现两条读
  // 起来一样、说的却是两回事的步。
  consult_memory: "回想",
  answer: "合成",
  // answer = 检索器决定作答并报告采用了哪些证据;synthesis = 答案真的写出来了。
  // 分两步是因为中间那次生成调用往往是整轮里最长的一段,合并会让它彻底隐形。
  synthesis: "作答",
  skip: "跳过",
  // gap_consult = 缺口外扩检索(ask.gap_consult,X9 PR-A):这一轮结束前向部署
  // 插件问了一次"笔记本之外有没有相关材料",与上面的 memory/experience/
  // consult_memory 都是不同的东西——那三步问的是"这个库/这类问题以前怎么样",
  // 这一步问的是"这个库以外还有什么"。零插件部署一步都不产生(见宿主 no-op)。
  gap_consult: "外扩",
  plugin: "扩展",
};

// next_action 取值来自 backend/app/services/prompts.py 的状态机决策(reflect 步骤
// next-step 提议),原样显示会把英文动作名泄漏给用户。
// 全部 13 个真实取值见 reasoning_retrieval.py 的 next_action if/elif 分发链——从
// `decision.next_action == "answer" or decision.sufficient` 起,到
// `elif decision.next_action == "expand_community":` 止(PR-2 在其中插入了
// enumerate_elements/enumerate_kg_objects 两个,精确查找通道插入了 exact_lookup
// 一个,O1 插入了 update_outline(`OUTLINE_ACTION`)一个,Agentic Memory P4 插入了
// consult_memory(`CONSULT_MEMORY_ACTION`,仅 deep 及以上档且经验注入闸开启时
// 出现)一个,T1 插入了 search_chunks(在原文段落里检索,无图时是唯一进入大预算
// 分区的一等入口,有图 run 也可选)一个,原为 7 个)。按分支内容定位而非行号:
// 本仓库的行号指针已知会随后续改动腐烂(见 test_architecture_documentation 一类
// 语义化守卫的教训),这里不重蹈覆辙。用「下一步意图」措辞而非机制名(ppr/
// community/chain/enumerate/exact_lookup/update_outline/consult_memory 这些是
// 内部机制,不该摆给用户)。
//
// PR-2.5 的来源清单刻意**不在**这张表里:它不是新增动作,而是 enumerate 动作的
// 一个参数值(`enumerate.collection="sources"`),所以反思步的「下一步意图」仍是
// 「列元素清单」,而真正发生的那一步由 enumerate 步自己的 summary 说清
// (「枚举来源清单: …」——后端 `_collection_label` 拼的)。update_outline 则相反:
// 它是货真价实的第 11 个动作 id(不是参数值),所以这张表要加一行。
const NEXT_ACTION: Record<string, string> = {
  answer: "开始作答",
  expand_graph: "顺着相关内容继续找",
  add_subquery: "换个角度再查一遍",
  search_elements: "回原文里找细节",
  search_chunks: "在原文段落里检索",
  enumerate_elements: "列元素清单",
  enumerate_kg_objects: "列知识对象清单",
  ppr_retrieve: "顺着关联扩大范围",
  expand_community: "找相似内容对比",
  follow_chain: "顺着推导链继续",
  exact_lookup: "按名称精确查找",
  update_outline: "整理大纲",
  consult_memory: "回想以往的查法",
};

export type ReasoningTraceSummary = {
  title: string;
  latestLabel: string;
  latestSummary: string;
  latestDetail: string;
  stepCountLabel: string;
  totalLabel: string;
};

// 把毫秒渲染成人话:<1s 用 ms、<1min 用 x.xs、更久用 xmxs。
// 非有限/负值一律归零,避免 NaN 泄漏到 UI。
export function formatDuration(ms: number): string {
  const v = Number.isFinite(ms) ? Math.max(0, Math.round(ms)) : 0;
  if (v < 1000) return `${v}ms`;
  if (v < 60000) return `${(v / 1000).toFixed(1)}s`;
  const totalSec = Math.round(v / 1000);
  return `${Math.floor(totalSec / 60)}m${totalSec % 60}s`;
}

// 轨迹总耗时 = 各步 duration_ms 之和(缺失按 0)。
function totalDurationMs(steps: ReasoningTraceStep[]): number {
  return steps.reduce(
    (sum, step) => sum + (typeof step.duration_ms === "number" ? step.duration_ms : 0),
    0,
  );
}

// 检索结束事实的摘要片段(设计稿 2026-09-07 §7.2)。
//
// **结束原因的中文短句由服务端给**(`termination_summary`),前端只渲染字段、不
// 自造一份 reason 码 → 文案的映射:那份映射与后端的闭集会各自演化,而先分叉的
// 一定是"新加了一个原因码,前端静默显示成兜底文案"这一种——用户看到的是"检索
// 结束",真正发生的事一个字都没说。收尾那条 skip 步不带这个字段,因为它的
// `summary` 本身就是同一句话(轨迹已经把它显示在标题位)。
//
// 计数一律按 `typeof === "number"` 防御:历史 trace 没有这些键,`?? 0` 会把
// "这条记录没有这个字段"渲染成"0 项未送达",而那是一句假话。
//
// 方面计数有**两种拼法**,因为写它们的是两个不同的步:检索收尾那条 skip 步记
// `aspects` / `unresolved_aspects`(reasoning_retrieval 的 `_run_termination`),
// 合成终步记 `aspects_total` / `aspects_pending`(ask_service / report_engine 经
// `reasoning_aspects.termination_synthesis_detail`)。后者的键带前缀是因为它与
// `aspects_undelivered` 等几个口径同处一个 detail,裸 `aspects` 在那里读不出是
// 哪一个口径。这里按序取第一个是数字的,而不是在后端改名——那两个键已经在
// T4-A 的用例里,改名会让"关闭态与历史 trace 逐字不变"这条红线失效。
function firstNumber(
  detail: Record<string, unknown>, ...keys: string[]
): number | undefined {
  for (const key of keys) {
    if (typeof detail[key] === "number") return detail[key] as number;
  }
  return undefined;
}

function terminationParts(detail: Record<string, unknown>): string[] {
  const parts: string[] = [];
  const total = firstNumber(detail, "aspects_total", "aspects");
  const pending = firstNumber(detail, "aspects_pending", "unresolved_aspects");
  if (typeof total === "number" && typeof pending === "number" && total > 0) {
    parts.push(`已处理 ${Math.max(0, total - pending)}/${total} 方面`);
  }
  const undelivered = detail.aspects_undelivered;
  if (typeof undelivered === "number" && undelivered > 0) {
    // 「未送达」= 模型说这个方面有支撑,但那些证据全被最终装配的预算/过滤挡在
    // 了合成之外(§7.2)。它与「未处理」是两件事,所以另占一格而不是并进上面
    // 那个分数——合起来说会让"模型判断"与"服务端送了什么"再也分不开。
    parts.push(`${undelivered} 项未送达`);
  }
  const channels = detail.unrecovered_channels;
  if (Array.isArray(channels) && channels.length) {
    // 只报条数:通道名(add_subquery / ppr_retrieve …)是内部动作词,上屏只会
    // 让用户去猜一个他们没有词汇表的东西;要排查的人在 detail 原文里读得到。
    parts.push(`${channels.length} 条通道未恢复`);
  }
  return parts;
}

export function getTraceStepDetail(step: ReasoningTraceStep): string {
  const detail = step.detail ?? {};
  if (step.step_type === "plugin") {
    const text = typeof detail.detail === "string" ? detail.detail : "";
    return detail.truncated
      ? [text, "后续步骤已截断"].filter(Boolean).join(" · ")
      : text;
  }
  if (step.step_type === "intent" && typeof detail.resolved_question === "string") {
    return detail.resolved_question;
  }
  if (step.step_type === "follow_chain") {
    const parts: string[] = [];
    if (typeof detail.hops === "number") parts.push(`${detail.hops} 跳`);
    if (typeof detail.count === "number") parts.push(`${detail.count} 条`);
    if (typeof detail.chain_trust === "number") {
      const percentage = Math.round(Math.max(0, Math.min(1, detail.chain_trust)) * 100);
      parts.push(`可信度 ${percentage}%`);
    }
    return parts.join(" · ");
  }
  if (step.step_type === "plan" && Array.isArray(detail.sub_queries)) {
    return `${detail.sub_queries.length} 个子查询`;
  }
  // 大纲便签(update_outline 落的 outline 步)。summary 已经是「更新大纲: N 节(M
  // 节待补证据)」这句完整文案,这里给一个更短的复述——供折叠态的 latestDetail
  // 用,与 enumerate 步「summary 详述、detail 给终态数字」的先例一致。sections 是
  // 整份大纲(数组),empty_sections 是空节标题(数组);两者都按 length 读,不存在
  // total=null 那类分母未知陷阱,但仍需 Array.isArray 防御,防止畸形 detail 把
  // "3 节(undefined 节待补)" 送上屏。
  if (step.step_type === "outline") {
    const sections = Array.isArray(detail.sections) ? detail.sections.length : 0;
    const empty = Array.isArray(detail.empty_sections) ? detail.empty_sections.length : 0;
    return `${sections} 节(${empty} 节待补)`;
  }
  // memory/synthesis/profile 必须先于下面按 detail 形状的通用分支:三者的
  // count/anchors/blocks 数的都不是「候选」,落到通用分支会给出一个读起来对、
  // 其实错位的数。profile 这一支尤其:它的 detail 还带 chars(整块字符数),
  // 通用分支虽然读不到 chars,但一旦哪天 profile 的 detail 多出一个 count 键,
  // 排在后面就会被静默渲染成「N 个候选」。
  if (step.step_type === "profile") {
    return typeof detail.blocks === "number" ? `${detail.blocks} 条已有理解` : "";
  }
  // experience 与 profile 同一条理由:它的 detail 是 entries/chars,两个都不是
  // 「候选数」,落到下面的通用分支就会渲染出一个读起来对、其实错位的数。
  if (step.step_type === "experience") {
    return typeof detail.entries === "number" ? `${detail.entries} 条打法` : "";
  }
  // consult_memory(Agentic Memory P4,T5):同一条理由,必须排在通用分支之前。
  // 后端 reasoning_retrieval.py 实际写入的 detail 只有 entries(本次新增的经验
  // 库条目数)与 chars(渲染出的整块字符数)——本人覆盖层「检索心得」是否被一并
  // 带出只是块内附加的一行文字,没有独立计数字段可读,所以这里不编造一个「M 条
  // 心得」出来。entries 为 0 但仍落这一步的情形(只带出覆盖层那句话、经验库没有
  // 新条目)照常显示「0 条打法」,与 profile/experience 对 0 的处理方式一致。
  if (step.step_type === "consult_memory") {
    return typeof detail.entries === "number" ? `${detail.entries} 条打法` : "";
  }
  // gap_consult(ask.gap_consult,X9 PR-A):同一条理由,必须排在通用分支之前。
  // detail.count 数的是插件给回的站外建议条数,不是"候选"——落到下面的通用
  // 分支会渲染成"N 个候选",读起来像是又找到了一批本笔记本内的证据,而这些
  // 建议从未参与检索、从未进入证据池。
  if (step.step_type === "gap_consult") {
    return typeof detail.count === "number" ? `${detail.count} 条建议` : "";
  }
  if (step.step_type === "memory") {
    return typeof detail.count === "number" ? `${detail.count} 条记忆` : "";
  }
  if (step.step_type === "synthesis") {
    // 按节合成(设计文档 §3.1)的每节进度步复用 synthesis 这个 step_type,但它的
    // detail 只有 section_index/section_total/section_title,没有 anchors —— 必须
    // 先判这个形状,否则会落到下面 `typeof detail.anchors === "number"` 的分支
    // 判定为假,渲染出空字符串,白白丢掉「写到第几节了」这条本该有的进度文案。
    if (typeof detail.section_index === "number" && typeof detail.section_total === "number") {
      return `第 ${detail.section_index}/共 ${detail.section_total} 节`;
    }
    // 用 anchors(模型真正绑上的 [k])而不是 citations —— 后者是「每条检索到的
    // 证据一张卡」,零绑定的回答上会读成「10 处引用」。citations/evidence_level
    // 仍留在 detail 里供排查,但不上屏:那是内部口径。included_kg/
    // included_chunks/included_elements 同理:PR-1 止血加的诊断字段,记录真正
    // 进入合成 prompt 的计数(区别于更早 answer 步的候选池计数),同样只供排查
    // 不上屏,不在此处渲染。outline_skipped/ungrounded_sections 则相反,必须
    // 上屏(codex r5):它们是按节合成的诚实披露——大纲里有节但答案里没有/
    // 有节但没过依据门,不显示的话用户拿到的是一份「看起来完整」的多节答案。
    // 标题服务端已截 60 字符、列表 ≤12 节,这里再收到前 3 个防折叠行超长。
    const parts: string[] = [];
    if (typeof detail.anchors === "number") parts.push(`${detail.anchors} 处引用`);
    const sectionTitles = (value: unknown): string[] =>
      Array.isArray(value)
        ? value.filter((title): title is string => typeof title === "string" && !!title)
        : [];
    const nameSections = (titles: string[]): string =>
      titles.slice(0, 3).join("、") + (titles.length > 3 ? ` 等 ${titles.length} 节` : "");
    const skipped = sectionTitles(detail.outline_skipped);
    if (skipped.length) parts.push(`证据不足略过 ${skipped.length} 节: ${nameSections(skipped)}`);
    const ungrounded = sectionTitles(detail.ungrounded_sections);
    if (ungrounded.length) parts.push(`${ungrounded.length} 节依据不足: ${nameSections(ungrounded)}`);
    // 检索结束事实(设计稿 §7.2)。**接在既有 parts 之后**:上面几项说的是这次
    // 合成本身,这几项说的是"喂给它的检索是怎么结束的"——顺序即因果。reflect v2
    // 关闭(默认)与所有历史 trace 都没有这些键,parts 一个都不追加,输出逐字
    // 回到接入前。这里刻意不加横幅、不改布局:一次部分完成的检索不是错误。
    const summary = detail.termination_summary;
    if (typeof summary === "string" && summary) parts.push(summary);
    parts.push(...terminationParts(detail));
    return parts.join(" · ");
  }
  if (step.step_type === "exact_lookup") {
    // terms 是服务端本轮真正探测过的名称(已按上限截过),不是问题里出现的全部。
    // 名称和新增段数一起显示,用户才看得出「查的是哪个名字、捞回了多少」——落到
    // 下面的通用 found 分支只说得出后半句。
    const terms = Array.isArray(detail.terms)
      ? detail.terms.filter((term): term is string => typeof term === "string" && !!term)
      : [];
    const parts: string[] = [];
    if (terms.length) parts.push(terms.join("、"));
    if (typeof detail.found === "number") parts.push(`新增 ${detail.found} 段`);
    return parts.join(" · ");
  }
  // search_chunks(T1):原文段落检索一等动作,首轮无图播种(detail.phase==="seed",
  // 无 query)与反思循环里的 agent 动作(不带 phase,有 query,与 ppr 步同形)
  // 共用同一个 step_type,也共用这一条渲染——seed 没有单条 query 时就只显示
  // found,不编造。必须排在下面 `detail.found` 通用分支之前,否则会把 query
  // 悄悄吞掉,只剩「新增 N」,用户看不出查的是哪句话。
  if (step.step_type === "search_chunks") {
    const parts: string[] = [];
    if (typeof detail.query === "string" && detail.query) parts.push(detail.query);
    if (typeof detail.found === "number") parts.push(`新增 ${detail.found} 段`);
    return parts.join(" · ");
  }
  // 检索收尾那条 skip 步(reflect v2,设计稿 §7.2)。它复用既有的 `skip` 类型 +
  // 一个稳定原因码,不新增 step_type。必须排在下面 `detail.pending` 那条之前:
  // 两者的 detail 形状不相交,但把 run 级叙述交给一条按 `pending` 判的分支去
  // 处理,只会在哪天它多出一个 pending 键时静默渲染成"N 个方向未执行"。
  // 结束原因本身不在这里复述——它已经是这一步的 `summary`(标题位)。
  if (step.step_type === "skip" && detail.reason === "retrieval_termination") {
    return terminationParts(detail).join(" · ");
  }
  if (step.step_type === "skip" && typeof detail.pending === "number") {
    // 步骤预算不足时未能执行的已确认检索方向数。summary 已逐条列出(有界),
    // detail 只补一个总数 —— 它不带 count/found,落到下面的通用分支只会返回空,
    // 用户就看不出"漏了几个"。
    return `${detail.pending} 个方向未执行`;
  }
  if (step.step_type === "enumerate" && typeof detail.scanned_rows === "number") {
    return `${detail.scanned_rows}/${Number(detail.known_total_rows ?? 0)} 行`;
  }
  // PR-2 集合枚举工具的 enumerate 步:字段名刻意与上面 Knowhow 那条不同
  // (returned_total/total,不是 scanned_rows/known_total_rows——那是表的「行」口径,
  // 这里数的是集合的「条目」,而且分母可能未知)。用 detail.collection 存在与否
  // 区分两条分支,不能共用同一个数字读法,否则会把「12 条」渲染成「12/0 行」。
  if (
    step.step_type === "enumerate"
    && typeof detail.collection === "string" && detail.collection
    && typeof detail.returned_total === "number"
  ) {
    if (detail.complete) return "已全部列出";
    if (detail.total === null || detail.total === undefined) {
      return `${detail.returned_total} 条（总数未知）`;
    }
    return `${detail.returned_total}/${detail.total} 条`;
  }
  if (typeof detail.count === "number") return `${detail.count} 个候选`;
  if (typeof detail.found === "number") return `新增 ${detail.found}`;
  if (typeof detail.next_action === "string") return label(NEXT_ACTION, detail.next_action, "");
  if (typeof detail.kg === "number" || typeof detail.elements === "number") {
    // 「知识对象」而非「概念」:detail.kg 数的是图谱里的各类对象(Concept / Claim /
    // Formula / Procedure,外加 knowhow 表带来的自定义类型),叫「概念」等于把这堆
    // 类型统统降格成其中一种,用户看到的数与图谱里实际的东西对不上。
    return `${Number(detail.kg ?? 0)} 个知识对象 / ${Number(detail.elements ?? 0)} 段原文`;
  }
  return "";
}

export function getReasoningTraceSummary(
  steps: ReasoningTraceStep[],
  live = false,
): ReasoningTraceSummary {
  const visibleSteps = steps.filter((step) => step.step_type !== "source_subgraph");
  const latest = visibleSteps[visibleSteps.length - 1];
  if (!latest) {
    return {
      title: live ? "Agent 推理中" : "Agent 推理轨迹",
      latestLabel: "",
      latestSummary: "等待后端事件…",
      latestDetail: "",
      stepCountLabel: "0 步",
      totalLabel: "",
    };
  }
  const totalMs = totalDurationMs(visibleSteps);
  return {
    title: live ? "Agent 推理中" : "Agent 推理轨迹",
    latestLabel: label(TRACE_STEP_LABELS, latest.step_type, "处理中"),
    latestSummary: latest.summary,
    latestDetail: getTraceStepDetail(latest),
    stepCountLabel: `${visibleSteps.length} 步`,
    totalLabel: totalMs > 0 ? formatDuration(totalMs) : "",
  };
}
