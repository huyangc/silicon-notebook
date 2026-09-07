import json

import pytest

from app.core.config import Settings
from app.models.schemas import AskRequest, NotebookCreate
from app.models.source_scope import BaseNotebookScope, SourceScope
from app.services.document_overview import overview_intent
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_chat_client


@pytest.mark.parametrize("question,kind", [
    ("这个库中的文档分别介绍了什么内容", "catalog"),
    ("介绍一下这个notebook中的文章", "catalog"),
    ("介绍一下这个 Notebook 中的文章。", "catalog"),
    ("请逐篇介绍当前LIBRARY里的所有论文", "catalog"),
    ("这个NOTebook里有哪些文章？", "catalog"),
    ("给我简要介绍下这几篇文章", "catalog"),
    ("这些论文都主要讲了什么？", "catalog"),
    ("这个 notebook 中这篇论文介绍了什么内容", "source"),
    ("请介绍这个notebook中关于机器学习的论文", None),
    ("介绍一下这个notebook中的文章的实验结论", None),
    ("介绍一下 Jupyter notebook 的用法", None),
    ("介绍一下notebook中的文章提到的功耗预测方法", None),
    ("介绍一下这个notebook中的文章，不包括参考库", "catalog"),
    ("介绍一下这个notebook中的文章，包括参考库中关于量化的论文", None),
    ("介绍这篇文档，包括参考库", None),
    ("Summarize all papers, excluding reference libraries", "catalog"),
    ("只介绍这个notebook中关于量化的文章", None),
    ("介绍一下这个notebook中的文章，排除量化相关论文", None),
    ("介绍一下这个notebook中的文章，不包括参考库中关于量化的论文", None),
    ("介绍这个notebook和参考库中的文章，不含参考库", None),
    ("只介绍这个notebook中的文章，包括参考库", None),
    ("介绍这个notebook中的文章，包括参考库，不含参考库", None),
    ("介绍这个notebook中的文章，不含参考库，包括参考库", None),
    ("Summarize papers about quantization, excluding reference libraries", None),
    ("Summarize papers in this notebook and reference libraries, excluding reference libraries", None),
    ("Summarize papers about notebooks, including reference libraries", None),
    ("这篇文档介绍了什么内容", "source"),
    ("这个库中这篇文档介绍了什么内容", "source"),
    ("介绍《部署手册》", "source"),
    ("Summarize each document in this library", "catalog"),
    ("What is this paper about?", "source"),
    ("介绍一下Transformer的注意力机制", None),
    ("介绍一下“Transformer”的注意力机制", None),
    ("介绍《部署手册》中的配置步骤", None),
    ("介绍文档中的实验方法", None),
    ("介绍库中文档的认证原理", None),
    ("介绍论文中的“注意力机制”原理", None),
    ("库中文档介绍了哪些错误码", None),
    ("比较这些论文的方法", None),
    ("Summarize this paper's methodology", None),
    ("分别介绍《部署手册》和《运维手册》", None),
    ('Summarize the papers "Deployment" and "Operations"', None),
    ("Summarize the experimental methods in this paper", None),
    ("Summarize the key findings from the article", None),
    ("列出关于机器学习的论文", None),
    ("What documents cover OAuth token expiration?", None),
    ("Summarize the methodology of this paper", None),
    ("List documents about OAuth", None),
    ("Summarize all papers about machine learning", None),
    ("请介绍这个库中关于机器学习的论文", None),
    ("介绍这篇文档的实验结论", None),
    ("Summarize the methodology of the paper \"Deployment\"", None),
    ("介绍《部署手册》的认证流程", None),
    ("请简要介绍一下这篇论文的主要内容。", "source"),
    ("介绍一下这篇文档", "source"),
    ("该文档的主要内容是什么？", "source"),
    ("请逐篇介绍这个库中的文档", "catalog"),
    ("这些论文分别讲了什么？", "catalog"),
    ("文档分别介绍了什么内容？", "catalog"),
    ("请列出这个库中的所有文档", "catalog"),
    ("这个库里有哪些文章？", "catalog"),
    ("Please summarize this document.", "source"),
    ("Give me an overview of that article", "source"),
    ("What does this file cover?", "source"),
    ("Please list all documents in this notebook", "catalog"),
    ("What are the papers in this library about?", "catalog"),
    ("What does each document cover?", "catalog"),
    # 生产复现题(「说明」不在原动词表里,整题因此掉出目录通道)与它的同义写法。
    ("当前notebook的文章说明了什么？", "catalog"),
    ("这篇文章说明了什么", "source"),
    ("what do the papers in this notebook say", "catalog"),
    ("这些论文讲了些什么", "catalog"),
    ("这些论文说了什么", "catalog"),
    ("这些论文主要说什么", "catalog"),
    ("这篇文章的大意是什么", "source"),
    ("What are the papers in this library saying", "catalog"),
    ("What does this paper explain?", "source"),
    # 新动词也要在 source 通道上有正例:catalog 与 source 走的是同一张动词表、
    # 不同的主语模板,只钉 catalog 的话「单篇 + 新动词」可能在主语侧悄悄漏掉。
    ("文档描述了什么", "source"),
    ("这份文档阐述了什么内容", "source"),
    ("这篇论文讨论了什么", "source"),
    # 扩表不得放宽边界:动词后面跟话题修饰语的仍旧走 ranked。
    ("文章说明了 CMRR 如何计算", None),
    ("这篇文章说明的公式是什么", None),
    ("当前notebook里关于布局的文章讲了什么", None),
    ("这篇文章描述的版图约束有哪些", None),
    ("What does this paper say about CMRR?", None),
])
def test_overview_routes_only_explicit_document_introductions(question, kind):
    result = overview_intent(question)
    assert (result.kind if result else None) == kind


