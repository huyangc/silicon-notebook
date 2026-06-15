"""把存库的 source_elements 合并成检索用 chunk(~600字)。纯函数无 IO。
碎 element(47%<150字)直接做检索单元噪声大;此处贪心合并相邻 prose 到目标字数,
heading 作 section 标签(切 chunk 边界 + 拼进文本帮助语义),跳过 image/空。
借鉴 kg/windowing 的贪心打包,但在 element 粒度上做(检索 chunk 比 KG 抽取窗口小)。"""
from __future__ import annotations
from typing import Dict, List

_SKIP_TYPES = {"image", "figure"}


def build_chunks(elements: List[dict], target_chars: int = 600,
                 overlap_chars: int = 0) -> List[Dict]:
    """elements: 有序 [{"id","element_type","text"}]。
    返回 [{"text","section_path","element_ids"}]。overlap_chars 预留(P1 默认 0)。"""
    chunks: List[Dict] = []
    section = ""
    buf: List[tuple] = []   # [(id, text)]
    buf_len = 0

    def flush():
        nonlocal buf, buf_len
        if buf:
            body = "\n".join(t for _, t in buf)
            text = f"[{section}] {body}" if section else body
            chunks.append({"text": text, "section_path": section,
                           "element_ids": [i for i, _ in buf]})
        buf, buf_len = [], 0

    for e in elements:
        etype = (e.get("element_type") or e.get("type") or "").lower()
        text = (e.get("text") or "").strip()
        if etype == "heading":
            flush()                 # heading 切边界
            section = text          # 更新 section 标签
            continue
        if etype in _SKIP_TYPES or not text:
            continue                # 跳过 image/空
        buf.append((e["id"], text))
        buf_len += len(text)
        if buf_len >= target_chars:
            flush()
    flush()
    return chunks
