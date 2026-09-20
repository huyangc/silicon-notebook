"""Recheck browser sessions before emitting authenticated HTTP stream frames."""
from __future__ import annotations

from starlette.concurrency import run_in_threadpool


class _AuthenticationRevoked(Exception):
    pass


class AuthenticationStreamGuard:
    """Stop delivery when a session expires, is revoked or loses stage admission.

    Durable jobs keep their own lifetime. This layer closes only the response;
    request-local streams perform their normal disconnect cleanup.
    """

    def __init__(self, app, *, is_valid):
        self.app = app
        self.is_valid = is_valid

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if path == "/mcp" or path.startswith("/mcp/"):
            # MCP rechecks Agent access at each HTTP request and tool boundary.
            # Dropping a committed tool's JSON-RPC acknowledgement leaves its
            # client waiting for that request ID even after an SSE EOF.
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", ()))
        authorization = headers.get(b"authorization", b"").decode("latin-1")
        if not authorization.lower().startswith("bearer "):
            return await self.app(scope, receive, send)
        token = authorization[7:].strip()
        streaming = False
        closed = False

        async def guarded_send(message):
            nonlocal streaming, closed
            if message["type"] == "http.response.start":
                response_headers = dict(message.get("headers", ()))
                content_type = response_headers.get(b"content-type", b"").split(b";", 1)[0]
                streaming = content_type in {b"application/x-ndjson", b"text/event-stream"}
            if streaming and message["type"] == "http.response.body" and message.get("body"):
                if not await run_in_threadpool(self.is_valid, token):
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
                    closed = True
                    raise _AuthenticationRevoked()
            if not closed:
                await send(message)

        try:
            await self.app(scope, receive, guarded_send)
        except* _AuthenticationRevoked:
            # Other failures in an ExceptionGroup continue to propagate.
            pass