@pytest.mark.parametrize("question,title", [
    ("介绍《部署手册》", "部署手册"),
    ("介绍文档“部署手册”", "部署手册"),
    ("“部署手册”这篇文档讲了什么？", "部署手册"),
    ('Summarize the paper "Deployment"', "Deployment"),
    ('What is the document "Deployment" about?', "Deployment"),
    ("介绍《系统设计与评审》", "系统设计与评审"),
])
def test_overview_extracts_only_whole_document_title_subjects(question, title):
    result = overview_intent(question)
    assert result is not None and result.kind == "source"
    assert result.title == title


@pytest.mark.parametrize("question,include_references", [
    ("介绍一下这个notebook中的文章", False),
    ("介绍一下这个notebook中的文章，不包括参考库", False),
    ("介绍一下这个notebook中的文章，不包含挂载的参考库", False),
    ("介绍一下这个notebook中的文章，不含参考资料库", False),
    ("介绍一下这个notebook中的文章，排除公共知识库", False),
    ("仅介绍这个notebook中的文章", False),
    ("请只逐篇介绍当前NOTEBOOK中的文章", False),
    ("Summarize all papers, excluding reference libraries", False),
    ("Please only summarize all papers in this notebook", False),
    ("Please list all documents in this notebook, without its mounted reference libraries", False),
    ("Summarize each document in this notebook", False),
    ("介绍一下这个notebook中的文章，包括挂载的参考库", True),
    ("介绍一下这个notebook中的文章，包含公共知识库", True),
    ("请逐篇介绍当前notebook和参考库中的全部论文", True),
    ("介绍这个笔记本以及挂载参考资料库中的文章", True),
    ("请列出这个Notebook中的所有文档，包括参考库", True),
    ("Summarize each document in this notebook, including its mounted reference libraries", True),
    ("What are the papers in this notebook and its reference libraries about?", True),
    ("Please list all documents in this notebook, including reference libraries", True),
])
def test_catalog_reference_scope_requires_explicit_positive_request(question, include_references):
    result = overview_intent(question)
    assert result is not None and result.kind == "catalog"
    assert result.include_reference_libraries is include_references


class AnswerClient:
    configured = True
    model = "test"

    def __init__(self, answer="介绍依据 [k5001]。"):
        self.prompts = []
        self.answer = answer

    def chat_json(self, messages, *args, **kwargs):
        self.prompts.append(messages[0]["content"])
        if args and '"documents"' in args[0] and self.answer:
            return json.dumps({"documents": [{"reference": "k5001", "purpose": self.answer,
                                              "method": "", "contribution": ""}]})
        return json.dumps({"answer": self.answer, "grounded": True})


@pytest.fixture
def repo(tmp_path):
    return SQLiteRepository(Settings(
        _env_file=None, database_url=f"sqlite:///{tmp_path / 't.db'}",
        storage_dir=str(tmp_path / "s"), model_services_config="",
        llm_log_enabled=False, event_log_enabled=False,
        document_overview_max_elements=3,
    ))


def seed(repo, nb, source_id, title, summary, texts=()):
    now = "2026-09-07T00:00:00+00:00"
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,summary,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (source_id, nb, title, "markdown", "parsed", "parsed", summary, now, now),
        )
        for i, text in enumerate(texts):
            db.execute(
                "INSERT INTO source_elements (id,source_id,element_type,location_label,text,metadata,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (f"{source_id}-{i:03}", source_id, "paragraph", f"第{i+1}节", text,
                 json.dumps({"section_path": f"第{i+1}节"}), now),
            )


