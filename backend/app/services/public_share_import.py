"""Build a self-contained source from the public projection, never private rows."""
from __future__ import annotations

import base64
import re
from typing import Callable

from markdown_it import MarkdownIt

from app.domain.share_import import MANIFEST_VERSION, encode_manifest
from app.services.citation_markers import MARKER_RE, marker_keys

_NUMERIC_MARKER = re.compile(r"\[(\d+(?:\s*[,，、]\s*\d+)*)\]")
_VERIFICATION_LABELS = {"changed": "原文已改动", "source_gone": "资料已删除", "unverifiable": "无法核对"}
_EVIDENCE_LABELS = {"grounded": "有据", "inferred": "推断", "overview": "概述"}


def _namespace_body(body: str, keys: dict[str, str]) -> str:
    # Markdown blocks and inline code/math are literal content, not citations.
    lines = body.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    protected = []
    for token in MarkdownIt("commonmark").parse(body):
        if token.type in {"fence", "code_block", "html_block"} and token.map:
            protected.append((offsets[token.map[0]], offsets[token.map[1]]))
    inline_literal = re.compile(r"(`+)[\s\S]*?\1|\$\$[\s\S]*?\$\$|(?<!\\)\$[^$\n]+(?<!\\)\$")

    def replace(segment: str) -> str:
        def convert(match: re.Match) -> str:
            cited = marker_keys(match[0])
            if not cited or any(key not in keys for key in cited):
                return match[0]
            return "".join(f"[{keys[key]}]" for key in cited)
        segment = MARKER_RE.sub(convert, segment)
        def numeric(match: re.Match) -> str:
            cited = re.split(r"\s*[,，、]\s*", match[1])
            return "".join(f"[{keys[key]}]" for key in cited) if all(key in keys for key in cited) else match[0]
        return _NUMERIC_MARKER.sub(numeric, segment)

    protected.extend((match.start(), match.end()) for match in inline_literal.finditer(body))
    merged = []
    for start, end in sorted(protected):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    result = []
    cursor = 0
    for start, end in merged:
        result.append(replace(body[cursor:start]))
        result.append(body[start:end])
        cursor = end
    result.append(replace(body[cursor:]))
    return "".join(result)


def build_saved_share(
    payload: dict, *, kind: str, title: str, turn_index: int | None = None,
    read_image: Callable[[str], tuple[str, bytes]] | None = None,
    max_bytes: int | None = None, max_images: int | None = None,
) -> bytes:
    """Stable bytes make upload content-hash reuse apply to repeated saves."""
    if kind == "reports":
        turns = [{"question": payload.get("question", ""),
                  "answer_md": payload.get("content_md", ""),
                  "answered_at": payload.get("updated_at", ""),
                  "references": payload.get("references", []),
                  "question_truncated": payload.get("question_truncated") is True,
                  "truncated_references": payload.get("truncated_references") is True}]
    else:
        turns = payload.get("turns", [])
    indexed = list(enumerate(turns))
    if turn_index is not None:
        if kind != "conversations" or turn_index < 0 or turn_index >= len(turns):
            raise ValueError("请选择分享页中存在的问答。")
        indexed = [(turn_index, turns[turn_index])]
    if not indexed:
        raise ValueError("分享内容为空，无法保存。")
    manifest = {"version": MANIFEST_VERSION, "kind": kind, "title": title, "turns": []}
    sections = [f"# {title}", "来自分享回答。以下内容为 AI 生成的回答及其公开引用证据，未导入原始文献全文。"]
    if payload.get("truncated_turns"):
        sections.append("会话过长，本副本只包含分享页当前公开的问答。")
    image_count = sum(len(turn.get("images", [])) for _index, turn in indexed)
    if max_images is not None and image_count > max_images:
        raise ValueError("分享图片数量超过当前保存上限，请联系管理员调整设置。")
    for index, turn in indexed:
        namespace = f"t{index + 1}-"
        refs = [{**ref, "original_key": ref["key"], "key": namespace + ref["key"]}
                for ref in turn.get("references", [])]
        for ref in refs:
            if ref.get("verification"):
                ref.pop("evidence", None)
        mapping = {ref["original_key"]: ref["key"] for ref in refs}
        manifest["turns"].append({"question": turn.get("question", ""),
                                  "asked_at": turn.get("asked_at", ""),
                                  "answered_at": turn.get("answered_at", ""),
                                  **{flag: True for flag in ("question_truncated", "truncated_references")
                                     if turn.get(flag) is True},
                                  "references": refs})
        sections.extend([f"## 问题 {index + 1}", turn.get("question", ""),
                         "回答时间：" + (turn.get("answered_at") or "未记录"),
                         _namespace_body(turn.get("answer_md", ""), mapping)])
        if turn.get("evidence_level") in _EVIDENCE_LABELS:
            sections.append("回答依据：" + _EVIDENCE_LABELS[turn["evidence_level"]])
        if turn.get("completeness_notice"):
            sections.append(str(turn["completeness_notice"]))
        if turn.get("omitted_result_sets"):
            sections.append("此轮部分结果卡未包含在分享页中，本副本也未保存这些结果卡。")
        if turn.get("truncated_references"):
            sections.append("分享页的引用清单不完整，本副本仅保存已公开的引用。")
        if turn.get("question_truncated"):
            sections.append("研究问题过长，已截断；本副本保留分享页公开的问题。")
        for image in turn.get("images", []):
            if read_image is None:
                raise ValueError("图片无法保存，请刷新分享页后重试。")
            mime, content = read_image(image["alias"])
            # Alt is generated from public citation keys, never unescaped caption Markdown.
            label = "、".join(mapping[key] for key in image.get("reference_keys", []) if key in mapping)
            sections.append(f"![分享引用图片 {label}](data:{mime};base64,{base64.b64encode(content).decode()})")
            if image.get("caption"):
                sections.append("> **图片描述**\n> " + str(image["caption"]).replace("\n", "\n> "))
            if max_bytes is not None and sum(len(section.encode()) for section in sections) > max_bytes:
                raise ValueError("分享内容超过单个文件的保存上限，请按单轮保存或联系管理员。")
        sections.append("### 引用证据")
        for ref in refs:
            sections.extend([f"#### [{ref['key']}] {ref.get('title') or ref.get('file_name') or '未命名资料'}",
                             ref.get("location", ""), ref.get("snippet", "")])
            verification = ref.get("verification")
            if verification:
                reason = _VERIFICATION_LABELS.get(verification, "无法核对")
                sections.append(f"此引用在回答生成时未通过核对：{reason}。以下摘录仅记录回答时的内容。")
            evidence = ref.get("evidence") if not verification else None
            if evidence:
                label = {"original": "原文及上下文", "knowledge": "知识解释", "memory": "作者的个人记忆"}.get(evidence.get("kind"), "引用内容")
                sections.extend([label + "：", evidence.get("text", "")])
                if evidence.get("truncated"):
                    sections.append("原文上下文过长，此处仅保存分享页公开的部分。")
            else:
                sections.append("原文已不可用，保留已有摘录。")
    content = (encode_manifest(manifest) + "\n\n".join(sections) + "\n").encode("utf-8")
    if max_bytes is not None and len(content) > max_bytes:
        raise ValueError("分享内容超过单个文件的保存上限，请按单轮保存或联系管理员。")
    return content
