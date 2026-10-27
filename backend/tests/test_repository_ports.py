import inspect

from app.repositories import ports


def test_required_protocol_catalogue_has_declared_operations():
    required = {
        "IdentityReadRepository": {"current_user", "resolve_session", "audit_labels_for_user_ids"},
        "IdentityRepository": {
            "create_user", "authenticate_user", "create_session", "delete_session",
        },
        "ModelStatusStorePort": {"get_all", "record"},
        "NotebookSharingRepository": {"share_notebook", "unshare_notebook", "copy_notebook", "add_member", "remove_member", "join_shared"},
        "KnowledgeGovernanceRepository": {"update_knowledge", "merge_knowledge", "confirm_conflict", "approve_promotion_as_reviewer", "reject_promotion_as_reviewer"},
        "KnowledgeLifecycleRepository": {"build_notebook_kg", "rebuild_unified_kg", "unified_kg_status"},
        "AskStateRepository": {"begin_ask_job", "finish_ask_job", "append_ask_trace", "get_conversation", "submit_feedback"},
        "ReportRepository": {"create_report", "update_report", "get_report", "list_reports", "delete_report", "export_reports"},
        "RetrievalPort": {"retrieve_scored", "federated_retrieve", "retrieve_neighbors", "retrieve_elements", "ppr_retrieve", "follow_chain", "node_context"},
        "EvidenceKnowledgeContextPort": {
            "cluster_map", "node_context", "in_network_relations",
            "relation_support_count",
        },
    }
    for protocol_name, methods in required.items():
        protocol = getattr(ports, protocol_name)
        assert methods <= set(protocol.__dict__), protocol_name
        for method in methods:
            assert inspect.isfunction(getattr(protocol, method)), (protocol_name, method)


def _protocol_members(protocol) -> set[str]:
    """Members a protocol declares itself or inherits from protocol bases."""
    return {
        name
        for base in protocol.__mro__
        if getattr(base, "_is_protocol", False) and base is not object
        for name, value in vars(base).items()
        if inspect.isfunction(value)
    }


def test_identity_repository_still_declares_the_facade_identity_reads():
    """S2 批 E split the facade's identity reads into ``IdentityReadRepository``
    (what ``NotebookRepository`` inherits). ``identity_repository()`` still
    hands out ``IdentityRepository``, whose callers resolve sessions and read
    the current user through it -- so it must keep those reads by inheriting
    the split-off base, not merely sit next to it."""
    # Protocols here are not runtime_checkable, so issubclass() raises: read
    # the inheritance off the MRO instead.
    assert ports.IdentityReadRepository in ports.IdentityRepository.__mro__
    assert {
        "current_user", "resolve_session", "audit_labels_for_user_ids",
    } <= _protocol_members(ports.IdentityRepository)
    assert ports.IdentityReadRepository in ports.NotebookRepository.__mro__
    assert ports.IdentityRepository not in ports.NotebookRepository.__mro__


def test_access_and_sharing_protocols_still_declare_the_facade_reads():
    """S2 批 F split the facade's access and sharing reads into
    ``NotebookAccessReadRepository`` / ``NotebookShareReadRepository`` (what
    ``NotebookRepository`` and ``McpMemoryRepository`` inherit).
    ``notebook_access_repository()`` / ``notebook_sharing_repository()`` still
    hand out the full protocols, whose callers read through them -- so those
    must keep the reads by inheriting the split-off bases."""
    access_reads = {
        "user_can_access_notebook", "user_can_read_notebook", "source_owner",
        "source_notebook_id", "answer_owner",
    }
    share_reads = {"share_state", "notebook_copy_stats", "shared_preview", "shared_by_me"}
    assert ports.NotebookAccessReadRepository in ports.NotebookAccessRepository.__mro__
    assert access_reads <= _protocol_members(ports.NotebookAccessRepository)
    assert ports.NotebookShareReadRepository in ports.NotebookSharingRepository.__mro__
    assert share_reads <= _protocol_members(ports.NotebookSharingRepository)
    for facade_bound in (ports.NotebookRepository, ports.McpMemoryRepository):
        assert ports.NotebookAccessReadRepository in facade_bound.__mro__
        assert ports.NotebookAccessRepository not in facade_bound.__mro__
    assert ports.NotebookShareReadRepository in ports.NotebookRepository.__mro__
    assert ports.NotebookSharingRepository not in ports.NotebookRepository.__mro__
    # S2-E: the facade's share_state had no consumer; the sharing protocol
    # still declares it for the routes, the facade-bound one does not.
    assert "share_state" not in _protocol_members(ports.NotebookRepository)


def test_facade_bound_model_contract_is_the_reasoning_provider_without_rerank():
    """S2-A: the facade implements ``chat`` / ``configured`` / ``parallelism``
    but never ``rerank``, and must not grow it -- the knowhow
    ``from_repository`` path hands the facade over as ``model_clients`` and
    stays on MMR only because the rerank probe finds nothing there. So the
    facade-bound ``NotebookRepository`` inherits ``ReasoningModelProvider``
    through ``FacadePropertyContract``, not ``ModelClientProvider``; the rerank
    callers (report engine, ``AskModelClientProvider``) keep the full port,
    which the runtime model provider implements."""
    assert ports.ReasoningModelProvider in ports.FacadePropertyContract.__mro__
    assert ports.ModelClientProvider not in ports.FacadePropertyContract.__mro__
    assert ports.ModelClientProvider not in ports.NotebookRepository.__mro__
    assert "rerank" not in _protocol_members(ports.NotebookRepository)
    assert {"chat", "configured", "parallelism"} <= _protocol_members(
        ports.NotebookRepository
    )
    assert "rerank" in _protocol_members(ports.ModelClientProvider)
    assert ports.ModelClientProvider in ports.AskModelClientProvider.__mro__
