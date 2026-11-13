import logging

from pymongo import ASCENDING, DESCENDING, AsyncMongoClient, IndexModel
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import PyMongoError

from app.config import Settings

logger = logging.getLogger(__name__)

SCRAPED_JOBS = "scraped_jobs"
CLASSIFIED_JOBS = "classified_jobs"
RANKED_JOBS = "ranked_jobs"
TASKS = "tasks"
RATE_LIMITS = "rate_limits"

# Documents written by the old batch jobs have no `job_id`, so the `job_id` indexes only cover documents that have one.
_HAS_JOB_ID = {"job_id": {"$exists": True}}

INDEXES: dict[str, list[IndexModel]] = {
    SCRAPED_JOBS: [
        IndexModel([("job_id", ASCENDING)], unique=True, partialFilterExpression=_HAS_JOB_ID, name="job_id_unique"),
        # A source's own ID identifies its posting. Partial because documents written by the old batch jobs, and
        # postings stored before `external_id` was required, have none.
        IndexModel(
            [("source_platform", ASCENDING), ("external_id", ASCENDING)],
            unique=True,
            partialFilterExpression={"external_id": {"$type": "string"}},
            name="source_platform_external_id_unique",
        ),
        # Listing a source's postings newest first, page by page; the second one for admin keys across sources.
        IndexModel(
            [("source_platform", ASCENDING), ("created_at", DESCENDING), ("job_id", DESCENDING)],
            name="source_platform_created_at_job_id",
        ),
        IndexModel([("created_at", DESCENDING), ("job_id", DESCENDING)], name="created_at_job_id"),
    ],
    CLASSIFIED_JOBS: [
        IndexModel([("job_id", ASCENDING)], unique=True, partialFilterExpression=_HAS_JOB_ID, name="job_id_unique"),
    ],
    RANKED_JOBS: [
        IndexModel([("job_id", ASCENDING)], unique=True, partialFilterExpression=_HAS_JOB_ID, name="job_id_unique"),
    ],
    TASKS: [
        IndexModel([("job_id", ASCENDING), ("stage", ASCENDING)], unique=True, name="job_id_stage_unique"),
        IndexModel([("status", ASCENDING), ("run_after", ASCENDING)], name="status_run_after"),
        IndexModel([("status", ASCENDING), ("locked_until", ASCENDING)], name="status_locked_until"),
    ],
    RATE_LIMITS: [
        IndexModel([("expires_at", ASCENDING)], expireAfterSeconds=0, name="expires_at_ttl"),
    ],
}


def create_client(settings: Settings) -> AsyncMongoClient:
    """The one MongoDB client of a process; datetimes come back timezone-aware (UTC)."""
    return AsyncMongoClient(
        settings.mongodb_uri,
        tz_aware=True,
        appname="job-postings",
        serverSelectionTimeoutMS=settings.mongodb_server_selection_timeout_ms,
    )


async def ensure_indexes(db: AsyncDatabase) -> None:
    """Create every index the service relies on. Safe to run on every start: existing indexes are left alone."""
    for collection, indexes in INDEXES.items():
        await db[collection].create_indexes(indexes)
    logger.info("Indexes ensured on %s", ", ".join(INDEXES))


async def ping(db: AsyncDatabase) -> bool:
    """Whether MongoDB answers within the server selection timeout."""
    try:
        await db.command("ping")
        return True
    except PyMongoError:
        logger.exception("MongoDB ping failed")
        return False
