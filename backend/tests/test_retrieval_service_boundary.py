from __future__ import annotations

import ast
import inspect

from app.core.config import Settings
from app.services.reasoning_retrieval import ReasoningRetriever
from app.services.graph_retrieval import GraphRetrievalService
from app.services.retrieval_candidates import CandidateRetrievalService
from app.services.retrieval_service import RetrievalService


class _Candidates:
    def retrieve_scored(self, *args, **kwargs):
        return []


class _Graph:
    pass


def test_retrieval_service_has_no_repository_backreference():
    communities = lambda: object()
    service = RetrievalService(
        candidates=_Candidates(), graph=_Graph(), community_queries=communities
    )
    assert service.__dict__ == {
        "candidates": service.candidates,
        "graph": service.graph,
        "_community_queries": communities,
    }


def test_retrieval_service_does_not_call_facade_private_retrieval():
    service = RetrievalService(
        candidates=_Candidates(), graph=_Graph(), community_queries=lambda: object()
    )
    assert service.retrieve_scored("nb", "query") == []


def test_retrieval_service_source_has_no_sqlite_repository_import():
    source = inspect.getsource(inspect.getmodule(RetrievalService))
    assert "SQLiteRepository" not in source
    assert "_repo" not in source


def test_graph_retrieval_has_one_cluster_map_definition():
    source = inspect.getsource(inspect.getmodule(GraphRetrievalService))
    tree = ast.parse(source)
    graph_class = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "GraphRetrievalService"
    )
    assert sum(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "cluster_map"
        for node in graph_class.body
    ) == 1


def test_reasoning_retriever_accepts_ports_without_sqlite_repository():
    class _Retrieval:
        def federated_retrieve(self, *args, **kwargs):
            return []

    class _Models:
        reasoning_llm_client = type("LLM", (), {"configured": False})()

    class _Communities:
        def first_base_notebook_id(self, active_notebook_id):
            return None

        def resolve_comparison_peers(self, *args, **kwargs):
            return [], "community"

    retriever = ReasoningRetriever(
        retrieval=_Retrieval(),
        model_clients=_Models(),
        communities=_Communities(),
        settings=Settings(),
    )
    assert retriever.search("nb", "query") == []
