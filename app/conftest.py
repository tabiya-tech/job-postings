import platform
import uuid

import pytest
from pymongo_inmemory import Mongod
from pymongo_inmemory.context import Context

from app.config import Settings


@pytest.fixture(scope="session", name="mongodb_uri")
def fixture_mongodb_uri():
    """One throwaway mongod for the whole run (set PYMONGOIM__USE_LOCAL_MONGOD=True to use the installed one).

    Set up as in compass-connect's backend conftest, which works around two pymongo_inmemory issues.
    """
    # On Ubuntu and Debian pymongo_inmemory falls back to MongoDB 4.0 unless the OS is named
    # (https://github.com/kaizendorks/pymongo_inmemory/issues/115).
    os_name = next((name for name in ("ubuntu", "debian") if name in platform.uname().version.lower()), None)
    context = Context(version="7.0", os_name=os_name)
    # It also picks the removed "ephemeralForTest" engine for MongoDB 7
    # (https://github.com/kaizendorks/pymongo_inmemory/pull/119).
    context.storage_engine = "wiredTiger"

    mongod = Mongod(context)
    mongod.start()
    try:
        yield mongod.connection_string
    finally:
        mongod.stop()


@pytest.fixture(name="settings")
def fixture_settings(mongodb_uri) -> Settings:
    """Settings pointing at a fresh database, so tests never see each other's data."""
    return Settings(
        mongodb_uri=mongodb_uri,
        mongodb_database_name=f"test_{uuid.uuid4().hex}",
        poll_interval_seconds=0.01,
        _env_file=None,
    )


@pytest.fixture(name="db")
async def fixture_db(settings):
    """A fresh database with the indexes in place."""
    # pylint: disable=import-outside-toplevel
    from app import db as database

    client = database.create_client(settings)
    try:
        db = client[settings.mongodb_database_name]
        await database.ensure_indexes(db)
        yield db
        await client.drop_database(settings.mongodb_database_name)
    finally:
        await client.close()
