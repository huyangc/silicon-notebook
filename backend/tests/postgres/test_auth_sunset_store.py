import pytest

from app.repositories.postgres.identity_store import IdentityStore
from app.repositories.postgres.migrator import PostgresMigrator
from tests.auth_sunset_contract import AuthSunsetContract


pytestmark = pytest.mark.postgres_integration


@pytest.fixture
def identity(postgres_database, postgres_settings):
    PostgresMigrator(postgres_database).migrate()
    with postgres_database.write() as db:
        db.execute("INSERT INTO users(id,email,display_name,role,status,username,local_login_name,created_at,updated_at) VALUES ('user-local','admin@test.invalid','Admin','admin','active','admin','admin',now(),now())")
    return IdentityStore(postgres_database,postgres_settings)


class TestPostgresSunset(AuthSunsetContract):
    pass
