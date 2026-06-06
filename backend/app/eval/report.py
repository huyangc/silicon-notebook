"""把指标 dict 渲染为 markdown 报告(纯函数)。"""
from __future__ import annotations
from typing import Dict, List


def _h(s: str) -> str:
    return f"\n## {s}\n"


def render_quality_report(per_book: Dict[str, dict]) -> str:
    out: List[str] = ["# KG 抽取质量报告", "",
                      "> 探针给的是**疑似信号**,非定论;含合理变体误报,真噪声率需抽样校准(见 spec §4.4)。"]
    for book, by_type in per_book.items():
        out.append(_h(f"书:{book}"))
        cm = by_type.get("concept", {})
        out.append(f"- concept 总数:{cm.get('total', 0)}")
        out.append(f"- **疑似非原子率:{cm.get('suspect_non_atomic_rate', 0):.1%}** "
                   f"({cm.get('suspect_non_atomic', 0)} 个)")
        out.append(f"- 孤儿节点:{cm.get('orphans', 0)};取值枚举组:{cm.get('enumerated_groups', 0)};"
                   f"近重复组:{cm.get('near_duplicate_groups', 0)}")
        pc = cm.get("probe_counts", {})
        if pc:
            out.append("- 各探针命中:" + ", ".join(f"{k}={v}" for k, v in sorted(pc.items())))
        for other in ("claim", "formula", "procedure"):
            om = by_type.get(other)
            if om:
                out.append(f"- {other}:总数 {om.get('total', 0)},"
                           f"退化 {om.get('degraded', 0)}({om.get('degraded_rate', 0):.1%})")
        samples = cm.get("samples", {})
        for tag, items in sorted(samples.items()):
            if items:
                out.append(f"  - 样例[{tag}]:" + "; ".join(items[:20]))
        for mask, variants in list(cm.get("enumerated_samples", {}).items())[:20]:
            out.append(f"  - 枚举组「{mask}」:" + "; ".join(variants[:8]))
    return "\n".join(out) + "\n"
