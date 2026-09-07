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


class AnswerClient:
    configured = True
    model = "test"

    def __init__(self, answer="介绍依据 [k5001]。"):
        self.prompts = []
        self.answer = answer

    def chat_json(self, messages, *args, **kwargs):
        self.prompts.append(messages[0]["content"])
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


def test_synthesis_failure_remains_visible_and_keeps_directory(repo):
    nb = repo.create_notebook(NotebookCreate(name="资料"))
    seed(repo, nb.id, "a", "手册", "摘要")
    client = AnswerClient("")
    bind_chat_client(repo, "ask_answer", client)
    response = ask(repo, nb.id, "这个库中的文档分别介绍了什么")
    assert len(client.prompts) == 2
    assert response.llm_mode == "synthesis_failed" and response.model_errors
    assert "合成未成功" in response.answer
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
