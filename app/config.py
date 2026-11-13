import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


def to_slug(value: Any) -> Any:
    """`" South Africa "` → `"south_africa"`; anything that is not a string is left for validation to reject."""
    return "_".join(value.lower().split()) if isinstance(value, str) else value


class RateLimit(BaseModel):
    """Limits per minute for one API key. A field left out uses the service-wide default."""

    requests_per_minute: int | None = Field(None, ge=1)
    postings_per_minute: int | None = Field(None, ge=1)


class ApiKey(BaseModel):
    """What one API key may do. The key itself is never stored, only its SHA-256."""

    # The source that owns every posting sent with this key. Several keys can share a source (rotation).
    source: str = Field(min_length=1)
    # Countries this key may post to, as slugs (written as in a posting, `South Africa` is `south_africa`).
    countries: list[Annotated[str, BeforeValidator(to_slug)]] = []
    # Admin keys read every source's postings and cannot post.
    admin: bool = False
    # Overrides the service-wide rate limits for this key.
    rate_limit: RateLimit | None = None


class Settings(BaseSettings):
    """Configuration for the API and the worker, read from the environment (and `.env` when present)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    mongodb_uri: str
    mongodb_database_name: str = "job_postings"
    # How long a MongoDB call waits for a reachable server before it fails (health check, startup).
    mongodb_server_selection_timeout_ms: int = 5000

    api_host: str = "0.0.0.0"
    api_port: int = 8080

    # API keys by SHA-256 of the key, as JSON. `API_KEYS_FILE` points at a JSON file of the same shape; both are merged.
    api_keys: dict[str, ApiKey] = {}
    api_keys_file: Path | None = None

    max_postings_per_request: int = 500

    # Browser origins allowed to call the API (JSON list), e.g. a local test page. Empty: no cross-origin calls.
    cors_allow_origins: list[str] = []

    # Language of a posting that does not say, by country (JSON, e.g. {"argentina": "es"}), else the default.
    default_language: str = "en"
    country_languages: dict[str, str] = {}

    # Attempts per task before it is marked failed.
    max_attempts: int = 5

    # How long a worker loop sleeps when there is nothing to do.
    poll_interval_seconds: float = 5.0
    # Claim loops per worker process; more throughput is more loops or more worker containers.
    worker_concurrency: int = Field(4, ge=1)
    # A task claimed longer ago than this is taken to belong to a crashed worker and can be claimed again. It has to be
    # longer than the slowest stage, or a slow call is run twice.
    lock_timeout_seconds: int = Field(600, ge=1)

    # The classifier (`POST {classifier_url}/v1/classify`). The key is only sent when set.
    classifier_url: str = "http://localhost:8000"
    classifier_api_key: str | None = None
    classifier_timeout_seconds: float = 60.0

    # The reranker (`POST {reranker_url}/rank`). LLM calls are slow, hence the longer timeout.
    reranker_url: str = "http://localhost:8001"
    reranker_api_key: str | None = None
    reranker_timeout_seconds: float = 180.0

    # How often the worker closes postings whose `closing_date` has passed.
    expiry_interval_minutes: int = Field(60, ge=1)

    # Per API key and minute: requests to the job-postings endpoints, and postings sent in `POST /job-postings`.
    rate_limit_requests_per_minute: int = Field(60, ge=1)
    rate_limit_postings_per_minute: int = Field(1000, ge=1)

    log_level: str = "INFO"

    @model_validator(mode="after")
    def _load_api_keys_file(self) -> "Settings":
        if self.api_keys_file is not None:
            from_file = json.loads(self.api_keys_file.read_text(encoding="utf-8"))
            self.api_keys = {**{h: ApiKey.model_validate(k) for h, k in from_file.items()}, **self.api_keys}
        # An entry under anything else (the key itself, an upper-case hash) would never match a request.
        not_hashes = [key_hash for key_hash in self.api_keys if not _SHA256_HEX.fullmatch(key_hash)]
        if not_hashes:
            raise ValueError(
                f"API keys must be listed by their SHA-256 in lower-case hex ({len(not_hashes)} entries are not); "
                "scripts/mint_api_key.py prints the entry to add"
            )
        return self

    def language_for(self, country: str) -> str:
        """The language assumed for a posting from `country` that does not give one."""
        return self.country_languages.get(country, self.default_language)


@lru_cache
def get_settings() -> Settings:
    """The process-wide settings, read once."""
    return Settings()
