import asyncio

from app.api.auth_stream import AuthenticationStreamGuard


def test_stream_stops_before_private_frame_after_revocation():
    frames = []
    active = {"value": True}
    finalized = []

    async def app(scope, receive, send):
        try:
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"application/x-ndjson")]})
            await send({"type": "http.response.body", "body": b"before\n", "more_body": True})
            active["value"] = False
            await send({"type": "http.response.body", "body": b"private-after\n", "more_body": True})
        finally:
            finalized.append(True)

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        frames.append(message)

    guard = AuthenticationStreamGuard(app, is_valid=lambda token: active["value"])
    asyncio.run(guard({"type": "http", "headers": [(b"authorization", b"Bearer test")]}, receive, send))
    assert b"".join(frame.get("body", b"") for frame in frames) == b"before\n"
    assert frames[-1]["more_body"] is False
    assert finalized == [True]


def test_public_stream_is_not_treated_as_a_local_session():
    frames = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")]})
        await send({"type": "http.response.body", "body": b"public", "more_body": False})

    async def send(message):
        frames.append(message)

    def should_not_check(token):
        raise AssertionError("public response has no session")

    guard = AuthenticationStreamGuard(app, is_valid=should_not_check)
    asyncio.run(guard({"type": "http", "headers": []}, None, send))
    assert frames[-1]["body"] == b"public"
