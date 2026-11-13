from datetime import UTC, date, datetime
from typing import Literal

from pymongo.asynchronous.database import AsyncDatabase

from app.db import CLASSIFIED_JOBS, RANKED_JOBS, SCRAPED_JOBS

ClosedReason = Literal["expired", "closed_by_source"]

_BATCH = 500


async def _set_is_active(db: AsyncDatabase, job_ids: list[str], active: bool) -> None:
    """The matching engine reads `is_active` from the stage documents, so they follow the posting's status."""
    for collection in (CLASSIFIED_JOBS, RANKED_JOBS):
        await db[collection].update_many({"job_id": {"$in": job_ids}}, {"$set": {"is_active": active}})


async def close_postings(db: AsyncDatabase, job_ids: list[str], reason: ClosedReason) -> int:
    """Close the active postings among `job_ids`. Returns how many this call closed.

    Safe to repeat and to run from several workers at once: only a posting that is still active is closed, by one
    update, so it is counted once. The stage documents go first: a crash in between leaves a posting that is still
    active but hidden from matching, which the next expiry pass or close request puts right.
    """
    if not job_ids:
        return 0
    await _set_is_active(db, job_ids, False)
    now = datetime.now(UTC)
    result = await db[SCRAPED_JOBS].update_many(
        {"job_id": {"$in": job_ids}, "status": "active"},
        {"$set": {"status": "closed", "closed_at": now, "closed_reason": reason, "updated_at": now}},
    )
    return result.modified_count


async def reopen_posting(db: AsyncDatabase, job_id: str) -> None:
    """Make a closed posting live again. The posting first: a crash in between leaves it live but hidden from
    matching, never closed but shown."""
    now = datetime.now(UTC)
    await db[SCRAPED_JOBS].update_one(
        {"job_id": job_id, "status": "closed"},
        {"$set": {"status": "active", "updated_at": now}, "$unset": {"closed_at": "", "closed_reason": ""}},
    )
    await _set_is_active(db, [job_id], True)


async def expire_postings(db: AsyncDatabase, today: date | None = None) -> int:
    """Close every active posting whose `closing_date` is before `today` (UTC). Returns how many this call closed."""
    today = today or datetime.now(UTC).date()
    closed = 0
    while True:
        # Dates are stored as ISO text, which sorts like dates.
        due = (
            await db[SCRAPED_JOBS]
            .find({"status": "active", "closing_date": {"$lt": today.isoformat()}}, {"job_id": 1})
            .limit(_BATCH)
            .to_list()
        )
        job_ids = [document["job_id"] for document in due if document.get("job_id")]
        if not job_ids:
            return closed
        closed += await close_postings(db, job_ids, "expired")


def is_live(closing_date: str | None, today: date | None = None) -> bool:
    """Whether a posting with this `closing_date` (ISO text, or none) is still open."""
    return closing_date is None or date.fromisoformat(closing_date) >= (today or datetime.now(UTC).date())


async def close_for_source(
    db: AsyncDatabase, source: str, field: Literal["job_id", "external_id"], values: list[str]
) -> list[tuple[str | None, str | None, Literal["closed", "already_closed", "not_found"]]]:
    """Close the source's own postings named by `values`. One `(job_id, external_id, outcome)` per value, in order."""
    results = []
    for value in values:
        posting = await db[SCRAPED_JOBS].find_one(
            {"source_platform": source, field: value}, {"job_id": 1, "external_id": 1, "status": 1}
        )
        if posting is None or not posting.get("job_id"):
            results.append((None, value if field == "external_id" else None, "not_found"))
            continue
        # A posting that looks closed is still passed on: it repairs a close that was cut short.
        closed = await close_postings(db, [posting["job_id"]], "closed_by_source")
        outcome = "closed" if closed else "already_closed"
        results.append((posting["job_id"], posting.get("external_id"), outcome))
    return results