def ask(repo, nb, question, **kwargs):
    return repo._runtime.ask_component.ask(
        nb, AskRequest(question=question, mode="chunk", **kwargs),
        user_id=repo.current_user().id,
    )


def test_catalog_works_without_embeddings_or_kg_and_persists_coverage(repo):
    nb = repo.create_notebook(NotebookCreate(name="资料"))
    seed(repo, nb.id, "a", "部署手册", "介绍部署流程")
    seed(repo, nb.id, "b", "无摘要", "")
    client = AnswerClient()
    bind_chat_client(repo, "ask_answer", client)
    response = ask(repo, nb.id, "这个库中的文档分别介绍了什么内容")
    assert len(client.prompts) == 1
    assert "部署流程" in client.prompts[0] and "暂无已存摘要" in client.prompts[0]
    assert response.result_sets[0].coverage.complete
    assert response.result_sets[0].coverage.returned_total == 2
    assert response.anchors[0].source_id == "a"
    assert response.mode == "chunk" and response.answer_id
    assert "目录列全不等于已阅读全文" in response.answer
    with repo._connect() as db:
        saved = json.loads(db.execute("SELECT payload FROM answers WHERE id=?", (response.answer_id,)).fetchone()[0])
    assert saved["result_sets"][0]["coverage"]["complete"]


def test_selected_single_document_reads_end_and_excludes_other_source(repo):
    nb = repo.create_notebook(NotebookCreate(name="资料"))
    seed(repo, nb.id, "a", "部署手册", "", ["起点", "中间一", "中间二", "末尾结论"])
    seed(repo, nb.id, "b", "无关文档", "不应出现")
    client = AnswerClient("末尾结论 [k3]。")
    bind_chat_client(repo, "ask_answer", client)
    response = ask(repo, nb.id, "这篇文档介绍了什么内容", source_scope=SourceScope(mode="include", source_ids=["a"]))
    assert client.prompts, response.answer
    assert "末尾结论" in client.prompts[0] and "不应出现" not in client.prompts[0]
    assert "3/4" in response.answer and "不代表覆盖所有章节" in response.answer
    assert response.anchors[0].element_id == "a-003"
    assert response.anchors[0].notebook_id == ""
    assert all(c.source_id == "a" for c in response.citations)


def test_same_title_requires_selection_instead_of_guessing(repo):
    nb = repo.create_notebook(NotebookCreate(name="资料"))
    seed(repo, nb.id, "a", "手册", "")
    seed(repo, nb.id, "b", "手册", "")
    client = AnswerClient()
    bind_chat_client(repo, "ask_answer", client)
    response = ask(repo, nb.id, "介绍《手册》")
    assert not client.prompts
    assert "同名文档" in response.answer
    assert not response.citations and not response.anchors


def test_generic_question_does_not_match_a_title_by_substring(repo):
    nb = repo.create_notebook(NotebookCreate(name="资料"))
    seed(repo, nb.id, "a", "文档", "", ["不应被猜中"])
    seed(repo, nb.id, "b", "部署手册", "", ["另一篇文档"])
    client = AnswerClient()
    bind_chat_client(repo, "ask_answer", client)
    response = ask(repo, nb.id, "这篇文档介绍了什么内容")
    assert not client.prompts
    assert "仅选择要介绍的文档" in response.answer
    assert not response.citations and not response.anchors


def test_new_verb_reaches_the_overview_lane_end_to_end(repo):
    """新动词不是只有分类器单测:它必须真的把请求送进
    `ask_service._try_document_overview`,并在那条路上得到既有的终态。

    多篇未收窄 ⇒ 「请在来源面板仅选择要介绍的文档」——与
    `test_generic_question_does_not_match_a_title_by_substring` 同一个终态,只是
    问句用的是扩表新加的动词。分类器不命中的话这句话根本不会出现(请求会掉回
    ranked 通道,合成客户端也会被调用)。
    """
    nb = repo.create_notebook(NotebookCreate(name="资料"))
    seed(repo, nb.id, "a", "部署手册", "", ["甲文正文"])
    seed(repo, nb.id, "b", "运维手册", "", ["乙文正文"])
    client = AnswerClient()
    bind_chat_client(repo, "ask_answer", client)

    response = ask(repo, nb.id, "文档描述了什么")

    assert not client.prompts
    assert "仅选择要介绍的文档" in response.answer
    assert not response.citations and not response.anchors


