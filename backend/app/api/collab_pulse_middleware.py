"""A response-start observer; observers cannot change the response outcome."""
from app.api.collab_marks import CURRENT_COLLAB_SCOPE
from app.api.collab_pulse_routes import PULSE_ROUTE_RULES, Branch, Capability, NoneKind


class CollabPulseMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def observe(message):
            if message["type"] == "http.response.start" and message["status"] < 400 and scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
                self._record(scope)
            await send(message)

        token = CURRENT_COLLAB_SCOPE.set(scope)
        try:
            await self.app(scope, receive, observe)
        finally:
            CURRENT_COLLAB_SCOPE.reset(token)

    @staticmethod
    def _record(scope):
        try:
            from app.api.deps import _CAPABILITY_PULSE_KIND, collaboration_service
            route = scope.get("route")
            template = getattr(route, "path_format", "")
            rule = PULSE_ROUTE_RULES.get((scope["method"], template))
            if rule is None and template.startswith("/api/extensions/"):
                # Extension host committed writes carry explicit core marks;
                # plugin handler success alone never implies a content write.
                rule = Branch("sources")
            if rule is None or isinstance(rule, NoneKind):
                return
            source = "guard" if isinstance(rule, Capability) else "explicit"
            seen = set()
            for mark in scope.get("state", {}).get("collab_marks", ()):
                if mark.source != source:
                    continue
                if isinstance(rule, Capability) and mark.capability != rule.name:
                    continue
                kind = _CAPABILITY_PULSE_KIND[mark.capability] if isinstance(rule, Capability) else rule.kind
                key = (mark.notebook_id, kind)
                if key in seen or kind == "none":
                    continue
                seen.add(key)
                collaboration_service().record(mark, kind, scope["method"] + " " + template, rule.affected_kinds)
        except Exception:
            # Memory hints and bounded async persistence are fail-open. No
            # payload, principal, notebook, path value or exception is logged.
            pass
