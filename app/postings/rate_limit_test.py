# pylint: disable=missing-function-docstring
import pytest
from fastapi.testclient import TestClient

from app import rate_limit
from app.config import RateLimit
from app.postings.test_support import KENYA_KEY, OTHER_KENYA_KEY, a_posting, get, post
from app.server import create_app


@pytest.fixture(autouse=True)
def _fixed_minute(monkeypatch):
    """Every request falls in the same minute, so a test never straddles a boundary."""
    monkeypatch.setattr(rate_limit, "current_window", lambda: 29_000_000)


def test_requests_over_the_limit_get_429_with_retry_after(client):
    # GIVEN a limit of three requests a minute
    client.app.state.settings.rate_limit_requests_per_minute = 3

    # WHEN a key sends four
    statuses = [get(client, "/job-postings").status_code for _ in range(4)]

    # THEN the fourth is refused, and says when to come back
    assert statuses == [200, 200, 200, 429]
    refused = get(client, "/job-postings")
    assert 1 <= int(refused.headers["retry-after"]) <= 60
    # AND another key is not affected
    assert get(client, "/job-postings", OTHER_KENYA_KEY).status_code == 200


def test_postings_are_counted_not_just_requests(client):
    client.app.state.settings.rate_limit_postings_per_minute = 5

    assert post(client, [a_posting() for _ in range(3)]).status_code == 202
    over = post(client, [a_posting() for _ in range(3)])

    assert over.status_code == 429
    assert "postings" in over.json()["detail"]
    # AND nothing of the refused request was stored
    assert len(get(client, "/job-postings").json()["items"]) == 3


def test_the_limit_holds_across_two_api_containers(client, settings):
    # GIVEN two API instances on the same database
    settings.rate_limit_requests_per_minute = 4
    with TestClient(create_app(settings)) as other:
        # WHEN the same key alternates between them
        statuses = [get(c, "/job-postings").status_code for c in (client, other, client, other, client)]

    # THEN the limit counts both
    assert statuses == [200, 200, 200, 200, 429]


def test_a_key_can_override_the_defaults(client):
    settings = client.app.state.settings
    settings.rate_limit_requests_per_minute = 1
    next(iter(key for key in settings.api_keys.values() if key.source == "source-b")).rate_limit = RateLimit(
        requests_per_minute=3
    )

    assert [get(client, "/job-postings", OTHER_KENYA_KEY).status_code for _ in range(4)] == [200, 200, 200, 429]
    assert [get(client, "/job-postings", KENYA_KEY).status_code for _ in range(2)] == [200, 429]


def test_requests_without_a_valid_key_get_401_and_use_up_nothing(client):
    client.app.state.settings.rate_limit_requests_per_minute = 1

    assert get(client, "/job-postings", "wrong").status_code == 401
    assert get(client, "/job-postings", KENYA_KEY).status_code == 200
