# pylint: disable=missing-function-docstring
import asyncio
from datetime import UTC, datetime, timedelta

from app.db import SCRAPED_JOBS, TASKS
from app.tasks import queue


async def _posting(db, job_id="job-1"):
    await db[SCRAPED_JOBS].insert_one({"job_id": job_id, "stage": "queued"})


async def _task(db, job_id="job-1", stage="classify"):
    return await db[TASKS].find_one({"job_id": job_id, "stage": stage}, {"_id": 0})


async def _stage(db, job_id="job-1"):
    return (await db[SCRAPED_JOBS].find_one({"job_id": job_id}))["stage"]


async def test_two_concurrent_claimers_never_get_the_same_task(db):
    # GIVEN three queued tasks
    for n in range(3):
        await _posting(db, f"job-{n}")
        await queue.enqueue(db, f"job-{n}", "classify", max_attempts=3)

    # WHEN ten claimers race for them
    claimed = await asyncio.gather(*(queue.claim(db, ("classify",), lock_timeout_seconds=60) for _ in range(10)))

    # THEN each task went to exactly one of them
    got = [task.job_id for task in claimed if task is not None]
    assert sorted(got) == ["job-0", "job-1", "job-2"]


async def test_claim_takes_the_oldest_due_task_and_skips_stages_it_was_not_asked_for(db):
    # GIVEN a rank task that is older than a classify task
    await _posting(db, "a")
    await _posting(db, "b")
    await queue.enqueue(db, "a", "rank", max_attempts=3)
    await queue.enqueue(db, "b", "classify", max_attempts=3)

    # WHEN claiming classify tasks only
    task = await queue.claim(db, ("classify",), lock_timeout_seconds=60)

    # THEN it is the classify task, with an attempt counted, and the posting shows it is running
    assert (task.job_id, task.stage, task.attempts) == ("b", "classify", 1)
    assert await _stage(db, "b") == "classifying"
    assert await queue.claim(db, ("classify",), lock_timeout_seconds=60) is None


async def test_a_task_not_yet_due_is_not_claimed(db):
    # GIVEN a task that failed and waits for its backoff
    await _posting(db)
    await queue.enqueue(db, "job-1", "classify", max_attempts=3)
    task = await queue.claim(db, ("classify",), lock_timeout_seconds=60)
    await queue.fail(db, task, "classifier error (500)")

    # THEN it cannot be claimed yet, and keeps the reason
    assert await queue.claim(db, ("classify",), lock_timeout_seconds=60) is None
    stored = await _task(db)
    assert (stored["status"], stored["last_error"]) == ("pending", "classifier error (500)")
    assert stored["run_after"] > datetime.now(UTC) + timedelta(seconds=queue.BACKOFF_BASE_SECONDS - 5)


async def test_a_task_of_a_crashed_worker_comes_back_after_the_lock_expires(db):
    # GIVEN a task claimed by a worker that then died (lock already expired)
    await _posting(db)
    await queue.enqueue(db, "job-1", "classify", max_attempts=3)
    first = await queue.claim(db, ("classify",), lock_timeout_seconds=60)
    expired = datetime.now(UTC) - timedelta(seconds=1)
    await db[TASKS].update_one({"job_id": "job-1"}, {"$set": {"locked_until": expired}})

    # WHEN another worker claims
    second = await queue.claim(db, ("classify",), lock_timeout_seconds=60)

    # THEN it gets the task, with another attempt, and the first worker's claim is gone
    assert second.attempts == 2
    assert not await queue.holds_claim(db, first)
    assert await queue.holds_claim(db, second)
    assert not await queue.complete(db, first)


async def test_a_task_in_progress_is_not_claimed_before_its_lock_expires(db):
    await _posting(db)
    await queue.enqueue(db, "job-1", "classify", max_attempts=3)
    await queue.claim(db, ("classify",), lock_timeout_seconds=60)

    assert await queue.claim(db, ("classify",), lock_timeout_seconds=60) is None


