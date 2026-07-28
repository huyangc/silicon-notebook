import inspect

from app.repositories import ports


def test_required_protocol_catalogue_has_declared_operations():
    required = {
        "IdentityRepository": {
            "current_user", "create_user",
            "authenticate_user", "create_session", "resolve_session", "delete_session",
        },
        "ModelStatusStorePort": {"get_all", "record"},
        "NotebookSharingRepository": {"share_notebook", "unshare_notebook", "copy_notebook", "add_member", "remove_member", "join_shared", "leave_notebook"},
        "KnowledgeGovernanceRepository": {"update_knowledge", "merge_knowledge", "confirm_conflict", "approve_promotion", "reject_promotion"},
        "KnowledgeLifecycleRepository": {"build_notebook_kg", "rebuild_unified_kg", "unified_kg_status"},
        "AskStateRepository": {"begin_ask_job", "finish_ask_job", "ask_job_status", "append_ask_trace", "get_conversation", "submit_feedback"},
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
