import json
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    HttpUrl,
    ValidationError,
    model_validator,
)

from app.config import to_slug
from app.tasks.queue import PostingStage, Stage


# Size limits, in characters. A posting has to fit in one MongoDB document (16 MB) with room to spare, and a
# request of many postings in memory.
MAX_TEXT_LENGTH = 10_000
MAX_LONG_TEXT_LENGTH = 200_000
MAX_EXTRA_LENGTH = 100_000


def _within_extra_limit(extra: dict[str, Any] | None) -> dict[str, Any] | None:
    if extra is not None and len(json.dumps(extra, ensure_ascii=False)) > MAX_EXTRA_LENGTH:
        raise ValueError(f"must be at most {MAX_EXTRA_LENGTH} characters as JSON")
    return extra


Text = Annotated[str, Field(min_length=1, max_length=MAX_TEXT_LENGTH)]
# The fields that hold the body of a posting.
LongText = Annotated[str, Field(min_length=1, max_length=MAX_LONG_TEXT_LENGTH)]
# As in Schema.org's JobPosting `employmentType`, which many job boards already publish.
EmploymentType = Literal["full_time", "part_time", "contract", "temporary", "internship", "volunteer", "other"]


class JobPosting(BaseModel):
    """A job posting as a source sends it: only what the posting says.

    Identity (`job_id`), ownership (the source, taken from the API key) and lifecycle (`status`) are set by the
    service. Anything else a source wants to keep goes in `extra`.

    `external_id` is what makes sending a posting again safe: the same ID from the same source is always the same
    posting, so a request that timed out can simply be repeated.
    """

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        json_schema_extra={
            "examples": [
                {
                    "external_id": "A-17",
                    "title": "Electrician",
                    "description": "Install and maintain wiring in commercial buildings.",
                    "country": "kenya",
                    "employer": "Acme Ltd",
                    "city": "Nairobi",
                    "employment_type": "full_time",
                    "closing_date": "2026-12-01",
                    "application_url": "https://jobs.example.org/a-17",
                }
            ]
        },
    )

    external_id: Annotated[str, Field(min_length=1, max_length=256)] = Field(
        description="The source's own ID for the posting, e.g. the job board's listing ID or URL. Sending the same "
        "ID again updates the posting; it never creates a second one."
    )
    title: Text
    description: LongText
    country: Annotated[str, BeforeValidator(to_slug), Field(pattern=r"^[a-z][a-z_]*$", max_length=64)] = Field(
        description="Country slug, e.g. `kenya`. Case and spaces are normalised: `South Africa` is `south_africa`."
    )

    language: Annotated[str | None, BeforeValidator(to_slug), Field(pattern=r"^[a-z]{2}$")] = Field(
        None, description="ISO 639-1 code, e.g. `es`. Defaults to the country's language."
    )
    requirements: LongText | None = None
    education: Text | None = None
    experience: Text | None = None
    employer: Text | None = None
    location: Text | None = Field(None, description='As published, e.g. "Westlands, Nairobi".')
    city: Text | None = None
    region: Text | None = Field(None, description="Province, county or state.")
    category: Text | None = Field(None, description="The source's own category.")
    employment_type: EmploymentType | None = None
    salary: Text | None = Field(None, description="As published.")
    posted_date: date | None = None
    closing_date: date | None = None
    application_url: HttpUrl | None = None
    extra: Annotated[dict[str, Any] | None, AfterValidator(_within_extra_limit)] = Field(
        None, description="Anything source-specific. Stored and passed on as is."
    )

    @model_validator(mode="before")
    @classmethod
    def _blank_optional_fields_are_null(cls, data: Any) -> Any:
        """Sources often send `""` for a field the posting does not have; that means the same as null."""
        if not isinstance(data, dict):
            return data
        # (pylint does not see that pydantic's `model_fields` is a dict on the class.)
        optional = {
            name for name, field in cls.model_fields.items() if not field.is_required()  # pylint: disable=no-member
        }
        return {
            name: None if name in optional and isinstance(value, str) and not value.strip() else value
            for name, value in data.items()
        }


