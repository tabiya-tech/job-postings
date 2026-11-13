# pylint: disable=missing-function-docstring
import json

import pytest
from pydantic import ValidationError

from app.config import ApiKey, Settings

HASH_A = "a" * 64
HASH_B = "b" * 64


def test_api_keys_are_read_from_the_environment_and_the_file(tmp_path, monkeypatch):
    # GIVEN one key in a file and one in the environment
    keys_file = tmp_path / "api_keys.json"
    keys_file.write_text(json.dumps({HASH_A: {"source": "source-a", "countries": ["kenya"]}}))
    monkeypatch.setenv("API_KEYS", json.dumps({HASH_B: {"source": "admin", "admin": True}}))
    monkeypatch.setenv("API_KEYS_FILE", str(keys_file))

    # WHEN the settings are read
    settings = Settings(mongodb_uri="mongodb://unused", _env_file=None)

    # THEN both keys are known
    assert settings.api_keys == {
        HASH_A: ApiKey(source="source-a", countries=["kenya"]),
        HASH_B: ApiKey(source="admin", admin=True),
    }


def test_the_language_comes_from_the_country_or_the_default(monkeypatch):
    monkeypatch.setenv("COUNTRY_LANGUAGES", json.dumps({"argentina": "es"}))

    settings = Settings(mongodb_uri="mongodb://unused", _env_file=None)

    assert settings.language_for("argentina") == "es"
    assert settings.language_for("kenya") == "en"


@pytest.mark.parametrize("not_a_hash", ["the-key-itself", "A" * 64], ids=["raw-key", "upper-case-hash"])
def test_an_api_key_entry_that_is_not_a_sha256_stops_the_start(monkeypatch, not_a_hash):
    # GIVEN an entry that could never match a request
    monkeypatch.setenv("API_KEYS", json.dumps({not_a_hash: {"source": "source-a", "countries": ["kenya"]}}))

    # THEN the settings refuse it, without repeating the entry (it may be a key)
    with pytest.raises(ValidationError) as error:
        Settings(mongodb_uri="mongodb://unused", _env_file=None)
    assert "SHA-256" in str(error.value)
    assert not_a_hash not in str(error.value)


def test_the_countries_of_a_key_are_slugs():
    assert ApiKey(source="source-a", countries=["Kenya", " South Africa "]).countries == ["kenya", "south_africa"]
