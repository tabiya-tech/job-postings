import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pymongo import ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from app.config import Settings
from app.db import SCRAPED_JOBS
from app.postings.closing import close_postings, is_live, reopen_posting
from app.postings.models import JobPosting
from app.tasks.queue import enqueue, requeue_failed

SaveStatus = Literal["created", "updated", "unchanged"]

# What a posting says. A change to any of these gives the posting a new content hash and sends it through the stages
# again, since the classified and ranked documents carry these fields.
# (pylint does not see that pydantic's `model_fields` is a dict on the class.)
CONTENT_FIELDS = tuple(
    name for name in JobPosting.model_fields if name != "external_id"  # pylint: disable=not-an-iterable
)


def content_hash(document: dict[str, Any]) -> str:
    """Hash of what the posting says, to tell a real change from a resend."""
    content = {name: document.get(name) for name in CONTENT_FIELDS}
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


async def save_posting(
    db: AsyncDatabase, settings: Settings, source: str, posting: JobPosting
) -> tuple[str, SaveStatus]:
    """Store a posting for `source` and queue it for classification when it is new or changed.

    A posting that arrives already past its `closing_date` is stored closed. A posting the source already sent with
    this `external_id` is updated in place: the fields sent replace the
    stored ones, the others are kept, and its `job_id` stays. Returns the posting's `job_id` and what happened.

    Sending a posting again that is unchanged but whose processing failed queues the failed stages again.

    Sending the same posting again is always safe, also after a failure part-way: the content hash, which is what
    makes a resend `unchanged`, is written last, after the posting is queued.
    """
    # Fields the source left out are not touched; fields it sent as null are cleared.
    sent = posting.model_dump(mode="json", exclude_unset=True)
    if sent.get("language") is None:
        sent.pop("language", None)

    try:
        before, after = await _upsert(db, settings, source, sent)
    except DuplicateKeyError:
        # Another request created the same posting at the same moment: this one is now an update.
        before, after = await _upsert(db, settings, source, sent)

    job_id, new_hash = after["job_id"], content_hash(after)
    # A posting that is already past its closing date is closed now, before it is classified, not at the next expiry
    # pass (which could be an hour away): its documents are written as not active.
    if not is_live(after.get("closing_date")):
        await close_postings(db, [job_id], "expired")
    # A closed posting the source sends again, still open by its closing date, is live again.
    reopened = before is not None and before.get("status") == "closed" and is_live(after.get("closing_date"))
    if reopened:
        await reopen_posting(db, job_id)
    if before is not None and before.get("content_hash") == new_hash:
        # Nothing changed, but a posting whose processing ran out of attempts is queued again: a resend is how a source
        # (or we, once the cause is fixed) gets it going.
        requeued = before.get("stage") == "failed" and await requeue_failed(
            db, job_id, max_attempts=settings.max_attempts
        )
        return job_id, "updated" if reopened or requeued else "unchanged"

    await enqueue(db, job_id, "classify", max_attempts=settings.max_attempts)
    await db[SCRAPED_JOBS].update_one(
        {"job_id": job_id}, {"$set": {"content_hash": new_hash, "updated_at": datetime.now(UTC)}}
    )
    return job_id, "created" if before is None else "updated"


async def _upsert(
    db: AsyncDatabase, settings: Settings, source: str, sent: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Write what was sent. Returns the posting as it was before (`None` when it is new) and as it is now."""
    now = datetime.now(UTC)
    # Everything a reader relies on is there from the first write, so a posting is never seen half-stored.
    on_insert: dict[str, Any] = {
        "job_id": str(uuid.uuid4()),
        "source_platform": source,
        "status": "active",
        "stage": "queued",
        "created_at": now,
        "updated_at": now,
    }
    if "language" not in sent:
        on_insert["language"] = settings.language_for(sent["country"])

    before = await db[SCRAPED_JOBS].find_one_and_update(
        {"source_platform": source, "external_id": sent["external_id"]},
        {"$set": {**sent, "last_received_at": now}, "$setOnInsert": on_insert},
        upsert=True,
        return_document=ReturnDocument.BEFORE,
    )
    return before, {**(before or on_insert), **sent}
