from app.repositories.ownership_manifest import OWNER_BY_MEMBER, SURFACE_MEMBERS

def test_every_manifest_member_has_one_owner():
    assert len(OWNER_BY_MEMBER) == len(SURFACE_MEMBERS)
    assert all(m.owner for m in SURFACE_MEMBERS)
