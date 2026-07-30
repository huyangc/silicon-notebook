"""按节合成的确定性部分(PR-3 O2,设计文档 §3.1)。

这里只有纯逻辑:把 O1 产出的终态大纲切成「每节一份证据」,给每节分配一段互不
相交的 `[k]` 命名空间,再把各节文本拼回一篇答案。模型调用留在
``ask_service._answer_reasoning_sections`` —— 这样切片/偏移/拼接可以脱开
AskService、脱开模型替身单测,而「哪一节失败要整体回退」那条控制流留在它真正
发生的地方。

DualGraph 借鉴的产出侧动机:一次性把全部证据喂给合成模型会 lost-in-the-middle,
按节喂只让模型在写这一节时看见这一节绑上的证据。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


# 每节的 `[k]` 编号基址步长。合成上下文里既有的分区基址(chunk 0 / KG 1000 /
# chains 2000 / Memory 3000 / element 4000 / 集合清单 5000)全部 < 6000,所以
# 「第 i 节整体加 i × 10000」保证任意两节的 key 号段不相交:合并后的锚点集合里
# 一个 `[k]` 只可能来自一节。
#
# 有界性:大纲封顶 12 节(`_OUTLINE_MAX_SECTIONS`),最大基址 110000,而 key 号
# 只在 prompt 文本与锚点 key 里出现,没有任何上限约束。
#
# 步长若被去掉(或缩到既有基址之内),第 2 节的 chunk 就会和第 1 节的 chunk 抢
# `k1`,合并锚点时后者覆盖前者——答案里两处不同的证据指向同一条来源,而且不会
# 报错,只会静静地把引用指错。回归门见 test_reasoning_outline_synthesis.py。
OUTLINE_SECTION_KEY_STRIDE = 10000


@dataclass
class OutlineSectionSlice:
    """一节及其绑定证据的切片(装配一次合成调用所需的全部输入)。"""

    section: Any                    # reasoning_retrieval.OutlineSection
    index: int                      # 在**实际合成**序列里的 0 基序号
    key_offset: int
    hits: list = field(default_factory=list)        # RetrievedKnowledge
    elements: list = field(default_factory=list)    # RetrievedElement
    chunks: list = field(default_factory=list)      # RetrievedChunk

    @property
    def evidence_count(self) -> int:
        return len(self.hits) + len(self.elements) + len(self.chunks)


def plan_outline_sections(
    sections: Sequence[Any],
    *,
    kg_by_id: Mapping[str, Any],
    element_by_id: Mapping[str, Any],
    chunk_by_id: Mapping[str, Any],
) -> tuple[list[OutlineSectionSlice], list[str]]:
    """把终态大纲切成 (可合成的切片, 被跳过的节标题)。

    绑定键的三个来源(知识对象 id / 元素 id / 原文段 id)是互不相交的代理 id 空间
    (128 位随机 + 各自前缀),所以按 map 依次查找不会误判;查不到的键只可能是本轮
    池子里已经没有的东西,静默略过 —— O1 的服务端校验保证键在**绑定那一刻**合法,
    而三个候选池只增不减,所以正常情况下每个键都查得到。

    **空节保留为跳过项而不是错误**:空节是「问到了但还没找到」的诚实记录(O1 合同),
    它没有可写的证据,硬要合成只会让模型凭常识编一节。被跳过的标题回到 trace,
    用户才看得见「这一节没写」。

    绑定键解析后仍没有任何证据的节同样跳过:那意味着这一节实际上是空的,与
    `evidence_keys` 为空等价。
    """
    slices: list[OutlineSectionSlice] = []
    skipped: list[str] = []
    for section in sections:
        hits: list = []
        elements: list = []
        chunks: list = []
        for key in getattr(section, "evidence_keys", ()) or ():
            if key in kg_by_id:
                hits.append(kg_by_id[key])
            elif key in element_by_id:
                elements.append(element_by_id[key])
            elif key in chunk_by_id:
                chunks.append(chunk_by_id[key])
        if not (hits or elements or chunks):
            skipped.append(str(getattr(section, "title", "")))
            continue
        slices.append(OutlineSectionSlice(
            section=section,
            index=len(slices),
            key_offset=len(slices) * OUTLINE_SECTION_KEY_STRIDE,
            hits=hits, elements=elements, chunks=chunks,
        ))
    return slices, skipped


def outline_answer_text(rendered: Sequence[tuple[OutlineSectionSlice, str]]) -> str:
    """按大纲顺序拼接各节文本,顶层节 ``## 标题``、子节 ``### 标题``。

    标题由服务端加,不由模型写(每节的 prompt 明确要求「不要重复标题」):模型自己
    写标题时层级、措辞、加不加编号都随机,而这里的层级必须与大纲的 parent 关系
    一致。Markdown 标题由前端 react-markdown 原生渲染成 h2/h3。
    """
    blocks: list[str] = []
    for item, text in rendered:
        level = "###" if getattr(item.section, "parent", "") else "##"
        title = str(getattr(item.section, "title", "")).strip()
        body = (text or "").strip()
        blocks.append(f"{level} {title}\n\n{body}".strip())
    return "\n\n".join(blocks)