async def test_a_task_fails_for_good_after_max_attempts(db):
    # GIVEN a task with two attempts allowed
    await _posting(db)
    await queue.enqueue(db, "job-1", "classify", max_attempts=2)

    # WHEN it fails twice
    outcomes = []
    for _ in range(2):
        await db[TASKS].update_one({"job_id": "job-1"}, {"$set": {"run_after": datetime.now(UTC)}})
        task = await queue.claim(db, ("classify",), lock_timeout_seconds=60)
        outcomes.append(await queue.fail(db, task, "classifier error (500)"))

    # THEN the second one is final, and the posting says so
    assert outcomes == ["retry", "failed"]
    assert (await _task(db))["status"] == "failed"
    assert await _stage(db) == "failed"


async def test_an_error_that_will_not_change_fails_at_once(db):
    await _posting(db)
    await queue.enqueue(db, "job-1", "classify", max_attempts=5)
    task = await queue.claim(db, ("classify",), lock_timeout_seconds=60)

    outcome = await queue.fail(db, task, "classifier rejected the posting (422)", retryable=False)

    assert outcome == "failed"
    assert (await _task(db))["attempts"] == 1


async def test_a_busy_service_puts_the_task_back_without_using_up_an_attempt(db):
    # GIVEN a task on its last attempt
    await _posting(db)
    await queue.enqueue(db, "job-1", "classify", max_attempts=1)
    task = await queue.claim(db, ("classify",), lock_timeout_seconds=60)

    # WHEN the classifier says it is busy
    outcome = await queue.fail(db, task, "classifier busy (429)", busy=True, retry_after=120)

    # THEN the task waits as long as it was told, and has all its attempts
    stored = await _task(db)
    assert (outcome, stored["status"], stored["attempts"]) == ("retry", "pending", 0)
    assert stored["run_after"] > datetime.now(UTC) + timedelta(seconds=100)


async def test_queueing_again_resets_the_task_and_takes_the_claim_from_a_running_worker(db):
    # GIVEN a task a worker is running
    await _posting(db)
    await queue.enqueue(db, "job-1", "classify", max_attempts=3)
    task = await queue.claim(db, ("classify",), lock_timeout_seconds=60)

    # WHEN the posting changes and is queued again
    await queue.enqueue(db, "job-1", "classify", max_attempts=3)

    # THEN there is still one task, pending with no attempts, and the running worker cannot complete it
    assert await db[TASKS].count_documents({"job_id": "job-1"}) == 1
    stored = await _task(db)
    assert (stored["status"], stored["attempts"]) == ("pending", 0)
    assert not await queue.complete(db, task)
    assert (await _task(db))["status"] == "pending"


def test_backoff_doubles_up_to_the_cap():
    assert [queue.backoff_seconds(n) for n in (1, 2, 3)] == [30, 60, 120]
    assert queue.backoff_seconds(30) == queue.BACKOFF_CAP_SECONDS


async def test_requeueing_failed_runs_only_the_stages_that_failed_again_from_scratch(db):
    # GIVEN a posting whose classify worked and whose rank ran out of attempts
    await _posting(db)
    await queue.enqueue(db, "job-1", "classify", max_attempts=2)
    await queue.enqueue(db, "job-1", "rank", max_attempts=2)
    await db[TASKS].update_one({"job_id": "job-1", "stage": "classify"}, {"$set": {"status": "done"}})
    await db[TASKS].update_one(
        {"job_id": "job-1", "stage": "rank"}, {"$set": {"status": "failed", "attempts": 2, "last_error": "x"}}
    )

    # WHEN it is requeued
    assert await queue.requeue_failed(db, "job-1", max_attempts=5)

    # THEN rank is pending with a fresh start and the new attempt limit, and classify was left alone
    rank = await _task(db, stage="rank")
    assert (rank["status"], rank["attempts"], rank["max_attempts"], rank["last_error"]) == ("pending", 0, 5, None)
    assert (await _task(db, stage="classify"))["status"] == "done"


async def test_requeueing_a_posting_with_nothing_failed_does_nothing(db):
    await _posting(db)
    await queue.enqueue(db, "job-1", "classify", max_attempts=2)

    assert not await queue.requeue_failed(db, "job-1", max_attempts=5)
