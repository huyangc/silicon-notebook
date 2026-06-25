from app.core.config import Settings
from app.services.sqlite_repository import (
    SQLiteRepository, set_request_user, reset_request_user,
)
from app.models.schemas import NotebookCreate


def _repo(tmp_path):
    return SQLiteRepository(Settings(database_url=f"sqlite:///{tmp_path}/t.db"))


def test_list_and_create_scoped_to_current_user(tmp_path):
    repo = _repo(tmp_path)
    zhang = repo.create_user("z00123456", "pw")
    li = repo.create_user("l00000042", "pw")

    tok = set_request_user(zhang)
    try:
        nb = repo.create_notebook(NotebookCreate(name="zhang nb"))
        names = [n.name for n in repo.list_notebooks()]
    finally:
        reset_request_user(tok)
    assert "zhang nb" in names

    tok = set_request_user(li)
    try:
        assert repo.list_notebooks() == []                      # li 看不到 zhang 的
        assert repo.user_can_access_notebook(nb.id, li.id) is False
        assert repo.user_can_access_notebook(nb.id, zhang.id) is True
    finally:
        reset_request_user(tok)


def test_admin_does_not_see_user_notebooks(tmp_path):
    repo = _repo(tmp_path)
    zhang = repo.create_user("z00123456", "pw")
    tok = set_request_user(zhang)
    try:
        repo.create_notebook(NotebookCreate(name="private"))
    finally:
        reset_request_user(tok)
    # admin（ContextVar 未设 → 回退 user-local）看不到 zhang 的私人本
    assert [n.name for n in repo.list_notebooks()] == []
