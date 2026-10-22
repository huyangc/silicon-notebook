"""Keep credentials and content out of the ASGI server's access log.

OAuth callbacks carry credentials in their query string (dropped); a few routes carry
content or a credential in a path segment (templated by ``app.core.log_paths``).
"""
import logging
from urllib.parse import urlsplit

from app.core.log_paths import loggable_target


class AuthenticationAccessFilter(logging.Filter):
    def filter(self, record):
        # Uvicorn's access tuple is client, method, request target, version, status.
        if isinstance(record.args, tuple) and len(record.args) == 5:
            target = record.args[2]
            if isinstance(target, str):
                if urlsplit(target).path.startswith("/api/auth/"):
                    loggable = target.split("?", 1)[0]
                else:
                    loggable = loggable_target(target)
                if loggable != target:
                    values = list(record.args)
                    values[2] = loggable
                    record.args = tuple(values)
        return True


def install_authentication_access_filter():
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, AuthenticationAccessFilter) for item in logger.filters):
        logger.addFilter(AuthenticationAccessFilter())
