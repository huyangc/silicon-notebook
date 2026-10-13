import inspect

from app.repositories import ports


def test_required_protocol_catalogue_has_declared_operations():
    required = {
        "IdentityReadRepository": {"current_user", "resolve_session", "audit_labels_for_user_ids"},
        "IdentityRepository": {
            "create_user", "authenticate_user", "create_session", "delete_session",
        },
        "ModelStatusStorePort": {"get_all", "record"},
        "NotebookSharingRepository": {"share_notebook", "unshare_notebook", "copy_notebook", "add_member", "remove_member", "join_shared", "leave_notebook"},
        "KnowledgeGovernanceRepository": {"update_knowledge", "merge_knowledge", "confirm_conflict", "approve_promotion", "reject_promotion"},
        "KnowledgeLifecycleRepository": {"build_notebook_kg", "rebuild_unified_kg", "unified_kg_status"},
        "AskStateRepository": {"begin_ask_job", "finish_ask_job", "append_ask_trace", "get_conversation", "submit_feedback"},
        "ReportRepository": {"create_report", "update_report", "get_report", "list_reports", "delete_report", "export_reports"},
        "RetrievalPort": {"retrieve_scored", "federated_retrieve", "federated_retrieve_relations", "retrieve_neighbors", "retrieve_elements", "ppr_retrieve", "follow_chain", "node_context"},
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
