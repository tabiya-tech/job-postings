import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pymongo import DESCENDING
from pymongo.asynchronous.database import AsyncDatabase

from app.config import ApiKey
from app.db import CLASSIFIED_JOBS, RANKED_JOBS, SCRAPED_JOBS, TASKS
from app.postings.models import PostingDetail, PostingList, PostingStage, PostingSummary

Include = Literal["classification", "ranking"]


class InvalidCursor(ValueError):
    """The `cursor` was not one this API handed out."""


@dataclass(frozen=True)
class PostingFilters:
    """What `GET /job-postings` narrows the list by. `None` means no filter."""

    country: str | None = None
    status: Literal["active", "closed"] | None = None
    stage: PostingStage | None = None
    external_id: str | None = None
    updated_since: datetime | None = None
    # Only honoured for admin keys; any other key only ever sees its own source.
    source: str | None = None


def _scope(api_key: ApiKey, source: str | None = None) -> dict[str, Any]:
    """Postings this key may read: its own source's, or any (optionally one) source's for an admin key."""
    if not api_key.admin:
        return {"source_platform": api_key.source}
    return {"source_platform": source} if source else {}


def encode_cursor(created_at: datetime, job_id: str) -> str:
    """Where the next page starts: after this posting, in newest-first order."""
    raw = json.dumps({"created_at": created_at.isoformat(), "job_id": job_id}).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
        return datetime.fromisoformat(data["created_at"]), str(data["job_id"])
    except (binascii.Error, ValueError, KeyError, TypeError, UnicodeError) as error:
        raise InvalidCursor("cursor: not a cursor returned by this API") from error


async def list_postings(
    db: AsyncDatabase, api_key: ApiKey, filters: PostingFilters, *, limit: int, cursor: str | None
) -> PostingList:
    """A page of the postings the key may read, newest first.

    Paging continues from the last posting of the previous page, so postings received meanwhile (which sort first)
    never shift the pages: every posting appears exactly once.
    """
    # `scraped_jobs` may also hold documents the old batch jobs wrote, which are not postings of this service.
    query: dict[str, Any] = {"job_id": {"$exists": True}, **_scope(api_key, filters.source)}
    for field in ("country", "status", "external_id"):
        if getattr(filters, field) is not None:
            query[field] = getattr(filters, field)
    if filters.stage is not None:
        # Postings stored before stages were recorded have none; they are queued.
        query["stage"] = {"$in": ["queued", None]} if filters.stage == "queued" else filters.stage
    if filters.updated_since is not None:
        query["updated_at"] = {"$gte": filters.updated_since}
    if cursor is not None:
        created_at, job_id = _decode_cursor(cursor)
        query["$or"] = [{"created_at": {"$lt": created_at}}, {"created_at": created_at, "job_id": {"$lt": job_id}}]

    documents = (
        await db[SCRAPED_JOBS]
        .find(query)
        .sort([("created_at", DESCENDING), ("job_id", DESCENDING)])
        .limit(limit + 1)
        .to_list()
    )
    page = documents[:limit]
    has_more = len(documents) > limit
    return PostingList(
        items=[PostingSummary.model_validate(document) for document in page],
        next=encode_cursor(page[-1]["created_at"], page[-1]["job_id"]) if has_more else None,
    )


async def get_posting(
    db: AsyncDatabase, api_key: ApiKey, job_id: str, include: frozenset[Include] = frozenset()
) -> PostingDetail | None:
    """One posting the key may read, with its processing, or `None` (unknown, or another source's)."""
    document = await db[SCRAPED_JOBS].find_one({"job_id": job_id, **_scope(api_key)})
    if document is None:
        return None

    tasks = await db[TASKS].find({"job_id": job_id}).to_list()
    document["processing"] = {task["stage"]: task for task in tasks}
    if "classification" in include:
        classified = await db[CLASSIFIED_JOBS].find_one({"job_id": job_id}, {"classification": 1})
        document["classification"] = classified.get("classification") if classified else None
    if "ranking" in include:
        ranked = await db[RANKED_JOBS].find_one({"job_id": job_id}, {"_id": 0})
        document["ranking"] = ranked
    return PostingDetail.model_validate(document)
