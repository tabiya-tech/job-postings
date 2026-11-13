# pylint: disable=missing-function-docstring
from datetime import UTC, datetime

import httpx
import pytest

from app.db import CLASSIFIED_JOBS, RANKED_JOBS, SCRAPED_JOBS
from app.stages.errors import StageError
from app.stages.rank import build_ranked_document, rank

NOW = datetime(2026, 11, 13, tzinfo=UTC)
RANKED = {"job_id": "job-1", "is_active": True, "processed_at": "2026-11-13T10:00:00+00:00", "llm_job_attributes": {}}


async def _stored(db, status="active", classified=True):
    await db[SCRAPED_JOBS].insert_one({"job_id": "job-1", "source_platform": "source-a", "status": status})
    if classified:
        await db[CLASSIFIED_JOBS].insert_one(
            {"job_id": "job-1", "title": "Electrician", "language": "es", "classified_at": NOW, "is_active": True}
        )


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_a_closed_posting_is_not_active_whatever_the_reranker_says():
    document = build_ranked_document({"job_id": "job-1", "status": "closed"}, RANKED, NOW)

    assert document["is_active"] is False
    assert document["ranked_at"] == NOW
    assert document["processed_at"] == datetime(2026, 11, 13, 10, tzinfo=UTC)


async def test_rank_sends_the_classified_document_in_its_language_and_stores_one_document(db, settings):
    # GIVEN a classified posting and a reranker that answers
    await _stored(db)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, dict(request.url.params), request.content))
        return httpx.Response(200, json={"ranked_document": RANKED})

    # WHEN the stage runs twice
    async with _client(handler) as client:
        assert await rank(db, client, settings, "job-1") == "ranked"
        assert await rank(db, client, settings, "job-1") == "ranked"

    # THEN the reranker got the document (without Mongo's `_id`) for the posting's language
    path, params, body = seen[0]
    assert (path, params) == ("/rank", {"language": "es"})
    assert b'"title": "Electrician"' in body and b'"_id"' not in body
    # AND there is one ranked document
    assert await db[RANKED_JOBS].count_documents({"job_id": "job-1"}) == 1


async def test_a_posting_closed_in_the_meantime_stays_closed(db, settings):
    # GIVEN a posting that is closed while the reranker works
    await _stored(db)

    async def handler(_request):
        await db[SCRAPED_JOBS].update_one({"job_id": "job-1"}, {"$set": {"status": "closed"}})
        return httpx.Response(200, json={"ranked_document": RANKED})

    # WHEN it is ranked
    async with _client(handler) as client:
        await rank(db, client, settings, "job-1")

    # THEN the stored document is not active
    assert (await db[RANKED_JOBS].find_one({"job_id": "job-1"}))["is_active"] is False


async def test_nothing_to_rank_is_an_outcome_not_a_failure(db, settings):
    await _stored(db)

    async with _client(lambda request: httpx.Response(200, json={"ranked_document": None})) as client:
        outcome = await rank(db, client, settings, "job-1")

    assert outcome == "nothing_to_rank"
    assert await db[RANKED_JOBS].count_documents({}) == 0


async def test_rank_has_nothing_to_do_without_a_classified_document(db, settings):
    await _stored(db, classified=False)

    async with _client(lambda request: httpx.Response(500)) as client:
        assert await rank(db, client, settings, "job-1") is None


@pytest.mark.parametrize(
    ("status", "expected"),
    [(500, (True, False)), (422, (False, False)), (503, (True, True))],
)
async def test_reranker_errors_say_what_to_do_and_hold_no_upstream_text(db, settings, status, expected):
    await _stored(db)

    async with _client(lambda request: httpx.Response(status, text="secret upstream details")) as client:
        with pytest.raises(StageError) as raised:
            await rank(db, client, settings, "job-1")

    assert (raised.value.retryable, raised.value.busy) == expected
    assert raised.value.reason.startswith("reranker") and "secret" not in raised.value.reason
