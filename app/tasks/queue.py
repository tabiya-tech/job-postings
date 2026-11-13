import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from pymongo import ASCENDING, ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase

from app.db import SCRAPED_JOBS, TASKS

Stage = Literal["classify", "rank"]

# How far a posting got. Stored on the posting (so postings can be listed and filtered by it) and kept current by
# this module whenever a task changes.
PostingStage = Literal["queued", "classifying", "ranking", "done", "failed"]

_POSTING_STAGE_WHEN_QUEUED: dict[Stage, PostingStage] = {"classify": "queued", "rank": "ranking"}
_POSTING_STAGE_WHEN_RUNNING: dict[Stage, PostingStage] = {"classify": "classifying", "rank": "ranking"}

# A failed task waits this long before its next attempt, doubling each time, up to the cap.
BACKOFF_BASE_SECONDS = 30
BACKOFF_CAP_SECONDS = 3600
# When the other service is busy and did not say for how long.
BUSY_RETRY_SECONDS = 60


@dataclass(frozen=True)
class Task:
    """A claimed task. `token` proves the claim: it is gone if the task was queued again or taken over meanwhile."""

    job_id: str
    stage: Stage
    attempts: int
    max_attempts: int
    token: str


def backoff_seconds(attempts: int) -> float:
    """How long to wait after the `attempts`-th failed attempt."""
    return min(BACKOFF_BASE_SECONDS * 2 ** max(attempts - 1, 0), BACKOFF_CAP_SECONDS)


async def enqueue(db: AsyncDatabase, job_id: str, stage: Stage, *, max_attempts: int) -> None:
    """Queue `stage` for a posting, or queue it again from scratch.

    There is one task per posting and stage, so queueing a posting that already has one resets it to `pending` with
    no attempts, instead of adding a second task. A worker that is running the old one loses its claim.
    """
    now = datetime.now(UTC)
    await db[TASKS].update_one(
        {"job_id": job_id, "stage": stage},
        {
            "$set": {
                "status": "pending",
                "attempts": 0,
                "max_attempts": max_attempts,
                "run_after": now,
                "locked_until": None,
                "lock_token": None,
                "last_error": None,
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )
    await set_posting_stage(db, job_id, _POSTING_STAGE_WHEN_QUEUED[stage])


async def requeue_failed(db: AsyncDatabase, job_id: str, *, max_attempts: int) -> bool:
    """Queue again every stage of a posting that ran out of attempts, from scratch. Returns whether there was one.

    A stage that works is not run again: if ranking failed, the posting is ranked again, not classified again.
    """
    stages = [task["stage"] async for task in db[TASKS].find({"job_id": job_id, "status": "failed"}, {"stage": 1})]
    for stage in stages:
        await enqueue(db, job_id, stage, max_attempts=max_attempts)
    return bool(stages)


async def claim(db: AsyncDatabase, stages: tuple[Stage, ...], *, lock_timeout_seconds: int) -> Task | None:
    """Take the oldest task that is due, or `None`.

    One `find_one_and_update`, so two workers never get the same task. A task another worker took but did not finish
    within the lock timeout (it crashed) is due again.
    """
    now = datetime.now(UTC)
    token = str(uuid.uuid4())
    doc = await db[TASKS].find_one_and_update(
        {
            "stage": {"$in": list(stages)},
            "$or": [
                {"status": "pending", "run_after": {"$lte": now}},
                {"status": "processing", "locked_until": {"$lt": now}},
            ],
        },
        {
            "$set": {
                "status": "processing",
                "locked_until": now + timedelta(seconds=lock_timeout_seconds),
                "lock_token": token,
                "updated_at": now,
            },
            "$inc": {"attempts": 1},
        },
        sort=[("run_after", ASCENDING)],
        return_document=ReturnDocument.AFTER,
    )
    if doc is None:
        return None
    await set_posting_stage(db, doc["job_id"], _POSTING_STAGE_WHEN_RUNNING[doc["stage"]])
    return Task(doc["job_id"], doc["stage"], doc["attempts"], doc["max_attempts"], token)


async def holds_claim(db: AsyncDatabase, task: Task) -> bool:
    """Whether `task` is still the claim on its row: false once it was queued again or another worker took it."""
    return await db[TASKS].count_documents({"job_id": task.job_id, "stage": task.stage, "lock_token": task.token}) > 0


async def complete(db: AsyncDatabase, task: Task, outcome: str | None = None) -> bool:
    """Mark a claimed task done, with what came of it (`nothing_to_rank`, ...). `False` when the claim was lost, in
    which case the newer claim or queueing decides."""
    now = datetime.now(UTC)
    result = await db[TASKS].update_one(
        {"job_id": task.job_id, "stage": task.stage, "lock_token": task.token},
        {
            "$set": {
                "status": "done",
                "outcome": outcome,
                "locked_until": None,
                "lock_token": None,
                "last_error": None,
                "updated_at": now,
            }
        },
    )
    return result.modified_count > 0


async def fail(  # pylint: disable=too-many-arguments
    db: AsyncDatabase,
    task: Task,
    reason: str,
    *,
    retryable: bool = True,
    busy: bool = False,
    retry_after: float | None = None,
) -> Literal["retry", "failed", "lost"]:
    """Record that an attempt did not work.

    The task goes back to `pending` after a backoff, or becomes `failed` when it cannot succeed (`retryable` is
    false) or is out of attempts. `busy` means the other service is overloaded: the task goes back after
    `retry_after` (or a minute) without using up an attempt. Returns what became of the task; `lost` when the claim
    was already gone.
    """
    now = datetime.now(UTC)
    if not retryable or (task.attempts >= task.max_attempts and not busy):
        status: Literal["retry", "failed"] = "failed"
        update: dict = {"$set": {"status": "failed", "run_after": now}}
    else:
        status = "retry"
        if busy:
            delay = retry_after if retry_after is not None else BUSY_RETRY_SECONDS
        else:
            delay = backoff_seconds(task.attempts)
        update = {"$set": {"status": "pending", "run_after": now + timedelta(seconds=delay)}}
        if busy:
            update["$inc"] = {"attempts": -1}
    update["$set"].update({"locked_until": None, "lock_token": None, "last_error": reason, "updated_at": now})

    result = await db[TASKS].update_one(
        {"job_id": task.job_id, "stage": task.stage, "lock_token": task.token}, update
    )
    if result.modified_count == 0:
        return "lost"
    await set_posting_stage(db, task.job_id, "failed" if status == "failed" else _POSTING_STAGE_WHEN_QUEUED[task.stage])
    return status


async def set_posting_stage(db: AsyncDatabase, job_id: str, stage: PostingStage) -> None:
    """Record how far a posting got."""
    await db[SCRAPED_JOBS].update_one({"job_id": job_id}, {"$set": {"stage": stage}})
