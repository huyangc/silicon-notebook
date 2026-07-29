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
    section = ""     # full breadcrumb → section_path column (labels, section grouping)
    prefix = ""      # bounded tail → the `[...]` prefix inside the SCORED text
    buf: List[tuple] = []   # [(id, text)]
    buf_len = 0

    def flush():
        nonlocal buf, buf_len
        if buf:
            body = "\n".join(t for _, t in buf)
            text = f"[{prefix}] {body}" if prefix else body
            chunks.append({"text": text, "section_path": section,
                           "element_ids": [i for i, _ in buf]})
        buf, buf_len = [], 0

    for e in elements:
        etype = (e.get("element_type") or e.get("type") or "").lower()
        text = (e.get("text") or "").strip()
        if etype == "heading":
            flush()                 # heading 切边界
            # 有面包屑(markdown 解析路径存的 section_path,含自身、" > " 分隔)就用它,
            # 避免子标题(如 Arguments/Examples)把上级标题(命令名)覆盖掉;没有(MinerU
            # heading、旧库存量行)回退标题自身文本 = 现状行为。空段(空标题/纯图片
            # 标题)剔除,防止悬挂的 "X > " 进标签或引用卡。
            # 标签与打分文本刻意解耦:section_path 列存完整面包屑,而拼进 chunk 文本
            # 的前缀只取尾部两段(父级 > 叶子)。keyword_score 是集合覆盖率、加词零
            # 稀释代价,完整面包屑会把文档名/章名 token 白送给整棵子树(真实库实测
            # 一个查询能让 187/187 全过相关性地板);KG 侧同款结论见 retrieval.py
            # 的 _PAYLOAD_SKIP_KEYS。父级+叶子已覆盖目标场景(命令名进 Arguments/
            # Examples 子块的可检索文本),更深的定位归 section_path 列与章节取齐。
            crumbs = [seg.strip() for seg in (e.get("section_path") or "").split(" > ")
                      if seg.strip()]
            if crumbs:
                section = " > ".join(crumbs)
                prefix = " > ".join(crumbs[-2:])
            else:
                section = prefix = text
            continue
        if (etype in _SKIP_TYPES and not e.get("caption")) or not text:
            continue                # 跳过无图注的 image/figure 与空文本；带图注的图保留图注进检索
        buf.append((e["id"], text))
        buf_len += len(text)
        if buf_len >= target_chars:
            flush()
    flush()
    return chunks
