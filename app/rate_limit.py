import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.api_keys import api_key_header, get_api_key, hash_api_key
from app.config import ApiKey
from app.db import RATE_LIMITS

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 60


def current_window() -> int:
    """The minute we are in, as a number. (Tests replace this to stay clear of minute boundaries.)"""
    return int(time.time() // WINDOW_SECONDS)


async def _count(request: Request, key: str, counter: str, amount: int) -> tuple[int, int]:
    """Add `amount` to the key's `counter` for this minute and return the total and the seconds left in the minute.

    One document per key and minute, incremented atomically, so it holds across several API containers. A TTL index
    removes old ones.
    """
    window = current_window()
    expires_at = datetime.fromtimestamp((window + 1) * WINDOW_SECONDS, UTC) + timedelta(seconds=WINDOW_SECONDS)
    # Only a short prefix of the key's hash is kept.
    query = {"_id": f"{hash_api_key(key)[:16]}:{window}"}
    update = {"$inc": {counter: amount}, "$setOnInsert": {"expires_at": expires_at}}
    collection = request.app.state.db[RATE_LIMITS]
    try:
        document = await collection.find_one_and_update(
            query, update, upsert=True, return_document=ReturnDocument.AFTER
        )
    except DuplicateKeyError:
        # Another request created this minute's document at the same moment: increment it.
        document = await collection.find_one_and_update(query, update, return_document=ReturnDocument.AFTER)
    retry_after = max(1, (window + 1) * WINDOW_SECONDS - int(time.time()))
    return document[counter], retry_after


def _too_many(what: str, limit: int, retry_after: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=f"Rate limit reached: at most {limit} {what} per minute for this API key.",
        headers={"Retry-After": str(retry_after)},
    )


async def limit_requests(
    request: Request,
    key: Annotated[str | None, Depends(api_key_header)],
    api_key: Annotated[ApiKey, Depends(get_api_key)],
) -> None:
    """FastAPI dependency: `429` when the key sent too many requests this minute. Unknown keys get `401` first."""
    limit = (
        api_key.rate_limit.requests_per_minute if api_key.rate_limit else None
    ) or request.app.state.settings.rate_limit_requests_per_minute
    total, retry_after = await _count(request, key, "requests", 1)
    if total > limit:
        raise _too_many("requests", limit, retry_after)


async def limit_postings(request: Request, key: str, api_key: ApiKey, amount: int) -> None:
    """`429` when sending `amount` more postings would pass the key's limit this minute."""
    limit = (
        api_key.rate_limit.postings_per_minute if api_key.rate_limit else None
    ) or request.app.state.settings.rate_limit_postings_per_minute
    total, retry_after = await _count(request, key, "postings", amount)
    if total > limit:
        raise _too_many("postings", limit, retry_after)
