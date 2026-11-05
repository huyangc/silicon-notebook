"""Pure reasoning-outline values, evidence binding, and reflect-note rendering.

Run state, action dispatch, cancellation, I/O, and budget consumption remain in
``reasoning_retrieval``. Legacy imports there re-export these same objects.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Sequence, Tuple

from app.services.citation_markers import LOOSE_MARKER_RE
from app.services.reports.policy import (
    DEFAULT_REASONING_MAX_OUTLINE_UPDATES,
    OUTLINE_EVIDENCE_KEY_CHARS,
    OUTLINE_ID_CHARS,
    OUTLINE_MAX_EVIDENCE,
    OUTLINE_MAX_SECTIONS,
    OUTLINE_TITLE_CHARS,
)
from app.services.retrieval import RetrievedKnowledge


def _rescored_keeping_origin(rescored, collected):
    """The closing rerank's rescored hit, carrying the ORIGIN the collected
    hit was stamped with.

    ``retrieve_scored`` re-scores against the active library only and returns
    unstamped hits; swapping one in for a federated hit dropped its
    ``notebook_id`` and ``tier``. In a global run (every hit stamped with its
    owner, the nominal active included) that left the answer's KG card and
    anchor with no library -- an unattributable citation. A copy, never an
    in-place edit: the rescored hit may be shared.
    """
    if rescored is None:
        return collected
    if not getattr(collected, "notebook_id", "") or getattr(rescored, "notebook_id", ""):
        return rescored
    return replace(rescored, notebook_id=collected.notebook_id, tier=collected.tier)


# --------------------------------------------------------- 大纲便签(outline)
#
# DualGraph(arXiv:2602.13830)借鉴的 v1(设计文档 §3.1):把「怎么写」(大纲)与
# 「知道什么」(证据)分开,逐轮共演化。大纲是 **run 局部**的一张便签——不持久化、
# 不进 AskResponse,只经 trace 与 ReasoningResult 出场。
#
# 它只在 exhaustive 档提供(见 outline_wiring_active):按节合成是 k 次真实的模型
# 调用,那笔钱必须由用户显式选「穷尽」来承担。
OUTLINE_ACTION = "update_outline"

# 有界性是硬约束(设计文档 §3.1)。三条边界各自的作用:
#   * 12 节 —— 一份能读的大纲的上限,同时是回喂账目长度的分母;
#   * 60 字符标题 —— 沿用 `_INTENT_DIRECTION_LABEL_CHARS` 的先例(同一份 prompt
#     里「一个标签占一行」的宽度);
#   * 每节 8 个证据 key —— 一节的支撑证据,不是一个证据池。
# id/parent 32 字符是**模型自己起的**短句柄的宽度;证据 key 48 字符则必须容得下
# 真实代理 id(`prefix-` + 32 位 uuid hex ≈ 35 字符),截短会让合法绑定对不上。
_OUTLINE_MAX_SECTIONS = OUTLINE_MAX_SECTIONS
_OUTLINE_TITLE_CHARS = OUTLINE_TITLE_CHARS
_OUTLINE_ID_CHARS = OUTLINE_ID_CHARS
_OUTLINE_EVIDENCE_KEY_CHARS = OUTLINE_EVIDENCE_KEY_CHARS
_OUTLINE_MAX_EVIDENCE = OUTLINE_MAX_EVIDENCE
# 标题里的引用形标记(`[k12]`/`[k12, k13]`)在解析入口剥掉(codex r6):证据引用
# 属于 evidence 字段,标题只是文案。放它留到 `## 标题` 进最终 Markdown 的话,
# 前端是对合并全文扫标记建引用表的——标题里的 `[k5001]` 要么显示成一个绑不上
# 的裸引用,要么恰好撞上别节号段、绑到毫不相干的证据。单一定义点在这里:
# trace 步、账目回喂与最终标题共用同一份解析产物,一处剥、处处干净。
_OUTLINE_TITLE_MARKER_RE = LOOSE_MARKER_RE
# 每 run 最多几次 update_outline。大纲本身不带来证据,所以它的成本是「轮次」:
# 无上限时一个偏爱整理的模型可以把整份步骤预算花在反复重排目录上。6 次足够
# 「建 → 补 3 轮 → 收尾」,而 exhaustive 档的 50 步预算仍有绝大部分留给检索。
_MAX_OUTLINE_UPDATES = DEFAULT_REASONING_MAX_OUTLINE_UPDATES
# pending 最坏由 6 次常规提交 + 1 次专用纠错各贡献 8 个不同 key。完整状态留在
# 服务端/终态 trace,每轮 prompt 只展示前 8 个,所以提示预算不随批次数增长。
_OUTLINE_MAX_PENDING_EVIDENCE = _OUTLINE_MAX_EVIDENCE * (
    _MAX_OUTLINE_UPDATES + 1
)


@dataclass
class OutlineSection:
    """大纲的一节。``evidence_keys`` 只保留通过服务端校验的候选标识。

    ``parent`` 为空 = 顶层节;非空 = 它的父节 id,且父节自己必须是顶层节(两层封顶,
    见 ``parse_outline_sections``)。这是给 O2 按节合成用的结构,不是给用户看的
    ——上屏的只有 trace 摘要与(O2 之后的)答案标题层级。
    """

    id: str
    title: str
    parent: str = ""
    evidence_keys: List[str] = field(default_factory=list)
    # 只存在于一次 update_outline 提交里:显式撤销同 id 旧节的绑定。合并后的
    # run 状态永远把它清空,所以下一轮账目不会把一次性命令误当成持久字段重放。
    remove_evidence_keys: List[str] = field(default_factory=list)


def _outline_text(value: object, limit: int) -> str:
    """模型给的自由文本 → 压空白 + 截断(镜像 knowhow 补全的 `_completion_text`)。"""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def parse_outline_sections(raw: object) -> List[OutlineSection]:
    """把 reflect 响应里的根级 ``outline_sections`` 列表夹成一份合法大纲(形状层,纯函数)。

    只做**形状与边界**:证据 key 是否合法要看 run 局部的候选集合,由
    ``bind_outline_evidence`` 单独判(那是事实层,而这里连候选是什么都不知道)。

    fail-open 逐项丢弃,与 enumerate 分支的非白名单值处理同形:
      * 非 dict 的条目、无标题的条目直接丢(没有标题的东西不是「一节」——O2 要拿
        它当 `## 标题`);
      * 超过 12 节的部分丢弃,不是整份拒绝;
      * id 缺失或重复时按位置补一个确定性 id(`s1`、`s2`……),模型才有稳定句柄
        可以在下一轮原样重提,parent 也才指得准;
      * parent 指向不存在的节、指向自己、或指向一个**本身就有父节**的节时清空
        (提为顶层)。第三条就是「两层」的实现,顺带让 A↔B 这类环自动化解。
    """
    if not isinstance(raw, list):
        return []
    sections: List[OutlineSection] = []
    used_ids: set = set()
    for entry in raw:
        if len(sections) >= _OUTLINE_MAX_SECTIONS:
            break
        if not isinstance(entry, dict):
            continue
        raw_title = entry.get("title")
        if isinstance(raw_title, str):
            # 先剥标记再压空白/截断:截断后再剥会把半截 `[k1` 留在标题里。
            raw_title = _OUTLINE_TITLE_MARKER_RE.sub("", raw_title)
        title = _outline_text(raw_title, _OUTLINE_TITLE_CHARS)
        if not title:
            continue
        section_id = _outline_text(entry.get("id"), _OUTLINE_ID_CHARS)
        if not section_id or section_id in used_ids:
            section_id = _unique_outline_id(
                section_id or f"s{len(sections) + 1}", used_ids
            )
        used_ids.add(section_id)
        keys: List[str] = []
        raw_keys = entry.get("evidence")
        if isinstance(raw_keys, list):
            for key in raw_keys:
                if len(keys) >= _OUTLINE_MAX_EVIDENCE:
                    break
                text = _outline_text(key, _OUTLINE_EVIDENCE_KEY_CHARS)
                if text and text not in keys:
                    keys.append(text)
        remove_keys: List[str] = []
        raw_remove_keys = entry.get("remove_evidence")
        if isinstance(raw_remove_keys, list):
            for key in raw_remove_keys:
                if len(remove_keys) >= _OUTLINE_MAX_EVIDENCE:
                    break
                text = _outline_text(key, _OUTLINE_EVIDENCE_KEY_CHARS)
                if text and text not in remove_keys:
                    remove_keys.append(text)
        sections.append(OutlineSection(
            id=section_id, title=title,
            parent=_outline_text(entry.get("parent"), _OUTLINE_ID_CHARS),
            evidence_keys=keys,
            remove_evidence_keys=remove_keys,
        ))
    # 两层封顶:先算出「谁有父节」,再据此裁剪。必须在收齐所有节之后做——模型完全
    # 可能先写子节再写父节,边走边判会把合法的前向引用误判成非法。
    ids = {section.id for section in sections}
    parented = {
        section.id for section in sections
        if section.parent and section.parent in ids and section.parent != section.id
    }
    for section in sections:
        if (
            section.parent not in ids
            or section.parent == section.id
            or section.parent in parented
        ):
            section.parent = ""
    return sections


def _unique_outline_id(base: str, used: set) -> str:
    """确定性去重:`s1` 撞了就 `s1-2`、`s1-3`……(同一份输入总得到同一份 id)。

    **有界性论证**(这个函数跑在 Ask 的 worker 线程里,循环体内没有取消检查,外层的
    `except Exception` 也接不到一个不返回的函数——所以它的终止必须是结构性的,不能
    靠「大概会撞上一个空位」):

    * 后缀段最多试 `_OUTLINE_MAX_SECTIONS + 1` 个候选,`for range` 天然有界;
    * 拼后缀**之前**先把 base 裁到 `_OUTLINE_ID_CHARS - len(后缀) - 1`,让后缀不会
      被那次截断吃掉。少了这一步,长 base 会让每个候选都截回同一个字符串:33 字符
      且前 32 位相同的两个 id(模型给对比大纲起 `…-approach-a` / `…-approach-b`
      这类 slug 是默认写法)、或者三个 31 字符的同名 id(`f"{base}-2"[:32]` 恰好把
      数字截掉、只剩一个尾随连字符),都会让原来的 `while True` 变成 100% CPU 的
      死循环,只能重启后端;
    * 位置 id 兜底:候选取自 `s1..s{len(used)+1}`,共 `len(used)+1` 个互异候选对
      `len(used)` 个已占用 id —— 鸽笼原理保证其中必有一个空位。调用方的 `used` 在
      分配第 N 个 id 时恰有 N-1 个成员(每接受一节加一个,且总数封在 12),所以这一
      段实际上只是防御性的。
    """
    if base:
        if base not in used:
            return base
        for suffix in range(2, _OUTLINE_MAX_SECTIONS + 2):
            stem = base[:max(1, _OUTLINE_ID_CHARS - len(str(suffix)) - 1)]
            candidate = f"{stem}-{suffix}"
            if candidate not in used:
                return candidate
    for position in range(1, len(used) + 2):
        candidate = f"s{position}"
        if candidate not in used:
            return candidate
    # 鸽笼原理下不可达。真到了这里就是上面的不变式被改坏了,响亮报错胜过返回一个
    # 与别人重复的 id(那会让 parent 指向两节中的某一节,静默错乱)。
    raise AssertionError("outline id space exhausted")


def outline_binding_keys(
    collected, elements, chunks, ever_shown_keys, retained_keys=()
) -> set:
    """大纲绑定键的合法集合(事实层):候选池里**曾对模型展示**的标识。

    两条边界,方向相反,都要守住:

    * 口径是「本 run 的任一候选摘要展示过它」,而不是只看上一轮窗口。`_summarize`
      对候选池开头尾窗口,所以用 run 内单调的 ``ever_shown_keys``:早先展示并绑定的
      key 即使后来滑出窗口也不会失效,窗口中段从未展示的 key 则不能靠猜 id 通过。
      ``retained_keys`` 是当前大纲已经合法持有的服务端状态,在 union 时继续有效。
    * 合法集必须 **⊆ 模型可见或服务端已持有**。所以 v1 **不含枚举清单条目的 id**:枚举
      账目按合同只回覆盖计数、不回条目 id(元素/知识对象清单一个字的正文都不带),
      模型根本拿不到那些 id,把它们放进合法集只是一片没人能踩到的死面——而它同时
      放宽了「只能引用服务端给过你的东西」这条校验。O2(或后续让清单条目 id 对模型
      可见的改动)要用它们时,连同可见性一起加回来,并在设计文档 §3.1 记一笔。
    * 同样刻意**不含来源 id**:一份文档不是一条证据,绑上它对 O2 的按节合成产不出
      任何上下文切片;来源清单给模型的句柄本来也是标题而不是 id
      (见 `_source_titles_note`)。
    """
    candidates = set(collected)    # KG 候选:`collected` 的键就是 object_id
    candidates.update(str(getattr(el, "element_id", "")) for el in elements)
    candidates.update(str(getattr(c, "chunk_id", "")) for c in chunks)
    candidates.discard("")
    allowed = {str(key) for key in ever_shown_keys if str(key)}
    allowed.update(str(key) for key in retained_keys if str(key))
    return candidates & allowed


def bind_outline_evidence(
    sections: List[OutlineSection], legal_keys: set
) -> Tuple[List[OutlineSection], int]:
    """把模型给的绑定键按合法集合过滤,返回 (新大纲, 被丢弃的键数)。

    非法键**静默丢弃**(口径同 knowhow 补全的证据 key 校验:模型只能引用服务端
    发出去的标识,自造的一律不算),而丢空了的节**保留为空节**——空节不是错误,
    它是下一步的检索方向,删掉它等于把这个特性最有用的那半功能扔掉。

    返回新对象而不是就地改:入参来自 `ReflectDecision`,run() 会把返回值当成新的
    大纲状态留存,共享同一批对象会让后续动作意外改到上一轮的账目。
    """
    bound: List[OutlineSection] = []
    dropped = 0
    for section in sections:
        keys = [key for key in section.evidence_keys if key in legal_keys]
        dropped += len(section.evidence_keys) - len(keys)
        bound.append(OutlineSection(
            id=section.id, title=section.title, parent=section.parent,
            evidence_keys=keys,
            remove_evidence_keys=list(section.remove_evidence_keys),
        ))
    return bound, dropped


def merge_outline_evidence(
    previous: List[OutlineSection],
    submitted: List[OutlineSection],
    legal_keys: set,
) -> Tuple[List[OutlineSection], int, Dict[str, List[str]], int]:
    """合并一次整份大纲提交,保住同 id 节已经绑定的证据。

    章节结构仍是全量替换:上一份里被整个省略的 section 会消失。证据则是单独的
    citation-persistence 合同:同 id 节先应用 ``remove_evidence`` 显式删除,再把
    本次合法 ``evidence`` 与剩余旧键取并集。遗漏一个 evidence key 不再等于删除。

    每节 8 键硬顶下旧键优先。装不下的**新**键不静默挤掉旧绑定,而是按 section id
    返回给账目回喂,让模型下一轮用 ``remove_evidence`` 明确腾位后再加。返回值依次为
    ``(merged, invalid_count, overflow_by_section, removed_count)``。
    """
    bound, dropped = bind_outline_evidence(submitted, legal_keys)
    old_by_id = {section.id: section for section in previous}
    merged: List[OutlineSection] = []
    overflow: Dict[str, List[str]] = {}
    removed = 0
    for section in bound:
        old = old_by_id.get(section.id)
        old_keys = list(old.evidence_keys) if old is not None else []
        remove_keys = set(section.remove_evidence_keys)
        retained = [key for key in old_keys if key not in remove_keys]
        removed += len(old_keys) - len(retained)
        keys = retained
        for key in section.evidence_keys:
            # 显式删除优先于同一提交里的重新添加,避免一个自相矛盾的载荷靠字段
            # 解析顺序得到不同结果。
            if key in remove_keys or key in keys:
                continue
            if len(keys) >= _OUTLINE_MAX_EVIDENCE:
                overflow.setdefault(section.id, []).append(key)
                continue
            keys.append(key)
        merged.append(OutlineSection(
            id=section.id,
            title=section.title,
            parent=section.parent,
            evidence_keys=keys,
        ))
    return merged, dropped, overflow, removed


def carry_outline_overflow(
    previous: Dict[str, List[str]],
    submitted: List[OutlineSection],
    merged: List[OutlineSection],
    current: Dict[str, List[str]],
    *,
    max_pending_evidence: int = _OUTLINE_MAX_PENDING_EVIDENCE,
) -> Dict[str, List[str]]:
    """把未接纳 key 当作持久服务端状态,而不是一次性诊断。

    pending key 只有三种退出方式:本次已经成功绑定;模型在 ``remove_evidence``
    里显式点名放弃;或整节从全量结构中删除。模型只腾出旧键却漏抄待换入 key 时,
    pending 必须继续留到下一轮/终态 trace,否则又回到了静默丢新证据。
    """
    submitted_by_id = {section.id: section for section in submitted}
    merged_by_id = {section.id: section for section in merged}
    carried: Dict[str, List[str]] = {}
    for section_id in merged_by_id:
        section = submitted_by_id.get(section_id)
        removed = set(section.remove_evidence_keys) if section is not None else set()
        accepted = set(merged_by_id[section_id].evidence_keys)
        keys: List[str] = []
        for key in list(previous.get(section_id, [])) + list(current.get(section_id, [])):
            if key in accepted or key in removed or key in keys:
                continue
            keys.append(key)
        if len(keys) > max_pending_evidence:
            # 解析层每次最多 8 key、状态机最多 6 次常规更新 + 1 次专用纠错;
            # 超过只能说明这两个结构上限被改坏。响亮失败胜过截断 pending 再静默丢 key。
            raise AssertionError("outline pending evidence bound exceeded")
        if keys:
            carried[section_id] = keys
    return carried


def outline_truncated_kg_evidence(
    sections: List[OutlineSection],
    collected: Dict[str, "RetrievedKnowledge"],
    top_hits: List["RetrievedKnowledge"],
    *,
    rescored: Dict[str, "RetrievedKnowledge"] = None,
    relevance_ceiling: float = None,
    retained_keys: Sequence[str] = (),
) -> List["RetrievedKnowledge"]:
    """大纲绑上、但没进最终相关性选集的知识对象。

    这一份是**按节合成(O2)的完整性补丁**,不是一条新的检索通道:`top_hits` 是
    `collected` 按相关度截到 `top_n` 的选集(穷尽档 cap 96),而绑定键的合法集合
    是**整个** `collected` —— 模型完全可以在第 3 轮把一个当时排在前面、最终被
    挤出选集的对象绑给某一节。缺了这一份,那一节的切片会凭空少掉一条它自己
    指名要的证据,而且悄无声息。

    **零查询**:三个候选池(`collected`/`elements`/`chunks`)在 run 内只增不减
    (见 `_window` 的注释),所以任何**曾经**合法的绑定键,在 run 收尾时仍然在池
    子里 —— 按 id 回库 hydrate 是一条永远走不到的分支,而 Ask 的合成路径上多一
    次数据库往返是要付钱的(运行效率是一等约束)。元素与原文段同理,由
    `ReasoningResult.elements`/`chunks` 原样带出,这里只补知识对象这一路。

    **相关度必须与 `top_hits` 同口径**,否则这份补集会变成一条抬分的后门:
    `collected[oid]` 里存的是**首次收下它的那个产出方**给的分,而 `top_hits` 的每
    一条都经过了对**整个问题**的重排。一个首收 0.95、重排 0.12 的对象若带着 0.95
    进 `classify_evidence` 的证据池,它一条就能把整篇答案抬到 grounded ——而它恰恰
    是被截断掉的那种"其实不相关"。两条路径各自校正,都不发新查询:

    * ``rescored`` —— 非 quota 路径手里现成的 `retrieve_scored` 结果映射,与
      `top_hits` 用的是**同一个** map、同一句 `.get(oid, rk)`,所以补集与选集逐条
      同口径(不在 map 里的沿用原对象,与 top_hits 对自己成员的处理完全一致)。
    * ``relevance_ceiling`` —— quota 路径没有全局重排 map(它按子查询配额融合),
      于是把带出值**夹到选集的最低分**:补集是被选集挤出去的那一批,不可能比选集
      里最差的一条更相关。选集为空时天花板取 0.0,补集一条都抬不动 —— 那正是
      "没有任何排名证据"该有的结果。

    返回按大纲顺序去重的列表,并排除已在 `top_hits` 里的对象 —— 调用方把它当成
    `top_hits` 的补集使用(合并时 `top_hits` 的重排分数优先)。
    """
    # Analysis dependencies need the same closing scores even without an
    # outline. This adds no lookup and leaves the selected set unchanged.
    keys = [key for section in (sections or ()) for key in section.evidence_keys]
    keys.extend(retained_keys)
    present = {hit.object_id for hit in top_hits}
    extra: List["RetrievedKnowledge"] = []
    for key in keys:
        if key in present or key not in collected:
            continue
        present.add(key)
        hit = collected[key]
        if rescored is not None:
            hit = _rescored_keeping_origin(rescored.get(key), hit)
        elif relevance_ceiling is not None and hit.relevance > relevance_ceiling:
            hit = replace(hit, relevance=relevance_ceiling)
        extra.append(hit)
    return extra


def outline_signature(sections: List[OutlineSection]) -> tuple:
    """大纲的可比较快照。用于判定一次 update_outline 是否**实质**改变了大纲——
    逐字重提同一份大纲不算进展(那正是需要被 stale 熔断兜住的空转),而增删节、
    改标题、改层级或补上一个绑定都算。"""
    return tuple(
        (s.id, s.title, s.parent, tuple(s.evidence_keys)) for s in sections
    )


def _outline_note(
    sections: List[OutlineSection],
    updates_left: int,
    overflow: Optional[Dict[str, List[str]]] = None,
    overflow_repair_available: bool = False,
    repair_only: bool = False,
    kg_gap_segment: str = "",
) -> str:
    """把大纲便签回喂给 reflect(镜像 visited / attempted / 枚举三份账目)。

    **这一份必须携带整份大纲的内容,而不只是账目**,理由是机制性的:reflect 的
    prompt 没有对话历史(每轮都是一条全新的 user message),而章节结构是全量替换——
    模型手上唯一一份「我上次交了什么」就是这段回喂。证据遗漏已有服务端 union
    保底,但只报节数与空节名仍会让模型无法保留/调整原来的章节结构。

    有界性仍然成立,且是**常数级**:12 节 × (32 id + 60 标题 + 32 parent +
    8 × 48 证据 key) + 固定文案 ≈ 6.5KB;pending 虽可在服务端累积至每节 56 个,
    每轮只展示前 8 个加剩余计数,所以 prompt 仍至多多带 12 × 8 个 key。
    总量与语料规模无关。真实 id 约 15–36 字符,
    典型值在 1–3KB。这只在 exhaustive 档出现,那一档的证据预算是 16k/120k 字符。

    ``updates_left`` 是本 run 还剩几次 update_outline(照集合地图那条剩余额度行的
    先例)。额度耗尽后措辞整段换成「已定稿、别再提交」:便签本身就是模型下一步动作
    的依据,它若还在说「再次提交时要带上……」,模型就会照做、撞上 outline_budget、
    白烧一轮反思——账目必须描述**现在**能做什么,而不是当初能做什么。

    ``kg_gap_segment`` 是已渲染好的弱支撑边提示段(见 `kg_gap_note_segment`),
    默认空串 —— 关闭态与无候选时本函数的输出逐字回到接入前。
    """
    if not sections:
        return ""
    lines = []
    for section in sections:
        parent = f"(隶属 {section.parent})" if section.parent else ""
        keys = "、".join(section.evidence_keys) if section.evidence_keys else "(无)"
        lines.append(f"- {section.id}「{section.title}」{parent} 证据: {keys}")
    empty = [section.title for section in sections if not section.evidence_keys]
    tail = (
        "\n无绑定证据的节: "
        + "、".join(f"「{title}」" for title in empty)
        + "。这些节就是下一步该定向检索的方向。"
        if empty else ""
    )
    overflow_lines = []
    by_id = {section.id: section for section in sections}
    for section_id, keys in (overflow or {}).items():
        section = by_id.get(section_id)
        if section is None or not keys:
            continue
        visible = list(keys[:_OUTLINE_MAX_EVIDENCE])
        remaining = len(keys) - len(visible)
        overflow_lines.append(
            f"- {section_id}「{section.title}」未接纳新证据: "
            + "、".join(visible)
            + (
                f"（另有 {remaining} 条待处理,处理或放弃这批后继续显示）"
                if remaining else ""
            )
        )
    overflow_note = ""
    if overflow_lines:
        overflow_note = (
            "\n当前因每节最多 8 条而未接纳的新证据(旧绑定已优先保留):\n"
            + "\n".join(overflow_lines)
        )
        if repair_only:
            overflow_note += (
                "\n本次纠错只能在该节 remove_evidence 中点名要移除的旧 key,"
                "并继续把要加入的 key 放在 evidence;如要明确放弃某个未接纳 key,"
                "也在 remove_evidence 中点名它。"
            )
        elif updates_left > 0 or overflow_repair_available:
            overflow_note += (
                "\n如要换入它们,下一次在该节 remove_evidence 中点名要移除的旧 key,"
                "并继续把要加入的 key 放在 evidence;如要明确放弃某个未接纳 key,"
                "也在 remove_evidence 中点名它。"
            )
        else:
            overflow_note += (
                "\n本 run 的大纲整理与纠错资格均已用完,不要再提交 update_outline;"
                "未接纳 key 会在收尾轨迹中披露。"
            )
    if repair_only and overflow_lines:
        head = (
            "（本轮大纲便签(这是终态溢出纠错轮,仅可提交一次章节 "
            "id/标题/parent 完全不变的 update_outline,不得改结构或执行检索):\n"
        )
    elif updates_left <= 0 and overflow_lines and overflow_repair_available:
        head = (
            "（本轮大纲便签(常规整理次数已用完;因上一份有未接纳证据,仅可再提交一次"
            "章节 id/标题/parent 完全不变的 update_outline,用 remove_evidence 腾位并"
            "换入点名的新 key):\n"
        )
    elif updates_left <= 0:
        head = (
            "（本轮大纲便签(已定稿:整理次数已用完,不要再提交 update_outline;"
            "请改用检索动作补齐空节,或直接作答):\n"
        )
    else:
        head = (
            "（本轮大纲便签。update_outline 会整体替换章节结构,再次提交时必须带上仍要"
            "保留的每一节;相同 id 节的 evidence 与旧绑定取并集,遗漏不会删除旧证据,"
            "要删除请在该节 remove_evidence 中点名;"
            f"还可以再整理 {updates_left} 次:\n"
        )
    if repair_only:
        # 终态纠错轮不给弱支撑边提示。这一段的头行写着「可用 add_subquery/
        # follow_chain 定向补证」,而同一份便签的开头写着「不得改结构或执行检索」
        # —— 两句话直接打架,而模型照着后写的那句做的话,这一轮会以
        # `outline_overflow_repair_declined` 收场:纠错资格白烧一次,未接纳的
        # key 一个都没换进来。这里只挡这一轮:`updates_left<=0` 的定稿轮与
        # cap-repair 邀请轮都仍然允许(也鼓励)检索动作,提示在那两处照常出现。
        kg_gap_segment = ""
    return head + "\n".join(lines) + tail + overflow_note + kg_gap_segment + "）"


# --------------------------------------------- 大纲采用引导(设计文档 §3.1.1)
#
# 真机动机(穷尽档三次 run、2 个库 3 个问题,update_outline 采用率 0/3)。最有说服
# 力的一次:84 篇来源的库 + 「综述这个 notebook 里的文章:每篇的核心贡献是什么,
# 它们之间有什么联系与分歧?」,模型先用 enumerate(collection=sources)**一次性
# 完整列出 84 篇**(complete=true),随后连开 4 次 PPR 找核心贡献、撞上 PPR 次数
# 上限,最后自述「虽然枚举了所有 84 篇文章标题,但多次子查询未返回具体每篇文章
# 的核心贡献,现有候选无法覆盖全部文章」并作答。它手里有完整清单,缺的恰恰是
# 「把清单变成结构、再逐节补证」——也就是大纲。
#
# prompt 里的动作说明讲清了大纲**是什么**、什么场合适用,却从不在模型真的拿到
# 清单的那一刻说一句「现在正是时候」。这一行补的就是那个时刻,而且是**账目**不是
# 命令:与 visited / attempted / 枚举三份回喂同区位、同形态,只陈述服务端手上现成
# 的确定性事实(已完整列出 N 篇 / 有 M 个已确认方向),并显式给出「判断不需要就
# 忽略」的出口。零新查询、零新模型调用、零新动作。
#
# 每 run 至多 2 轮:账目要教新东西,重复喊话只烧 prompt 预算——模型看过两次仍不
# 采用,就是它判断这题不需要大纲,那正是出口存在的意义。
_OUTLINE_NUDGE_MAX_ROUNDS = 2
# 触发引导的最小规模。1 篇文档 / 1 个方向的问题一次合成就答完了,给它建大纲纯属
# 噪音(prompt 自己也写着「Do NOT open an outline for a single-fact question」)。
_OUTLINE_NUDGE_MIN_ITEMS = 2


def _outline_nudge_note(
    sections: List[OutlineSection],
    enum_chains,
    direction_count: int,
    *,
    active: bool,
    nudges_used: int,
) -> str:
    """「该开大纲却还没开」时的一行引导;空串 = 本轮不引导。

    出现条件全部成立才渲染:门开着(``active``,即 `outline_wiring_active`)、大纲
    **仍为空**(一旦模型建了大纲,`_outline_note` 接管整段区位,引导就成了噪音)、
    本 run 引导次数未用尽,且存在下列结构性理由之一——

    * (a) 本 run 已把**来源清单**完整列完(`state == "complete"`)且条数 ≥ 2;
    * (b) 已确认意图给出了 ≥ 2 个必答检索方向。

    两条都成立时优先用 (a):它更具体,而且带着一个真实条数——「84 篇」比「若干个
    方面」更能让模型看出把清单摊成章节的性价比。

    ``state == "complete"`` 是硬判据,不能放宽成「列过就算」:半份清单变成的大纲
    天然缺节,而模型此刻并不知道自己缺了什么;引导它按一份没列完的目录定稿,比
    不引导更糟。条数取各条来源清单链里的最大值(限定单源与全作用域是两条独立的
    链),`getattr` 兜底是防御性的——覆盖率字段缺失时按 0 处理,即不引导。

    ``direction_count`` 由调用方从 run() 手上现成的已确认方向清单算出(见调用点),
    这里不做任何解析,也不新增任何查询。
    """
    if not active:
        return ""
    if sections:
        return ""
    if nudges_used >= _OUTLINE_NUDGE_MAX_ROUNDS:
        return ""
    listed = 0
    for chain in (enum_chains or {}).values():
        outcome = getattr(chain, "outcome", None)
        if outcome is None or getattr(outcome, "collection", "") != "sources":
            continue
        if getattr(chain, "state", "") != "complete":
            continue
        returned_total = getattr(
            getattr(outcome, "coverage", None), "returned_total", 0
        )
        listed = max(listed, int(returned_total or 0))
    if listed >= _OUTLINE_NUDGE_MIN_ITEMS:
        basis = f"已完整列出 {listed} 篇文档"
    elif direction_count >= _OUTLINE_NUDGE_MIN_ITEMS:
        basis = f"本题有 {direction_count} 个已确认的必答方向"
    else:
        return ""
    return (
        f"（本轮尚未建立大纲。{basis}:把它们变成 update_outline 的章节、"
        "再逐节补证,通常比反复全库检索更省轮次;若判断一次即可答完,忽略本行。）"
    )


