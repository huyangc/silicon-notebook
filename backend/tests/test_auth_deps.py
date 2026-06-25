import pytest
from app.api import deps


def test_repository_singleton_importable_from_deps():
    assert deps.repository() is deps.repository()
