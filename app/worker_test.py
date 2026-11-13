# pylint: disable=missing-function-docstring
import asyncio
from datetime import UTC, datetime, timedelta

import httpx

from app import db as database
from app.db import CLASSIFIED_JOBS, RANKED_JOBS, SCRAPED_JOBS, TASKS
from app.tasks import queue
from app.worker import process, run_loops, run_worker
from app.stages.classify import classify

RESULT = {"classification": {"entities": []}, "metadata": {}}


async def test_worker_creates_indexes_and_stops_when_asked(settings):
    # GIVEN a running worker
    stop = asyncio.Event()
    worker = asyncio.create_task(run_worker(settings, stop))
    await asyncio.sleep(0.2)

    # WHEN it is asked to stop
    stop.set()

    # THEN it stops promptly
    await asyncio.wait_for(worker, timeout=2)

    # AND it created the indexes on start
    client = database.create_client(settings)
    try:
        db = client[settings.mongodb_database_name]
        names = {index["name"] async for index in await db[database.TASKS].list_indexes()}
        assert "job_id_stage_unique" in names
        await client.drop_database(settings.mongodb_database_name)
    finally:
        await client.close()


async def _queued_posting(db, settings, job_id="job-1"):
    await db[SCRAPED_JOBS].insert_one(
        {
            "job_id": job_id,
            "source_platform": "source-a",
            "status": "active",
            "stage": "queued",
            "title": "Electrician",
            "description": "Install wiring.",
            "country": "kenya",
            "language": "en",
        }
    )
    await queue.enqueue(db, job_id, "classify", max_attempts=settings.max_attempts)


async def _wait_until(condition, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not await condition():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.02)


def _services(request: httpx.Request) -> httpx.Response:
    """A classifier and a reranker that both answer."""
    if request.url.path == "/v1/classify":
        return httpx.Response(200, json=RESULT)
    return httpx.Response(200, json={"ranked_document": {"job_id": "x", "is_active": True, "llm_job_attributes": {}}})


async def test_the_worker_takes_a_posting_through_classify_and_rank(db, settings):
    # GIVEN a queued posting, a classifier and a reranker that answer
    await _queued_posting(db, settings)
    settings.worker_concurrency = 2
    stop = asyncio.Event()
    async with httpx.AsyncClient(transport=httpx.MockTransport(_services)) as http:
        worker = asyncio.create_task(run_loops(db, http, settings, stop))

        # WHEN the worker runs
        async def ranked():
            return (await db[TASKS].find_one({"job_id": "job-1", "stage": "rank"}) or {}).get("status") == "done"

        await _wait_until(ranked)
        stop.set()
        await asyncio.wait_for(worker, timeout=2)

    # THEN both documents are stored and the posting is done
    assert await db[CLASSIFIED_JOBS].count_documents({"job_id": "job-1"}) == 1
    assert await db[RANKED_JOBS].count_documents({"job_id": "job-1"}) == 1
    assert (await db[SCRAPED_JOBS].find_one({"job_id": "job-1"}))["stage"] == "done"


async def _run_once(db, settings, handler):
    task = await queue.claim(db, ("classify",), lock_timeout_seconds=60)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await process(db, http, settings, task, classify)
    return await db[TASKS].find_one({"job_id": "job-1", "stage": "classify"})


async def test_a_classifier_500_is_retried_and_a_422_fails(db, settings):
    # GIVEN a queued posting and a classifier that fails
    await _queued_posting(db, settings)

    # WHEN it answers 500
    stored = await _run_once(db, settings, lambda r: httpx.Response(500))

    # THEN the task waits for another attempt
    assert (stored["status"], stored["last_error"]) == ("pending", "classifier error (500)")

    # WHEN it then answers 422
    await db[TASKS].update_one({"job_id": "job-1"}, {"$set": {"run_after": datetime.now(UTC)}})
    stored = await _run_once(db, settings, lambda r: httpx.Response(422))

    # THEN the task fails at once
    assert stored["status"] == "failed"
    assert (await db[SCRAPED_JOBS].find_one({"job_id": "job-1"}))["stage"] == "failed"


async def test_a_429_does_not_use_up_an_attempt(db, settings):
    await _queued_posting(db, settings)

    stored = await _run_once(db, settings, lambda r: httpx.Response(429, headers={"retry-after": "30"}))

    assert (stored["status"], stored["attempts"]) == ("pending", 0)
    assert stored["run_after"] > datetime.now(UTC) + timedelta(seconds=20)


async def test_a_task_that_crashed_on_its_last_attempt_fails_when_claimed_again(db, settings):
    # GIVEN a task claimed past its attempts by a worker that died each time
    await _queued_posting(db, settings)
    await db[TASKS].update_one(
        {"job_id": "job-1"},
        {
            "$set": {
                "status": "processing",
                "attempts": settings.max_attempts,
                "locked_until": datetime(2000, 1, 1, tzinfo=UTC),
            }
        },
    )

    # WHEN it is claimed once more
    stored = await _run_once(db, settings, lambda r: httpx.Response(200, json=RESULT))

    # THEN it is marked failed instead of run
    assert stored["status"] == "failed"
    assert await db[CLASSIFIED_JOBS].count_documents({}) == 0


async def test_a_posting_that_changed_while_classifying_is_not_marked_done_or_ranked(db, settings):
    # GIVEN a worker busy classifying
    await _queued_posting(db, settings)

    async def edit_during_call(_request):
        # the posting is edited and queued again while the classifier works
        await queue.enqueue(db, "job-1", "classify", max_attempts=settings.max_attempts)
        return httpx.Response(200, json=RESULT)

    stored = await _run_once(db, settings, edit_during_call)

    # THEN the new task stays pending and no rank task was queued from the stale result
    assert stored["status"] == "pending"
    assert await db[TASKS].count_documents({"stage": "rank"}) == 0
