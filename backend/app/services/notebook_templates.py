"""Preset notebook templates (产品方案 §6.2).

Each template seeds purpose / domain / target users / expected questions /
source types / taxonomy so a new notebook starts with structured guidance
instead of blank. Applied at creation time when `NotebookCreate.template` is set;
explicitly-provided fields on the request always win over template defaults.
"""

from __future__ import annotations

from typing import Dict, List

NOTEBOOK_TEMPLATES: List[Dict[str, object]] = [
    {
        "id": "rule",
        "label": "规则库 Rule Notebook",
        "purpose": "沉淀某领域的设计规则（must/should/不得），可检索、可引用、可治理。",
        "primary_domain": "Semiconductor",
        "target_users": "设计 / 版图 / 封装工程师",
        "expected_questions": ["这个场景下有哪些必须遵守的规则？", "违反会有什么风险？"],
        "source_types": ["设计规范", "review checklist", "经验总结"],
        "taxonomy": ["噪声", "ESD", "封装", "电源", "时钟"],
    },
    {
        "id": "method",
        "label": "方法库 Method Notebook",
        "purpose": "收集分析/验证方法与最佳实践：何时用、收益、局限。",
        "primary_domain": "Semiconductor",
        "target_users": "工程师 / 新人",
        "expected_questions": ["这个问题该用什么方法？", "该方法的局限是什么？"],
        "source_types": ["方法文档", "流程 SOP", "技术分享"],
        "taxonomy": ["仿真", "测量", "调试", "建模"],
    },
    {
        "id": "case",
        "label": "案例库 Case Notebook",
        "purpose": "记录历史 debug 案例：症状、背景、根因、解决、经验。",
        "primary_domain": "Semiconductor",
        "target_users": "全体研发",
        "expected_questions": ["有没有类似的问题案例？", "根因和解决办法是什么？"],
        "source_types": ["debug 报告", "失效分析", "复盘记录"],
        "taxonomy": ["噪声", "可靠性", "良率", "信号完整性"],
    },
    {
        "id": "review",
        "label": "评审库 Review Notebook",
        "purpose": "围绕设计评审组织场景化 checklist 与签核证据。",
        "primary_domain": "Semiconductor",
        "target_users": "评审专家 / 项目组",
        "expected_questions": ["这次评审要检查哪些项？", "每项需要什么证据？"],
        "source_types": ["review checklist", "评审记录", "规范"],
        "taxonomy": ["package review", "tapeout", "DFM", "DFT"],
    },
    {
        "id": "article",
        "label": "文献库 Article Notebook",
        "purpose": "把技术文章/论文的 claim 与现有规则关联，提炼派生规则候选。",
        "primary_domain": "Semiconductor",
        "target_users": "技术专家",
        "expected_questions": ["这篇文章对我们的规则有什么影响？", "能否提炼成新规则？"],
        "source_types": ["论文", "技术文章", "白皮书"],
        "taxonomy": ["机理", "实验结果", "对比"],
    },
    {
        "id": "general",
        "label": "通用 General Notebook",
        "purpose": "",
        "primary_domain": "Semiconductor",
        "target_users": "",
        "expected_questions": [],
        "source_types": [],
        "taxonomy": [],
    },
]


def get_template(template_id: str) -> Dict[str, object] | None:
    for template in NOTEBOOK_TEMPLATES:
        if template["id"] == template_id:
            return template
    return None
