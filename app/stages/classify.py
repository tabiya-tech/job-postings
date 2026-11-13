import json
from datetime import UTC, datetime
from typing import Any

import httpx
from pymongo import ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase

from app.config import Settings
from app.db import CLASSIFIED_JOBS, SCRAPED_JOBS
from app.stages.http import post_json

# The keys of `extra` copied to the top level of the classified document, because the reranker and the matching engine
# read them there (sector, and Zambia's qualification levels). Not configuration: a source cannot widen it, which is
# what keeps `extra` from reaching service fields. Everything else stays in `extra`, and `translation` is never copied
# (the reranker would read it instead of the title and description). Delete this when the reranker reads these fields
# from `extra`.
PROMOTED_EXTRA_KEYS = ("sector", "zqf_min", "zqf_max", "zqf_min_label", "zqf_max_label")


def build_classified_document(posting: dict[str, Any], result: dict[str, Any], now: datetime) -> dict[str, Any]:
    """The `classified_jobs` document: what the reranker reads, in the names it reads them.

    `region` is written as `province`, `salary` as `salary_text`, and `status` as `is_active`.
    """
    extra = posting.get("extra") or {}
    return {
        # Whatever the source put in `extra` goes first, so nothing below can be replaced by it.
        **{key: extra[key] for key in PROMOTED_EXTRA_KEYS if extra.get(key) is not None},
        "title": posting.get("title"),
        "description": posting.get("description"),
        "requirements": posting.get("requirements"),
        "employer": posting.get("employer"),
        "location": posting.get("location"),
        "city": posting.get("city"),
        "province": posting.get("region"),
        "category": posting.get("category"),
        "employment_type": posting.get("employment_type"),
        "salary_text": posting.get("salary"),
        "education": posting.get("education"),
        "experience": posting.get("experience"),
        "posted_date": posting.get("posted_date"),
        "closing_date": posting.get("closing_date"),
        "application_url": posting.get("application_url"),
        "country": posting.get("country"),
        "language": posting.get("language"),
        # Set by the service and the classifier, never by the source.
        "job_id": posting["job_id"],
        "source_platform": posting["source_platform"],
        "is_active": posting.get("status") == "active",
        "classification": result.get("classification"),
        "metadata": result.get("metadata"),
        "classified_at": now,
        "updated_at": now,
    }


async def call_classifier(client: httpx.AsyncClient, settings: Settings, posting: dict[str, Any]) -> dict[str, Any]:
    """Ask the classifier for the occupations and skills in a posting."""
    body = {
        "title": posting["title"],
        "description": posting["description"],
        "options": {"language": posting.get("language") or settings.default_language},
    }
    return await post_json(
        client,
        "classifier",
        f"{settings.classifier_url.rstrip('/')}/v1/classify",
        content=json.dumps(body).encode(),
        api_key=settings.classifier_api_key,
        timeout=settings.classifier_timeout_seconds,
    )


async def classify(db: AsyncDatabase, client: httpx.AsyncClient, settings: Settings, job_id: str) -> str | None:
    """Classify one posting and store the result. Returns `None` when there is nothing to classify.

    Writing is an upsert keyed by `job_id`, so running it twice leaves one document.
    """
    posting = await db[SCRAPED_JOBS].find_one({"job_id": job_id}, {"_id": 0})
    if posting is None:
        return None
    result = await call_classifier(client, settings, posting)
    document = build_classified_document(posting, result, datetime.now(UTC))
    await db[CLASSIFIED_JOBS].find_one_and_update(
        {"job_id": job_id}, {"$set": document}, upsert=True, return_document=ReturnDocument.AFTER
    )
    return "classified"
