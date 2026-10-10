import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from tests.auth_store_contract import AuthStoreContract


@pytest.fixture
def identity(tmp_path):
    repo = SQLiteRepository(Settings(database_url=f"sqlite:///{tmp_path}/auth.db",storage_dir=str(tmp_path/"storage"),_env_file=None,event_log_enabled=False,llm_log_enabled=False))
    yield repo._runtime.identity
    repo.close()


class TestSQLiteAuthStore(AuthStoreContract):
    pass
