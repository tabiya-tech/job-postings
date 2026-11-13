import logging
from datetime import datetime
from typing import Annotated, Any, Literal, get_args

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BeforeValidator, ValidationError
from pymongo.errors import PyMongoError

from app.api_keys import api_key_header, get_api_key, get_posting_api_key
from app.config import ApiKey, to_slug
from app.postings.closing import close_for_source
from app.postings.models import (
    CloseRequest,
    CloseResponse,
    CloseResult,
    JobPosting,
    PostingDetail,
    PostingList,
    PostingResult,
    PostingsResponse,
    PostingStage,
    describe_validation_error,
)
from app.postings.reading import Include, InvalidCursor, PostingFilters, get_posting, list_postings
from app.postings.service import save_posting
from app.rate_limit import limit_postings, limit_requests

logger = logging.getLogger(__name__)

router = APIRouter(tags=["job postings"], dependencies=[Depends(limit_requests)])

_NOT_FOUND = "No such posting for this API key."


@router.get(
    "/job-postings",
    response_model=PostingList,
    summary="List job postings",
    responses={401: {"description": "No valid `x-api-key` header."}, 400: {"description": "Invalid `cursor`."}},
)
async def get_job_postings(  # pylint: disable=too-many-arguments
    request: Request,
    *,
    api_key: Annotated[ApiKey, Depends(get_api_key)],
    country: Annotated[str | None, BeforeValidator(to_slug)] = None,
    status_: Annotated[Literal["active", "closed"] | None, Query(alias="status")] = None,
    stage: PostingStage | None = None,
    external_id: str | None = None,
    updated_since: Annotated[
        datetime | None, Query(description="Only postings that changed at or after this time (ISO 8601).")
    ] = None,
    source: Annotated[str | None, Query(description="Admin keys only: one source's postings.")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query(description="The `next` of the previous page.")] = None,
) -> PostingList:
    """The caller's postings, newest first, a page at a time. An admin key sees every source's."""
    filters = PostingFilters(
        country=country,
        status=status_,
        stage=stage,
        external_id=external_id,
        updated_since=updated_since,
        source=source,
    )
    try:
        return await list_postings(request.app.state.db, api_key, filters, limit=limit, cursor=cursor)
    except InvalidCursor as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error


@router.get(
    "/job-postings/{job_id}",
    response_model=PostingDetail,
    summary="Get one job posting",
    responses={401: {"description": "No valid `x-api-key` header."}, 404: {"description": _NOT_FOUND}},
)
async def get_job_posting(
    request: Request,
    job_id: str,
    api_key: Annotated[ApiKey, Depends(get_api_key)],
    include: Annotated[
        str | None,
        Query(description="Comma-separated stage results to add: `classification`, `ranking`."),
    ] = None,
) -> JSONResponse:
    """The posting as stored, and how far its processing got (`processing`, with each stage's last error)."""
    wanted = frozenset(part.strip() for part in (include or "").split(",") if part.strip())
    unknown = wanted - set(get_args(Include))
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"include: unknown {', '.join(sorted(unknown))} (allowed: classification, ranking)",
        )

    posting = await get_posting(request.app.state.db, api_key, job_id, wanted)
    if posting is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    # Stage results appear only when asked for (as null when that stage has not run yet).
    not_asked = {"classification", "ranking"} - wanted
    return JSONResponse(posting.model_dump(mode="json", exclude=not_asked))


