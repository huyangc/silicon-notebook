"""Keep OAuth query credentials out of the ASGI server's access log."""
import logging
from urllib.parse import urlsplit


class AuthenticationAccessFilter(logging.Filter):
    def filter(self, record):
        # Uvicorn's access tuple is client, method, request target, version, status.
        if isinstance(record.args, tuple) and len(record.args) == 5:
            target = record.args[2]
            if isinstance(target, str) and urlsplit(target).path.startswith("/api/auth/"):
                values = list(record.args)
                values[2] = target.split("?", 1)[0]
                record.args = tuple(values)
        return True


def install_authentication_access_filter():
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, AuthenticationAccessFilter) for item in logger.filters):
        logger.addFilter(AuthenticationAccessFilter())
