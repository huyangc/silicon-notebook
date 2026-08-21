"""Stable cancellation token shape used by application ports."""
from __future__ import annotations

import threading
from typing import Optional


CancelEvent = Optional[threading.Event]


class CoreCancellation(Exception):
    """Base class for request cancellation that extension hosts must propagate."""
