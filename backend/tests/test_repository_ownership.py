import pytest

from app.repositories.ownership_manifest import (
    OWNER_BY_MEMBER,
    SURFACE_MEMBERS,
    SurfaceMember,
    validate_ownership_manifest,
)

def test_every_manifest_member_has_one_owner():
    assert len(OWNER_BY_MEMBER) == len(SURFACE_MEMBERS)
    assert all(m.owner for m in SURFACE_MEMBERS)


def test_duplicate_surface_members_fail_fast():
    with pytest.raises(ValueError, match="duplicate ownership"):
        validate_ownership_manifest(
            (
                SurfaceMember("same", "OwnerA", "method", ()),
                SurfaceMember("same", "OwnerB", "method", ()),
            )
        )
