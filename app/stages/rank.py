import json
from datetime import UTC, datetime
from typing import Any

import httpx
from pymongo.asynchronous.database import AsyncDatabase

from app.config import Settings
from app.db import CLASSIFIED_JOBS, RANKED_JOBS, SCRAPED_JOBS
from app.stages.http import post_json


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def build_ranked_document(posting: dict[str, Any], ranked: dict[str, Any], now: datetime) -> dict[str, Any]:
    """The `ranked_jobs` document: the reranker's `ranked_document`, with `is_active` from the posting.

    The reranker always answers `is_active: true`. The posting's status decides, so a posting closed while its task
    was waiting does not come back to life.
    """
    document = dict(ranked)
    if isinstance(document.get("processed_at"), str):
        document["processed_at"] = datetime.fromisoformat(document["processed_at"])
    return {
        **document,
        "job_id": posting["job_id"],
        "is_active": posting.get("status") == "active",
        "ranked_at": now,
    }


async def rank(db: AsyncDatabase, client: httpx.AsyncClient, settings: Settings, job_id: str) -> str | None:
    """Rank one classified posting and store the result.

    Returns `ranked`, `nothing_to_rank` (the reranker found nothing to rank: not a failure) or `None` when the
    posting or its classified document is gone. Writing is an upsert keyed by `job_id`, so running it twice leaves one
    document.
    """
    classified = await db[CLASSIFIED_JOBS].find_one({"job_id": job_id}, {"_id": 0})
    posting = await db[SCRAPED_JOBS].find_one({"job_id": job_id}, {"_id": 0, "job_id": 1, "status": 1})
    if classified is None or posting is None:
        return None

    answer = await post_json(
        client,
        "reranker",
        f"{settings.reranker_url.rstrip('/')}/rank",
        content=json.dumps(classified, default=_json_default).encode(),
        api_key=settings.reranker_api_key,
        timeout=settings.reranker_timeout_seconds,
        params={"language": classified.get("language") or settings.default_language},
    )
    ranked = answer.get("ranked_document")
    if ranked is None:
        return "nothing_to_rank"

    # The status may have changed while the reranker worked: read it again, so closing is not undone.
    posting = await db[SCRAPED_JOBS].find_one({"job_id": job_id}, {"_id": 0, "job_id": 1, "status": 1}) or posting
    document = build_ranked_document(posting, ranked, datetime.now(UTC))
    await db[RANKED_JOBS].update_one({"job_id": job_id}, {"$set": document}, upsert=True)
    return "ranked"
