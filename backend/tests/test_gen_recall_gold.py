from app.eval.retrieval_metrics import leakage_ratio
import json
import pytest


def test_leakage_ratio_high_when_question_quotes_source():
    # 问题逐字复用源文本 → 高泄漏
    r = leakage_ratio("regulated cascode adds a gain stage",
                      "regulated cascode adds a gain stage to boost output resistance")
    assert r > 0.8


def test_leakage_ratio_low_when_paraphrased():
    r = leakage_ratio("如何在不堆叠太多管子的前提下进一步提高输出阻抗?",
                      "regulated cascode adds a gain stage to boost output resistance")
    assert r < 0.3


@pytest.mark.parametrize("question", [["question"], {"text": "question"}, True, 7, None])
def test_generated_question_rejects_non_text(question):
    from app.scripts.gen_recall_gold import _gen_question

    class Client:
        def chat_json(self, messages, schema_hint):
            return json.dumps({"question": question})

    assert _gen_question(Client(), "source") == ""


def test_generated_question_preserves_text():
    from app.scripts.gen_recall_gold import _gen_question

    class Client:
        def chat_json(self, messages, schema_hint):
            return json.dumps({"question": " 工程问题？ "})

    assert _gen_question(Client(), "source") == "工程问题？"
