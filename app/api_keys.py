import hashlib
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader

from app.config import ApiKey

api_key_header = APIKeyHeader(name="x-api-key", auto_error=False)


def hash_api_key(key: str) -> str:
    """How a key is stored in the configuration: its SHA-256, hex-encoded."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def get_api_key(request: Request, key: Annotated[str | None, Depends(api_key_header)]) -> ApiKey:
    """FastAPI dependency: the caller's API key, or `401`."""
    api_key = request.app.state.settings.api_keys.get(hash_api_key(key)) if key else None
    if api_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid x-api-key header is required.",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    return api_key


def get_posting_api_key(api_key: Annotated[ApiKey, Depends(get_api_key)]) -> ApiKey:
    """FastAPI dependency: the caller's API key when it may post, else `403` (admin keys only read)."""
    if api_key.admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin keys cannot post job postings.")
    return api_key
