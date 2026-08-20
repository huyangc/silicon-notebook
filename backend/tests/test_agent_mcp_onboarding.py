from fastapi.testclient import TestClient


def test_onboarding_document_is_public_and_uses_configured_mcp_url(monkeypatch):
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://notebook.example.test/mcp")
    monkeypatch.setenv("MCP_REQUIRE_HTTPS", "1")

    from app.main import create_app

    client = TestClient(create_app())
    response = client.get("/api/agent-mcp/onboarding")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.headers["cache-control"] == "no-store"
    assert "https://notebook.example.test/mcp" in response.text
    assert "Authorization: Bearer <AGENT_TOKEN>" in response.text
    assert "select_notebook" in response.text
    assert "propose_memory" in response.text
    assert "export SILICON_NOTEBOOK_AGENT_TOKEN" not in response.text
    # The interpolated header form must stay ON OFFER next to the placeholder.
    # Dropping it leaves the anonymous instruction plane telling every Agent to
    # paste the credential into `~/.claude.json` — and this document is the one
    # a user never reviews before the Agent acts on it. The `export` assertion
    # above still holds: an Agent subprocess cannot export into its parent, so
    # the variable is a reported user action, not a command to run.
    assert "Bearer ${SILICON_NOTEBOOK_AGENT_TOKEN}" in response.text
    assert "does not persist the token" in response.text
    assert "does not prove an authenticated connection" in response.text

    unsafe_requests = (
        client.get("/api/agent-mcp/onboarding?token=agent-token-secret"),
        client.get(
            "/api/agent-mcp/onboarding",
            headers={"Authorization": "Bearer agent-token-secret"},
        ),
    )
    for rejected in unsafe_requests:
        assert rejected.status_code == 400
        assert rejected.headers["cache-control"] == "no-store"
        assert "agent-token-secret" not in rejected.text


def test_onboarding_document_lists_the_live_public_tool_manifest():
    from app.api.agent_mcp_onboarding import render_agent_mcp_onboarding
    from app.api.mcp_server import PUBLIC_TOOLS

    document = render_agent_mcp_onboarding("http://127.0.0.1:8000/mcp")

    for tool in PUBLIC_TOOLS:
        assert document.count(f"- `{tool}`") == 1
    assert "agent-token-secret" not in document
