import pytest
from fastapi.testclient import TestClient

from app.api_keys import hash_api_key
from app.config import ApiKey
from app.postings.test_support import ADMIN_KEY, KENYA_KEY, OTHER_KENYA_KEY, SECOND_KEY_OF_SOURCE_A
from app.server import create_app


@pytest.fixture(name="client")
def fixture_client(settings):
    """The API on a fresh database, with source A (two keys), source B and an admin key."""
    settings.api_keys = {
        hash_api_key(KENYA_KEY): ApiKey(source="source-a", countries=["kenya"]),
        hash_api_key(SECOND_KEY_OF_SOURCE_A): ApiKey(source="source-a", countries=["kenya"]),
        hash_api_key(OTHER_KENYA_KEY): ApiKey(source="source-b", countries=["kenya", "argentina"]),
        hash_api_key(ADMIN_KEY): ApiKey(source="admin", admin=True),
    }
    settings.country_languages = {"argentina": "es"}
    with TestClient(create_app(settings)) as test_client:
        yield test_client
