# pylint: disable=missing-function-docstring
import pytest
from fastapi.testclient import TestClient

from app import db as database
from app.server import create_app


@pytest.fixture(name="client")
def fixture_client(settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_health_is_ok_when_mongodb_answers(client):
    # WHEN the health check is called
    response = client.get("/health")

    # THEN the service reports itself and MongoDB as ok
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "mongodb": "ok"}


def test_health_is_503_when_mongodb_does_not_answer(client, monkeypatch):
    # GIVEN MongoDB stops answering after the start
    async def failing_ping(_db):
        return False

    monkeypatch.setattr(database, "ping", failing_ping)

    # WHEN the health check is called
    response = client.get("/health")

    # THEN the service reports itself unavailable
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "mongodb": "unreachable"}


def test_start_creates_the_indexes(client):
    # GIVEN the API has started (the client fixture runs its startup)
    # THEN the indexes exist
    indexes = client.portal.call(_index_names, client.app.state.db)
    assert "job_id_stage_unique" in indexes


def _preflight(client, origin):
    return client.options(
        "/job-postings",
        headers={"Origin": origin, "Access-Control-Request-Method": "POST",
                 "Access-Control-Request-Headers": "x-api-key, content-type"},
    )


def test_cross_origin_calls_are_refused_by_default(client):
    assert "access-control-allow-origin" not in _preflight(client, "http://localhost:8091").headers


def test_configured_origins_may_call_the_api(settings):
    # GIVEN a local test page's origin is allowed
    settings.cors_allow_origins = ["http://localhost:8091"]
    with TestClient(create_app(settings)) as client:
        # THEN that page may send postings with an API key, and other origins may not
        assert _preflight(client, "http://localhost:8091").headers["access-control-allow-origin"] == (
            "http://localhost:8091"
        )
        assert "access-control-allow-origin" not in _preflight(client, "http://evil.example.org").headers


async def _index_names(db):
    return {index["name"] async for index in await db[database.TASKS].list_indexes()}
