from typing import Any

import httpx

from app.stages.errors import StageError


def retry_after(response: httpx.Response) -> float | None:
    """The `Retry-After` of a response in seconds (the date form is not used by our services)."""
    try:
        return max(float(response.headers["retry-after"]), 0)
    except (KeyError, ValueError):
        return None


async def post_json(  # pylint: disable=too-many-arguments
    client: httpx.AsyncClient,
    name: str,
    url: str,
    *,
    content: bytes,
    api_key: str | None,
    timeout: float,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    """POST JSON to another service and return its JSON answer, or raise a `StageError` that says what to do next.

    `name` is the service's name in the error texts, which end up in `last_error`: they hold a status code, never
    what the service answered.
    """
    headers = {"content-type": "application/json"}
    if api_key:
        headers["x-api-key"] = api_key
    try:
        response = await client.post(url, content=content, headers=headers, params=params, timeout=timeout)
    except httpx.TimeoutException as error:
        raise StageError(f"{name} timed out") from error
    except httpx.HTTPError as error:
        raise StageError(f"{name} unreachable") from error

    status = response.status_code
    if status in (429, 503):
        raise StageError(f"{name} busy ({status})", busy=True, retry_after=retry_after(response))
    if status in (400, 422):
        raise StageError(f"{name} rejected the posting ({status})", retryable=False)
    if status != 200:
        raise StageError(f"{name} error ({status})")
    try:
        result = response.json()
    except ValueError as error:
        raise StageError(f"{name} answered with something that is not JSON") from error
    if not isinstance(result, dict):
        raise StageError(f"{name} answered with something unexpected")
    return result