@router.post(
    "/job-postings",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=PostingsResponse,
    response_model_exclude_none=True,
    summary="Send job postings",
    responses={
        401: {"description": "No valid `x-api-key` header."},
        403: {"description": "The key is an admin key, which cannot post."},
        422: {"description": "The body is not a non-empty array within the size limit."},
        429: {"description": "Too many requests or postings this minute for this key; see `Retry-After`."},
    },
    # The body is validated one posting at a time (so one bad posting does not reject the others); this documents
    # its shape.
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": {"type": "array", "items": JobPosting.model_json_schema()}}
            },
        }
    },
)
async def post_job_postings(
    request: Request,
    body: Annotated[list[Any], Body()],
    api_key: Annotated[ApiKey, Depends(get_posting_api_key)],
    key: Annotated[str, Depends(api_key_header)],
) -> PostingsResponse:
    """Store job postings and queue them for classification and ranking.

    Send one or more postings as an array. Each one gets its own result: `created`, `updated` (a posting sent before
    with the same `external_id` changed, was closed and is open again, or failed processing and is queued again),
    `unchanged`, `rejected` with the reason, or `failed` when the service could not store it. One posting's result
    does not stop the others.

    Sending a posting again is always safe, so after a `failed` result, an error or a timeout, send it again.
    """
    settings = request.app.state.settings
    if not body or len(body) > settings.max_postings_per_request:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Send between 1 and {settings.max_postings_per_request} postings.",
        )
    await limit_postings(request, key, api_key, len(body))

    results: list[PostingResult] = []
    external_ids_seen: set[str] = set()
    for index, item in enumerate(body):
        try:
            posting = JobPosting.model_validate(item)
        except ValidationError as error:
            results.append(PostingResult(index=index, status="rejected", error=describe_validation_error(error)))
            continue

        rejection = _rejection(posting, api_key, external_ids_seen)
        if rejection:
            results.append(
                PostingResult(index=index, external_id=posting.external_id, status="rejected", error=rejection)
            )
            continue
        external_ids_seen.add(posting.external_id)

        try:
            job_id, saved = await save_posting(request.app.state.db, settings, api_key.source, posting)
        except PyMongoError:
            # The database's message is for our logs, not for the source.
            logger.exception("Could not store posting %r of source %r", posting.external_id, api_key.source)
            results.append(
                PostingResult(
                    index=index,
                    external_id=posting.external_id,
                    status="failed",
                    error="The posting could not be stored. Send it again.",
                )
            )
            continue
        results.append(PostingResult(index=index, id=job_id, external_id=posting.external_id, status=saved))

    return PostingsResponse(results=results)


@router.post(
    "/job-postings/close",
    response_model=CloseResponse,
    response_model_exclude_none=True,
    summary="Close job postings",
    responses={
        401: {"description": "No valid `x-api-key` header."},
        403: {"description": "The key is an admin key, which cannot close postings."},
        422: {"description": "Neither or both of `external_ids` and `ids`, or too many."},
        429: {"description": "Too many requests this minute for this key; see `Retry-After`."},
    },
)
async def close_job_postings(
    request: Request,
    body: CloseRequest,
    api_key: Annotated[ApiKey, Depends(get_posting_api_key)],
) -> CloseResponse:
    """Close the postings of the key's source that are no longer open.

    A closed posting stays stored but is no longer shown to jobseekers. Per posting the result is `closed`,
    `already_closed` or `not_found` (unknown, or another source's). Sending the posting again with a `closing_date`
    in the future, or none, opens it again.
    """
    field, values = ("external_ids", body.external_ids) if body.external_ids else ("ids", body.ids)
    limit = request.app.state.settings.max_postings_per_request
    if len(values) > limit:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"Send between 1 and {limit} postings."
        )
    outcomes = await close_for_source(
        request.app.state.db, api_key.source, "external_id" if field == "external_ids" else "job_id", values
    )
    return CloseResponse(
        results=[
            CloseResult(index=index, id=job_id, external_id=external_id, status=outcome)
            for index, (job_id, external_id, outcome) in enumerate(outcomes)
        ]
    )


def _rejection(posting: JobPosting, api_key: ApiKey, external_ids_seen: set[str]) -> str | None:
    if posting.country not in api_key.countries:
        allowed = ", ".join(api_key.countries) or "none"
        return f"country: this API key may not post to {posting.country!r} (allowed: {allowed})"
    if posting.external_id in external_ids_seen:
        return "external_id: sent more than once in this request"
    return None