def test_synthesis_failure_remains_visible_and_keeps_directory(repo):
    nb = repo.create_notebook(NotebookCreate(name="资料"))
    seed(repo, nb.id, "a", "手册", "摘要")
    client = AnswerClient("")
    bind_chat_client(repo, "ask_answer", client)
    response = ask(repo, nb.id, "这个库中的文档分别介绍了什么")
    assert len(client.prompts) == 2
    assert response.llm_mode == "synthesis_failed" and response.model_errors
    assert "合成未成功" in response.answer
    assert "手册" in response.answer and "已存摘要摘录" in response.answer
    assert response.result_sets[0].coverage.complete


def test_no_model_does_not_claim_synthesis_was_performed(repo):
    from tests.model_testkit import UNCONFIGURED_CHAT_CLIENT
    nb = repo.create_notebook(NotebookCreate(name="资料"))
    seed(repo, nb.id, "a", "手册", "摘要")
    bind_chat_client(repo, "ask_answer", UNCONFIGURED_CHAT_CLIENT)
    response = ask(repo, nb.id, "这个库中的文档分别介绍了什么")
    assert response.result_sets[0].synthesis_rows == 0
    assert response.result_sets[0].synthesis_complete is None
    assert "模型未配置" in response.answer
    assert "本次合成展示" not in response.answer


def test_catalog_scope_excludes_unselected_documents_and_denominator(repo):
    nb = repo.create_notebook(NotebookCreate(name="资料"))
    seed(repo, nb.id, "a", "手册", "可见摘要")
    seed(repo, nb.id, "b", "未选文档", "不应出现")
    client = AnswerClient()
    bind_chat_client(repo, "ask_answer", client)
    response = ask(repo, nb.id, "这个库中的文档分别介绍了什么", source_scope=SourceScope(mode="exclude", source_ids=["b"]))
    result = response.result_sets[0]
    assert result.coverage.total == result.coverage.returned_total == 1
    assert result.coverage.complete and result.items[0].source_id == "a"
    assert "不应出现" not in client.prompts[0]


def test_mounted_library_respects_selection_and_preserves_original_citation(repo):
    nb = repo.create_notebook(NotebookCreate(name="资料"))
    base = repo.create_notebook(NotebookCreate(name="参考资料"))
    repo.mark_notebook_base(base.id)
    repo.replace_notebook_bases(nb.id, [base.id], repo.current_user().id)
    seed(repo, nb.id, "a", "本库手册", "本库摘要")
    seed(repo, base.id, "b", "参考手册", "参考摘要", ["参考正文"])
    client = AnswerClient("参考正文 [k1]。")
    bind_chat_client(repo, "ask_answer", client)
    response = ask(repo, nb.id, "介绍《参考手册》")
    assert "参考正文" in client.prompts[0]
    assert response.anchors[0].notebook_id == base.id
    assert response.citations[0].source_id == "b"
    excluded = ask(repo, nb.id, "这个库中的文档分别介绍了什么", base_scope=BaseNotebookScope(mode="include", notebook_ids=[]))
    assert excluded.result_sets[0].coverage.total == 1
    assert "参考摘要" not in client.prompts[-1]


def test_catalog_map_source_count_matches_local_selection(repo):
    from app.services.source_scope import source_scope_context
    nb = repo.create_notebook(NotebookCreate(name="资料"))
    seed(repo, nb.id, "a", "手册", "摘要")
    seed(repo, nb.id, "b", "其他", "")
    with source_scope_context(nb.id, SourceScope(mode="include", source_ids=["a"])):
        assert repo.collection_catalog.collection_map(nb.id).sources == 1


def test_mixed_notebook_introduction_covers_local_papers_without_mounted_noise(repo):
    nb = repo.create_notebook(NotebookCreate(name="DeepSeek-V4"))
    base = repo.create_notebook(NotebookCreate(name="LLM Structure & Infra"))
    repo.mark_notebook_base(base.id)
    repo.replace_notebook_bases(nb.id, [base.id], repo.current_user().id)
    titles = ["DeepSeek-V4", "DSpark", "mHC", "EnergAIzer", "DualGraph"]
    for index, title in enumerate(titles):
        seed(repo, nb.id, f"local-{index}", title, f"研究{title}的核心方法")
    seed(repo, base.id, "noise", "DeepSeek-V2", "土拨鼠阅读示例")
    client = AnswerClient()
    bind_chat_client(repo, "ask_answer", client)
    response = ask(repo, nb.id, "介绍一下这个notebook中的文章")
    assert response.result_sets[0].coverage.total == 5
    assert response.result_sets[0].coverage.complete
    assert all(title in response.answer for title in titles)
    assert "土拨鼠" not in client.prompts[0] and "DeepSeek-V2" not in response.answer
    assert {c.source_id for c in response.citations} == {f"local-{i}" for i in range(5)}
    assert all(not c.notebook_id for c in response.citations)


