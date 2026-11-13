import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import PyMongoError

from app import db as database
from app.config import Settings
from app.postings.closing import expire_postings
from app.stages.classify import classify
from app.stages.rank import rank
from app.stages.errors import StageError
from app.tasks import queue
from app.tasks.queue import Stage, Task

logger = logging.getLogger(__name__)

# What a stage does with a posting. It returns what came of it, or `None` when there was nothing to do (the posting is
# gone).
# The classified document is written before the next stage is queued, so a crash in between only repeats this stage.
Handler = Callable[[AsyncDatabase, httpx.AsyncClient, Settings, str], Awaitable[str | None]]


def _handlers() -> dict[Stage, Handler]:
    return {"classify": classify, "rank": rank}


async def process(
    db: AsyncDatabase, client: httpx.AsyncClient, settings: Settings, task: Task, handler: Handler
) -> None:
    """Run one claimed task and record how it went."""
    if task.attempts > task.max_attempts:
        # Claimed again after a crash, with no attempts left.
        await queue.fail(db, task, "worker stopped before finishing", retryable=False)
        return
    try:
        outcome = await handler(db, client, settings, task.job_id)
    except StageError as error:
        result = await queue.fail(
            db, task, error.reason, retryable=error.retryable, busy=error.busy, retry_after=error.retry_after
        )
        logger.warning("%s of %s: %s (%s)", task.stage, task.job_id, error.reason, result)
        return
    except Exception:  # pylint: disable=broad-exception-caught
        # A bug or a database error. The source sees a fixed text; the log has the details.
        logger.exception("%s of %s failed", task.stage, task.job_id)
        await queue.fail(db, task, "internal error")
        return

    if outcome is None or not await queue.holds_claim(db, task):
        # The posting is gone, or it changed while this ran and its new task covers it.
        await queue.complete(db, task, outcome or "stale")
        return
    if task.stage == "classify":
        await queue.enqueue(db, task.job_id, "rank", max_attempts=task.max_attempts)
    if await queue.complete(db, task, outcome) and task.stage == "rank":
        await queue.set_posting_stage(db, task.job_id, "done")


async def _claim_loop(
    db: AsyncDatabase,
    client: httpx.AsyncClient,
    settings: Settings,
    stop: asyncio.Event,
    handlers: dict[Stage, Handler],
) -> None:
    stages = tuple(handlers)
    while not stop.is_set():
        task = await queue.claim(db, stages, lock_timeout_seconds=settings.lock_timeout_seconds)
        if task is None:
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.poll_interval_seconds)
            except TimeoutError:
                pass
            continue
        await process(db, client, settings, task, handlers[task.stage])


async def _expiry_loop(db: AsyncDatabase, settings: Settings, stop: asyncio.Event) -> None:
    """Close postings whose closing date has passed, every `expiry_interval_minutes`. Several workers doing this at
    once is harmless: a posting is closed once."""
    while not stop.is_set():
        try:
            closed = await expire_postings(db)
            if closed:
                logger.info("Closed %d expired postings", closed)
        except PyMongoError:
            logger.exception("Expiry pass failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.expiry_interval_minutes * 60)
        except TimeoutError:
            pass


async def run_worker(settings: Settings, stop: asyncio.Event) -> None:
    """Run `worker_concurrency` claim loops until `stop` is set."""
    mongo = database.create_client(settings)
    try:
        db = mongo[settings.mongodb_database_name]
        await database.ensure_indexes(db)
        async with httpx.AsyncClient() as http:
            await run_loops(db, http, settings, stop)
    finally:
        await mongo.close()


async def run_loops(db: AsyncDatabase, http: httpx.AsyncClient, settings: Settings, stop: asyncio.Event) -> None:
    """The claim loops on their own, for tests that bring their own database and HTTP client."""
    logger.info("Worker started with %d claim loops", settings.worker_concurrency)
    handlers = _handlers()
    loops = (_claim_loop(db, http, settings, stop, handlers) for _ in range(settings.worker_concurrency))
    await asyncio.gather(*loops, _expiry_loop(db, settings, stop))
    logger.info("Worker stopped")
