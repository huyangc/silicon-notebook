"""Same collaboration eligibility/activity behaviors run on both adapters."""
from app.repositories.user_references import USER_REFERENCE_COLUMNS
from tests.notebook_edit_access_cases import SHAPES


def owner_probes_match_boolean_predicates(world):
    for shape, (_, _, edit, admin, _) in SHAPES.items():
        user = world.users[shape]
        assert world.store.owner_if_can_edit(world.notebook, user) == (world.owner_id if edit else None), shape
        assert world.store.owner_if_can_admin(world.notebook, user) == (world.owner_id if admin else None), shape
    assert world.store.owner_if_can_edit("missing", world.owner_id) is None
    assert world.store.owner_if_can_admin("missing", world.owner_id) is None
    world.write([("UPDATE notebooks SET status='deleting' WHERE id=?", (world.notebook,))])
    assert world.store.owner_if_can_edit(world.notebook, world.owner_id) is None


def nullable_owner_preserves_existing_grant_authority(world):
    world.write([("UPDATE notebooks SET created_by=NULL WHERE id=?", (world.notebook,))])
    editor = world.users["editor"]
    admin = world.users["group_admin"]
    assert world.store.user_can_edit_notebook(world.notebook, editor)
    assert world.store.user_can_admin_notebook(world.notebook, admin)
    # Match NotebookAccess's owner normalization. Empty is an authorized
    # legacy owner value; only None means the predicate did not return a row.
    assert world.store.owner_if_can_edit(world.notebook, editor) == ""
    assert world.store.owner_if_can_admin(world.notebook, admin) == ""
    assert world.repo._runtime.sharing.owner_if_can_edit(world.notebook, editor) == ""
    assert world.repo._runtime.sharing.owner_if_can_admin(world.notebook, admin) == ""


def pulse_eligibility_truth_table(world):
    nb = world.notebook
    for shape in SHAPES:
        assert world.store.collab_pulse_wanted(nb, world.users[shape]) is (shape != "stranger"), shape
    world.write([
        ("DELETE FROM notebook_grants WHERE notebook_id=?", (nb,)),
        ("DELETE FROM notebook_members WHERE notebook_id=?", (nb,)),
    ])
    assert not world.store.collab_pulse_wanted(nb, world.owner_id)
    # An enabled link with no member is not a collaborator.
    world.write([("UPDATE notebooks SET is_shared=1 WHERE id=?", (nb,))])
    assert not world.store.collab_pulse_wanted(nb, world.owner_id)
    world.write([("UPDATE notebooks SET tier='base' WHERE id=?", (nb,))])
    assert world.store.collab_pulse_wanted(nb, world.owner_id)
    assert not world.store.collab_pulse_wanted(nb, world.users["stranger"])
    world.write([("UPDATE notebooks SET tier='personal' WHERE id=?", (nb,)),
                 ("INSERT INTO notebook_grants(id,notebook_id,principal_type,principal_id,role,created_at) VALUES (?,?,?,?,?,?)",
                  ("pulse-public", nb, "everyone", "", "viewer", "2026-11-01T00:00:00+00:00"))])
    assert world.store.collab_pulse_wanted(nb, world.owner_id)
    assert not world.store.collab_pulse_wanted(nb, world.users["stranger"])
    # A restricted path survives alongside public access and enables pulse.
    world.repo._runtime.sharing.add_member(nb, world.users["link_member"])
    assert world.store.collab_pulse_wanted(nb, world.users["link_member"])
    world.write([("DELETE FROM notebook_members WHERE notebook_id=?", (nb,)),
                 ("DELETE FROM notebook_grants WHERE notebook_id=?", (nb,)),
                 ("INSERT INTO notebook_grants(id,notebook_id,principal_type,principal_id,role,created_at) VALUES (?,?,?,?,?,?)",
                  ("pulse-group", nb, "group", world.groups["viewers"], "viewer", "2026-11-01T00:00:00+00:00"))])
    assert world.store.collab_pulse_wanted(nb, world.owner_id)
    world.write([("DELETE FROM group_members WHERE group_id=?", (world.groups["viewers"],)),
                 ("INSERT INTO group_members(group_id,user_id,role,added_at) VALUES (?,?,?,?)",
                  (world.groups["viewers"], world.owner_id, "admin", "2026-11-01T00:00:00+00:00"))])
    assert not world.store.collab_pulse_wanted(nb, world.owner_id)
    assert not world.store.collab_pulse_wanted("missing", world.owner_id)


def activity_order_retention_and_cascade(world):
    nb = world.notebook
    for id_, at in (("b", "2026-11-01T00:00:00+00:00"), ("a", "2026-11-01T00:00:00+00:00"),
                    ("old", "2026-01-01T00:00:00+00:00")):
        world.store.insert_activity(dict(id=id_, notebook_id=nb, kind="sources", capability="sources:write",
                                         actor_id="deleted-account", actor_name="同事", target="PATCH /template", created_at=at))
    assert [row["id"] for row in world.store.list_recent_activity(nb, 2)] == ["a", "b"]
    assert world.store.list_recent_activity("missing", 10) == []
    assert world.store.purge_expired_activity("2026-02-01T00:00:00+00:00") == 1
    assert world.store.purge_expired_activity("2026-11-01T00:00:00+00:00") == 0
    assert any(reference.table == "notebook_activity" and reference.column == "actor_id"
               and reference.kind == "audit_kept" for reference in USER_REFERENCE_COLUMNS)
    # This is deliberately an orphan actor: account deletion does not erase
    # useful history, while removing its notebook does.
    world.write([("DELETE FROM notebooks WHERE id=?", (nb,))])
    assert world.store.list_recent_activity(nb, 10) == []


def startup_purges_activity_without_feature_switch(world):
    base = dict(notebook_id=world.notebook, kind="sources", capability="sources:write",
                actor_id=world.users["editor"], actor_name="同事", target="PATCH /template")
    world.store.insert_activity({**base, "id": "expired", "created_at": "1900-01-01T00:00:00+00:00"})
    world.store.insert_activity({**base, "id": "future", "created_at": "2999-01-01T00:00:00+00:00"})
    world.repo._recover_interrupted_jobs()
    assert [row["id"] for row in world.store.list_recent_activity(world.notebook, 10)] == ["future"]


CASES = {function.__name__: function for function in (
    owner_probes_match_boolean_predicates, pulse_eligibility_truth_table,
    nullable_owner_preserves_existing_grant_authority,
    activity_order_retention_and_cascade,
    startup_purges_activity_without_feature_switch,
)}