def test_explicit_reference_overview_still_intersects_library_selection(repo):
    nb = repo.create_notebook(NotebookCreate(name="资料"))
    base = repo.create_notebook(NotebookCreate(name="参考库"))
    repo.mark_notebook_base(base.id)
    repo.replace_notebook_bases(nb.id, [base.id], repo.current_user().id)
    seed(repo, nb.id, "local", "本库文章", "本库摘要")
    seed(repo, base.id, "borrowed", "参考文章", "参考摘要")
    client = AnswerClient()
    bind_chat_client(repo, "ask_answer", client)
    question = "介绍一下这个notebook中的文章，包括挂载的参考库"
    response = ask(repo, nb.id, question)
    assert response.result_sets[0].coverage.total == 2
    assert any(c.notebook_id == base.id for c in response.citations)
    response = ask(repo, nb.id, question, base_scope=BaseNotebookScope(mode="include", notebook_ids=[]))
    assert response.result_sets[0].coverage.total == 1
    assert "参考摘要" not in client.prompts[-1]


def test_catalog_missing_summary_reads_original_without_other_sources(repo):
    nb = repo.create_notebook(NotebookCreate(name="资料"))
    seed(repo, nb.id, "a", "原文文章", "", ["研究问题", "核心方法", "最终贡献"])
    seed(repo, nb.id, "b", "未选文章", "", ["不应读取"])
    client = AnswerClient("介绍依据")
    bind_chat_client(repo, "ask_answer", client)
    response = ask(repo, nb.id, "介绍一下这个notebook中的文章",
                   source_scope=SourceScope(mode="include", source_ids=["a"]))
    assert "最终贡献" in client.prompts[0] and "不应读取" not in client.prompts[0]
    assert any(c.element_id == "a-002" for c in response.citations)
    assert "全部 3" in response.answer


def test_catalog_preserves_shared_language_policy_and_saved_style(repo, monkeypatch):
    from app.services.prompt_layers import fragment_text
    nb = repo.create_notebook(NotebookCreate(name="Papers"))
    seed(repo, nb.id, "a", "Paper", "Summary")
    client = AnswerClient()
    bind_chat_client(repo, "ask_answer", client)
    service = repo._runtime.ask_component
    monkeypatch.setattr(service, "_search_profile_style_block", lambda _: "Prefer English and detailed explanations.")
    ask(repo, nb.id, "Summarize each document")
    assert fragment_text("answer.style_language") in client.prompts[0]
    assert "Prefer English and detailed explanations." in client.prompts[0]
    assert "Write Chinese prose" not in client.prompts[0]


@pytest.mark.parametrize("include_local", [False, True])
def test_untitled_single_document_keeps_authorized_mounted_scope(repo, include_local):
    nb = repo.create_notebook(NotebookCreate(name="资料"))
    base = repo.create_notebook(NotebookCreate(name="参考库"))
    repo.mark_notebook_base(base.id)
    repo.replace_notebook_bases(nb.id, [base.id], repo.current_user().id)
    seed(repo, base.id, "reference", "参考文章", "", ["参考库正文"])
    if include_local:
        seed(repo, nb.id, "local", "本库文章", "", ["本库正文"])
    client = AnswerClient("原文介绍 [k1]。")
    bind_chat_client(repo, "ask_answer", client)
    response = ask(repo, nb.id, "这篇文档介绍了什么内容")
    if include_local:
        assert not client.prompts
        assert "仅选择要介绍的文档" in response.answer
    else:
        assert "参考库正文" in client.prompts[0]
        assert response.anchors[0].notebook_id == base.id


def test_catalog_keeps_prior_user_preferences_without_prior_assistant_evidence(repo):
    nb = repo.create_notebook(NotebookCreate(name="资料"))
    seed(repo, nb.id, "a", "文章", "实际摘要")
    client = AnswerClient("旧回答中的参考库噪声 [k5001]")
    bind_chat_client(repo, "ask_answer", client)
    first = ask(repo, nb.id, "介绍一下这个notebook中的文章")
    with repo._write() as db:
        db.execute("UPDATE answers SET question=? WHERE id=?",
                   ("接下来的回答请面向初学者，保留\n多行说明要求", first.answer_id))
    ask(repo, nb.id, "介绍一下这个notebook中的文章", conversation_id=first.conversation_id)
    assert "面向初学者" in client.prompts[-1] and "多行说明要求" in client.prompts[-1]
    assert "旧回答中的参考库噪声" not in client.prompts[-1]
