"""Three-layer decomposition of the Ask/report prompt surface.

This module is PURE DATA: two frozen dataclasses and the registries built
from them, nothing else. It deliberately imports nothing from
``app.services`` or ``app.core`` (only ``dataclasses``/``typing`` from the
standard library) — the architecture guard
(``scripts/check_architecture_boundaries.py``) treats a service-layer module
importing back into another service module as ordinary, but this module is
kept import-free on purpose so a future lower layer (e.g. a per-notebook
prompt-override store) can depend on it without ever depending on
``app.services.prompts`` itself, and so this file can be read and diffed as
pure content with zero risk of an import cycle.

THE THREE LAYERS
-----------------
**L0 — system skeleton.** Citation discipline ([k] binding, （推断） markers,
grounded=true/false), the JSON schema contract each prompt function
promises its caller, completeness/enumeration wording,
``SCOPE_DEIXIS_GROUNDING``, clarification gating, the claim ledger contract,
and every other piece of prompt text that a server-side parser or a
cross-stack contract (frontend rendering, citation validation, the claim
ledger consumer) depends on verbatim. L0 text can NEVER be overridden by a
per-library or per-notebook customization — doing so would silently break a
contract enforced elsewhere in the stack.

**L1 — optimizable fragments.** Content-organization templates, domain
worked examples, and intent-understanding domain rules: presentation/
understanding-layer text with no server-side parser depending on its exact
wording. This is the layer a future per-notebook customization or
self-evolution loop is meant to act on. This task extracts the CURRENT
wording of 9 such spots into named ``PromptFragment`` entries in
``L1_FRAGMENTS`` — verbatim, byte for byte, with no behavior change — and
gives them exactly one read path, ``fragment_text()``. Nothing outside this
module overrides a fragment yet; that is deliberately future work, not part
of this change.

**L2 — data injection blocks.** Runtime parameters already threaded through
every prompt function as ordinary keyword arguments — retrieved content
(``context_block`` / ``candidates_summary``), conversation state
(``history_block``), and per-notebook/per-run state assembled elsewhere
(``profile_block``, ``experience_block``, ``style_block``,
``collection_map``, ``corpus_langs``, ``discovered_structure``,
``assumptions``, ``report_frame``, ``synthesis_commitment``,
``intent_block``, ``coverage_block``, ``corpus_map``). This module does not
change their code path at all — ``L2_BLOCKS`` only records what exists and
where each one is assembled, as a map for anyone extending the L1/L2
boundary later.

L0-ONLY PROMPTS
----------------
The following prompt-building functions in ``app.services.prompts`` carry NO
L1 fragment at all — every word of their text is control-flow or
self-optimization machinery that must not vary per notebook:
``reflect_prompt`` (and ``reflect_schema_hint``), ``report_synthesis_prompt``,
``report_sufficiency_prompt``, the evidence-verification path
(``evidence_refine_prompt``), ``followup_rewrite_prompt``, and the whole
Agentic Memory group (``agent_profile_base_prompt``,
``agent_profile_overlay_prompt``, ``retrieval_experience_prompt``, and the
untrusted-observation framing constants). Customizing any of these is not a
"different wording" problem — it is a control-flow / self-optimization
mechanism, and any future customization there belongs behind a data
injection parameter (an L2 block), never a text-slot override.

FUTURE OVERRIDE SEAM
---------------------
The only place a future per-notebook or self-evolution feature is meant to
hook in is ``fragment_text()``. Nothing else in this module, or in
``app.services.prompts``, reads ``L1_FRAGMENTS`` any other way. Adding a
lookup-by-notebook layer only ever means changing what ``fragment_text()``
returns for a given id — never adding a second read path.

MODULE LOCATION (decided)
---------------------------
This module lives in ``app.services``, not in ``app.repositories`` or
``app.domain`` — deliberately, because prompt composition itself is a
service-layer concern (this is where ``app.services.prompts`` already
lives, and where every prompt-building call site sits). A future
per-notebook override RESOLVER (the piece that decides, at call time, which
notebook's override — if any — should win over a fragment's default) also
belongs in the service layer, next to the prompt-composition call sites it
serves, not inside ``app.repositories``. If a future override needs
persistent storage, ``app.repositories`` may grow a port for READING/WRITING
override rows — plain data in, plain data out — but that port must never
import this module, and this module must never import a repository port:
the resolver in the service layer is the only piece allowed to see both
sides. ``app.repositories`` importing ``app.services.prompt_layers`` (or any
other ``app.services`` module) is exactly the reverse-dependency shape
``scripts/check_architecture_boundaries.py`` already rejects for
``app.repositories.ports`` — this module makes no exception for itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple


@dataclass(frozen=True)
class PromptFragment:
    """One named, currently-fixed-default L1 text fragment.

    ``boundary`` states in one sentence what this fragment is allowed to
    change and what it must never touch — most importantly, that it can
    never loosen an L0 contract (citation binding, grounding, schema shape).
    """

    fragment_id: str
    prompt_id: str
    layer: str
    boundary: str
    text: str


def _fragment(
    fragment_id: str, prompt_id: str, boundary: str, text: str
) -> PromptFragment:
    return PromptFragment(
        fragment_id=fragment_id,
        prompt_id=prompt_id,
        layer="L1",
        boundary=boundary,
        text=text,
    )


L1_FRAGMENTS: Dict[str, PromptFragment] = {
    fragment.fragment_id: fragment
    for fragment in (
        _fragment(
            "answer.style_language",
            "answer_prompt",
            "回答语言与具体性风格；不得授权新的引用绑定。"
            "起始序号「4. 」与结尾换行符属于契约，覆盖值必须保留两者。",
            "4. Answer in the question's language. Be concrete.\n",
        ),
        _fragment(
            "answer.mechanism_organization",
            "answer_prompt",
            "答案组织模板与领域分层示例；引用/推断标注规则不受影响。"
            "起始序号「8. 」与结尾换行符属于契约，覆盖值必须保留两者。",
            "8. For a question that asks for a multi-layer mechanism or derivation, "
            "organize the answer layer by layer (e.g. circuit principle -> device "
            "physics -> statistical/solid-state physics -> quantum/lattice origin -> "
            "engineering practice) and keep the derivation chain complete within each "
            "layer; where the knowledge items lack a link of the chain, bridge it "
            "explicitly as （推断）.\n",
        ),
        _fragment(
            "answer.domain_conventions",
            "answer_prompt",
            "领域公式书写惯例。"
            "起始序号「9. 」与结尾换行符属于契约，覆盖值必须保留两者。",
            "9. Keep formulas dimensionally consistent and prefer the circuit-"
            "realizable form the sources use (e.g. $\\Delta V_{BE}=V_T\\ln N$ rather "
            "than an abstract $K\\cdot V_T$); when converting between energy and "
            "voltage, state the conversion (e.g. $E_g=qV_{G0}$) — as a （推断） note "
            "if the items use a different notation.\n",
        ),
        _fragment(
            "answer.numeric_attribution",
            "answer_prompt",
            "数值呈现与归因风格；grounding 底线由 L0 规则 1/2 兜底。"
            "起始序号「10. 」与结尾换行符属于契约，覆盖值必须保留两者。",
            "10. When a specific numeric value comes from a single source, attribute "
            "it as that source's stated value; you may add the typical engineering "
            "range or the factors that shift it, marked as （推断）.\n",
        ),
        _fragment(
            "expand_query.decomposition_guidance",
            "expand_query_prompt",
            "子查询分解启发式与领域示例；不改变输出 schema 与数量上限。"
            "plan_prompt 是同一条规划指令的 backup 拼写，在 prompts.py:438-445 "
            "自带一段未被抽取、措辞不同但语义对等的分解指导；两份拼写受同一条 "
            "「must be added to BOTH」纪律约束（见 plan_prompt 函数体前的 NOTE），"
            "任何覆盖此片段的改动都必须同时处理 plan_prompt 那一份，否则规划模型"
            "在两条拼写路径上会看到互相矛盾的分解规则。",
            "For a COMPARISON, "
            "emit ONE sub-query per entity (e.g. 'DeepSeek-V2 architecture and features', "
            "'DeepSeek-V3 improvements'). For a BROAD/overview question, emit one per "
            "distinct dimension. For a simple single-topic question, ONE sub-query is "
            "fine. For a DEEP MECHANISM/DERIVATION question that spans abstraction "
            "levels, emit one sub-query per level it crosses (e.g. circuit principle / "
            "device physics / statistical or solid-state physics / quantum-lattice "
            "origin / engineering constraints such as packaging & materials). Use "
            "canonical entity names.\n",
        ),
        _fragment(
            "intent.cross_tool_mapping",
            "query_intent_prompt",
            "跨工具拆题的领域规则；澄清 gating 与 schema 不受影响。",
            "When the question names TWO OR MORE tools/systems/products and asks how a "
            "capability of one maps to, compares with, or is achieved in another:\n"
            "- Each named tool/system MUST own its own mandatory topic. Never fold the "
            "target tool's side into the source tool's topic.\n"
            "- The target-side topic's retrieval_queries MUST pair the target tool's NAME "
            "with functional description words (what the capability does), NEVER the "
            "source tool's command/API names alone — the target's documents do not "
            "mention the source's identifiers.\n"
            "- If the topic budget cannot cover every split, per-tool topics take "
            "precedence over other decompositions, and the target tool's topic is "
            "never the one dropped.\n"
            "  Example: for \"how do I do Innovus's place_opt_design in ICC2?\", a good "
            "target-side query is \"ICC2 placement optimization command\", not "
            "\"place_opt_design usage\".\n",
        ),
        _fragment(
            "report.storm_lenses",
            "report_storm_outline_prompt",
            "报告预写视角集合；MECE/意图合同不可变等结构规则不受影响。"
            "起始序号「1. 」与结尾换行符属于契约，覆盖值必须保留两者。"
            "L0 第 2 步（RAISE：从每个视角提出深度问题）、输出 schema 的 "
            "perspectives 字段，以及 report_engine 的下游消费点，都建立在"
            "「本步已经定义出一组视角」这个前提上；覆盖值必须继续产出一组"
            "非空的视角集合，否则下游会静默退化为空，而不是报错。",
            "1. Adopt 3-4 expert perspectives (lenses): first dynamically generate 2-3 "
            "PERSPECTIVES tailored to THIS question & corpus; then add 1-2 from the "
            "general set (domain expert / hands-on practitioner / risk-skeptic). "
            "Perspectives must serve answering the user's question — do not add lenses "
            "for mere variety.\n",
        ),
        _fragment(
            "report.frame_example",
            "report_storm_outline_prompt",
            "frame 的领域反例句。以尾随空格「peers. 」结束，紧接 L0 的 "
            "\"For other questions…\"；覆盖值必须保留这个尾随空格，否则两句"
            "会在渲染结果里粘连成一个词。",
            "A capacity mechanism, sequence mixer, macro topology, and memory mechanism are "
            "different facets and must not be emitted as mutually exclusive peers. ",
        ),
        _fragment(
            "report_section.domain_conventions",
            "report_section_prompt",
            "节内领域写作惯例；引用与 claim 台账规则（L0）不受影响。"
            "起始序号「3. 」与结尾换行符属于契约，覆盖值必须保留两者。",
            "3. Keep the derivation chain complete within this section's scope; "
            "keep formulas dimensionally consistent and prefer circuit-realizable "
            "forms; single-source numeric values: attribute as that source's "
            "stated value, ranges may be added as （推断）.\n",
        ),
    )
}


def fragment_text(fragment_id: str) -> str:
    """Return the current text for one L1 fragment.

    This is the ONLY read path for L1 text — the single seam a future
    per-notebook or self-evolution override is meant to intercept (see the
    module docstring's "FUTURE OVERRIDE SEAM" section). An unknown id raises
    ``KeyError`` rather than silently returning an empty string, so a typo'd
    fragment id fails loudly instead of quietly dropping prompt text.
    """
    return L1_FRAGMENTS[fragment_id].text


@dataclass(frozen=True)
class L2Block:
    """Metadata for one runtime data-injection parameter.

    Purely descriptive: it does not read, render, or gate anything. It
    exists so the L1/L2 boundary is legible in one place instead of only in
    scattered per-parameter comments across ``app.services.prompts``.
    """

    block_id: str
    prompts: Tuple[str, ...]
    description: str
    source: str


L2_BLOCKS: Tuple[L2Block, ...] = (
    L2Block(
        "context_block",
        ("answer_prompt", "report_section_prompt"),
        "检索得到的知识条目列表（id: [type][tier] name — context），按 [k] 引用契约编号。",
        "app.services.reasoning_retrieval（组装）",
    ),
    L2Block(
        "candidates_summary",
        ("reflect_prompt",),
        "reflect 循环当前已收集候选证据的摘要文本，驱动下一步检索动作的选择。",
        "app.services.reasoning_retrieval（组装）",
    ),
    L2Block(
        "history_block",
        (
            "answer_prompt",
            "plan_prompt",
            "expand_query_prompt",
            "query_intent_prompt",
            "report_outline_prompt",
            "report_storm_outline_prompt",
            "followup_rewrite_prompt",
        ),
        "此前对话轮次拼成的历史文本，用于消解指代/省略；无历史时为空串。"
        "（reflect_prompt 没有 history_block 形参，不在此列；"
        "followup_rewrite_prompt 把它作为第一个位置参数 history_block 接收。）",
        "app.services.ask_service / app.services.report_engine（调用方内联拼接，无单一渲染函数）",
    ),
    L2Block(
        "profile_block",
        ("plan_prompt", "expand_query_prompt"),
        "Agentic Memory P1：该 notebook 的共享理解摘要（corpus_shape/key_entities/corpus_gaps）；关闭或未生成时为空串。",
        "app.services.agent_profile_block",
    ),
    L2Block(
        "experience_block",
        ("plan_prompt", "expand_query_prompt"),
        "Agentic Memory P2：部署级检索经验库摘要（哪个检索通道对哪类问题有效）；开关关闭时为空串。",
        "app.services.retrieval_experience_block",
    ),
    L2Block(
        "style_block",
        ("answer_prompt", "plan_prompt", "expand_query_prompt"),
        "Agentic Memory P3（T8）：按用户的检索风格提示（仅组织/措辞偏好，不涉及范围或检索通道）；无提示时为空串。",
        "app.services.search_profile.render_style_block",
    ),
    L2Block(
        "collection_map",
        ("plan_prompt", "expand_query_prompt"),
        "按类型分类的可枚举集合计数（仅计数，不含标题/正文），供规划把「列出所有 X」类问题当成盘点而非关键词搜索来处理。",
        "app.services.collection_catalog.render_collection_map",
    ),
    L2Block(
        "corpus_langs",
        ("expand_query_prompt",),
        "语料库涉及的语言代码列表，决定 high/low level keywords 要覆盖哪些语言拼写。",
        "调用方直接传入的参数（非渲染出的文本块）",
    ),
    L2Block(
        "discovered_structure",
        ("report_section_prompt",),
        "本节深挖时整理出的子大纲（报告 PR-5），每行携带绑定到该子主题的知识条目 id。",
        "app.services.report_engine",
    ),
    L2Block(
        "assumptions",
        ("report_section_prompt",),
        "意图合同里记录的范围默认值；仅限定范围，绝不作为证据。",
        "app.services.report_engine（源自 query_intent_prompt 的产出）",
    ),
    L2Block(
        "report_frame",
        ("report_section_prompt",),
        "已确认的分析 frame（facet/axis 定义），供分类字段引用。",
        "app.services.report_engine / app.services.report_synthesis",
    ),
    L2Block(
        "synthesis_commitment",
        ("report_section_prompt",),
        "报告级证据综合承诺：本节被分配要写的论点部分，证据 id 已换算为本节局部 [k] 编号。",
        "app.services.report_engine（源自 report_synthesis_prompt 的产出）",
    ),
    L2Block(
        "intent_block",
        ("report_summary_prompt", "report_storm_outline_prompt", "report_synthesis_prompt"),
        "已确认的意图合同 JSON，跨多个报告阶段传递作为不可变约束。",
        "app.services.report_engine（源自 query_intent_prompt 的产出）",
    ),
    L2Block(
        "coverage_block",
        ("report_storm_outline_prompt",),
        "意图优先的覆盖度探针结果，供大纲规划参考语料实际覆盖情况。",
        "app.services.report_engine",
    ),
    L2Block(
        "corpus_map",
        ("report_storm_outline_prompt",),
        "语料库实际内容的摘要（该库真正包含什么），供大纲跨专家视角规划参考。",
        "app.services.report_engine",
    ),
)
