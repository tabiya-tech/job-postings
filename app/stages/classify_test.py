# pylint: disable=missing-function-docstring
from datetime import UTC, datetime

import httpx
import pytest

from app.db import CLASSIFIED_JOBS
from app.stages.classify import PROMOTED_EXTRA_KEYS, build_classified_document, classify
from app.stages.errors import StageError

NOW = datetime(2026, 11, 13, tzinfo=UTC)
RESULT = {"classification": {"entities": [{"label": "electrician"}]}, "metadata": {"model": "x"}}


def a_stored_posting(**fields):
    return {
        "job_id": "job-1",
        "source_platform": "source-a",
        "status": "active",
        "external_id": "A-1",
        "title": "Electrician",
        "description": "Install wiring.",
        "country": "kenya",
        "language": "en",
        **fields,
    }


def test_the_document_uses_the_names_the_reranker_reads():
    # GIVEN a posting with a region, a salary and a closed status
    posting = a_stored_posting(region="Nairobi County", salary="KES 40,000", status="closed")

    # WHEN the document is built
    document = build_classified_document(posting, RESULT, NOW)

    # THEN the fields carry the reranker's names
    assert document["province"] == "Nairobi County"
    assert document["salary_text"] == "KES 40,000"
    assert document["is_active"] is False
    assert document["classification"] == RESULT["classification"]
    assert document["job_id"] == "job-1"
    assert "region" not in document and "salary" not in document


def test_extra_cannot_replace_what_the_service_sets():
    # GIVEN a source that put service fields and everything else in `extra`
    posting = a_stored_posting(
        extra={
            "is_active": True,
            "job_id": "someone-elses",
            "source_platform": "source-b",
            "classification": {"fake": True},
            "_id": "x",
            "province": "Elsewhere",
            "translation": {"text_en": "other text"},
            "sector": "Construction",
            "zqf_min": 3,
        },
        status="closed",
        region="Nairobi County",
    )

    # WHEN the document is built
    document = build_classified_document(posting, RESULT, NOW)

    # THEN those fields are as the service set them
    assert document["is_active"] is False
    assert document["job_id"] == "job-1"
    assert document["source_platform"] == "source-a"
    assert document["classification"] == RESULT["classification"]
    assert document["province"] == "Nairobi County"
    assert "_id" not in document
    assert "translation" not in document
    # AND only the promoted keys came through
    assert (document["sector"], document["zqf_min"]) == ("Construction", 3)


def test_only_the_promoted_extra_keys_are_copied():
    # GIVEN every promoted key and one that is not
    extra = {key: "x" for key in PROMOTED_EXTRA_KEYS} | {"anything": "else"}

    # WHEN the document is built
    document = build_classified_document(a_stored_posting(extra=extra), RESULT, NOW)

    # THEN just the promoted ones are there
    assert all(document[key] == "x" for key in PROMOTED_EXTRA_KEYS)
    assert "anything" not in document


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_classify_sends_the_posting_and_stores_one_document_even_when_run_twice(db, settings):
    # GIVEN a stored posting and a classifier that answers
    await db["scraped_jobs"].insert_one(a_stored_posting())
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("x-api-key"), request.content))
        return httpx.Response(200, json=RESULT)

    settings.classifier_url = "http://classifier.test"
    settings.classifier_api_key = "secret"

    # WHEN the stage runs twice
    async with _client(handler) as client:
        assert await classify(db, client, settings, "job-1")
        assert await classify(db, client, settings, "job-1")

    # THEN it called the classifier with title, description and language, with the key
    url, key, body = seen[0]
    assert (url, key) == ("http://classifier.test/v1/classify", "secret")
    assert b'"language":"en"' in body.replace(b" ", b"")
    # AND there is one classified document
    assert await db[CLASSIFIED_JOBS].count_documents({"job_id": "job-1"}) == 1


async def test_classify_has_nothing_to_do_for_a_posting_that_is_gone(db, settings):
    async with _client(lambda request: httpx.Response(500)) as client:
        assert not await classify(db, client, settings, "missing")


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(500, text="secret upstream details"), (True, False, None)),
        (httpx.Response(422, text="secret upstream details"), (False, False, None)),
        (httpx.Response(429, headers={"retry-after": "30"}), (True, True, 30)),
        (httpx.Response(503), (True, True, None)),
    ],
)
async def test_classifier_errors_become_stage_errors_with_no_upstream_text(db, settings, response, expected):
    # GIVEN a classifier that answers with an error
    await db["scraped_jobs"].insert_one(a_stored_posting())

    # WHEN the stage runs
    async with _client(lambda request: response) as client:
        with pytest.raises(StageError) as raised:
            await classify(db, client, settings, "job-1")

    # THEN the error says whether to retry, and the reason holds no upstream text
    error = raised.value
    assert (error.retryable, error.busy, error.retry_after) == expected
    assert "secret" not in error.reason
    assert await db[CLASSIFIED_JOBS].count_documents({}) == 0


async def test_a_classifier_that_cannot_be_reached_is_retried(db, settings):
    await db["scraped_jobs"].insert_one(a_stored_posting())

    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    async with _client(handler) as client:
        with pytest.raises(StageError) as raised:
            await classify(db, client, settings, "job-1")

    assert raised.value.retryable