def describe_validation_error(error: ValidationError) -> str:
    """One line per problem, e.g. `description: Field required`."""
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or 'posting'}: {item['msg']}" for item in error.errors()
    )


class PostingResult(BaseModel):
    """What happened to one posting of a `POST /job-postings` request."""

    index: int = Field(description="Position of the posting in the request array.")
    id: str | None = Field(None, description="The posting's `job_id`. Absent when the posting was rejected.")
    external_id: str | None = None
    status: Literal["created", "updated", "unchanged", "rejected", "failed"] = Field(
        description="`rejected`: the posting is invalid, fix it. `failed`: the service could not store it, send it "
        "again."
    )
    error: str | None = Field(None, description="Why the posting was rejected or could not be stored.")


class PostingsResponse(BaseModel):
    """`POST /job-postings`: one result per posting, in the order sent."""

    results: list[PostingResult]


# ── Reading postings ──────────────────────────────────────────────────────────

_STORED = ConfigDict(extra="ignore", json_schema_extra=None)


class _StoredFields(BaseModel):
    """Fields the service sets on every stored posting (read from `scraped_jobs`)."""

    model_config = _STORED

    id: str = Field(validation_alias="job_id", description="The posting's ID, given when it was first received.")
    status: Literal["active", "closed"] = Field(description="`closed` once expired or closed by its source.")
    stage: PostingStage = Field("queued", description="How far processing got.")
    created_at: datetime
    updated_at: datetime = Field(description="When the posting last changed (an unchanged resend does not count).")


class PostingSummary(_StoredFields):
    """One posting in `GET /job-postings`."""

    # Postings stored before `external_id` was required have none.
    external_id: str | None = None
    title: str
    country: str


class PostingList(BaseModel):
    """`GET /job-postings`: a page of postings, newest first."""

    items: list[PostingSummary]
    next: str | None = Field(description="Pass as `cursor` to get the next page; null on the last page.")


class StageProgress(BaseModel):
    """One stage of a posting's processing."""

    model_config = ConfigDict(extra="ignore")

    status: Literal["pending", "processing", "done", "failed"]
    attempts: int
    last_error: str | None = None
    outcome: str | None = Field(
        None, description="How a finished stage ended, when it matters: `nothing_to_rank` (not a failure)."
    )
    updated_at: datetime


class PostingDetail(_StoredFields, JobPosting):
    """`GET /job-postings/{id}`: the posting as stored, and how its processing went."""

    model_config = _STORED

    source: str = Field(validation_alias="source_platform", description="The source that sent the posting.")
    # Postings stored before `external_id` was required have none.
    external_id: str | None = None
    closed_at: datetime | None = None
    closed_reason: Literal["expired", "closed_by_source"] | None = None
    last_received_at: datetime | None = Field(None, description="When the source last sent it, changed or not.")
    processing: dict[Stage, StageProgress] = Field(
        default_factory=dict, description="Each stage that has been queued for this posting."
    )
    classification: dict[str, Any] | None = Field(
        None, description="With `include=classification`: the classifier's output, once classified."
    )
    ranking: dict[str, Any] | None = Field(
        None, description="With `include=ranking`: the ranked document, once ranked."
    )


# ── Closing postings ──────────────────────────────────────────────────────────


class CloseRequest(BaseModel):
    """`POST /job-postings/close`: the postings to close, by `external_ids` or by `ids`, not both."""

    model_config = ConfigDict(extra="forbid")

    external_ids: list[str] | None = Field(None, min_length=1, description="The source's own IDs.")
    ids: list[str] | None = Field(None, min_length=1, description="IDs the service gave (`job_id`).")

    @model_validator(mode="after")
    def _exactly_one(self) -> "CloseRequest":
        if (self.external_ids is None) == (self.ids is None):
            raise ValueError("send either `external_ids` or `ids`")
        return self


class CloseResult(BaseModel):
    """What happened to one posting of a close request."""

    index: int
    id: str | None = None
    external_id: str | None = None
    status: Literal["closed", "already_closed", "not_found"]


class CloseResponse(BaseModel):
    """`POST /job-postings/close`: one result per posting, in the order sent."""

    results: list[CloseResult]
