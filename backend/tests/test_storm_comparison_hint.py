"""Task 9: 报告 STORM 大纲规划提示——对比题应规划一节横向对比。

对比/广度类报告题(X vs 其他 Y)需要一节专门的横向对比,其 sub_queries
面向同类实体的对应维度;该节深挖时由 reasoning 的 expand_community 落地。
本测试只约束 prompt 文本含相应规划指令。
"""
from app.services.prompts import report_storm_outline_prompt


def test_storm_prompt_mentions_comparison_section():
    p = report_storm_outline_prompt("分析X相比其他Y", "corpus", max_sections=6)
    assert "横向对比" in p or "cross-model" in p.lower() or "compare" in p.lower()


def test_storm_prompt_still_includes_question_and_corpus():
    """回归护栏:新增指令不破坏既有 prompt 结构(问题/语料仍在)。"""
    p = report_storm_outline_prompt("分析X相比其他Y", "MY_CORPUS_MARK", max_sections=4)
    assert "分析X相比其他Y" in p
    assert "MY_CORPUS_MARK" in p
