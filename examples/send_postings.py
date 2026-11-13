"""Send postings from a JSON file to the job-postings API, the way a source should.

    API_URL=http://localhost:8090 API_KEY=<key> python examples/send_postings.py postings.json

The file holds an array of postings. They are sent in batches. A request that fails (a network error, `429`, `5xx`) is
sent again after a wait, and so are the postings of a batch the service answered `failed`: nothing is stored twice,
because a posting is identified by its `external_id`. A posting that is `rejected` is not sent again: it has to be
fixed. Standard library only.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

API_URL = os.environ.get("API_URL", "http://localhost:8090")
API_KEY = os.environ["API_KEY"]
BATCH_SIZE = 200  # the service accepts up to 500 (MAX_POSTINGS_PER_REQUEST)
ATTEMPTS = 5


def post_batch(batch: list[dict]) -> list[dict]:
    """Send one batch and return the result of each posting. Retries the request when it fails."""
    for attempt in range(1, ATTEMPTS + 1):
        wait = 2.0**attempt
        request = urllib.request.Request(
            f"{API_URL}/job-postings",
            data=json.dumps(batch).encode(),
            headers={"content-type": "application/json", "x-api-key": API_KEY},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)["results"]
        except urllib.error.HTTPError as error:
            if error.code != 429 and error.code < 500:
                raise  # 401, 403, 422: sending again will not help
            wait = float(error.headers.get("retry-after", wait))
        except (urllib.error.URLError, TimeoutError):
            pass
        print(f"request failed (attempt {attempt}/{ATTEMPTS}), waiting {wait:.0f}s", file=sys.stderr)
        time.sleep(wait)
    raise RuntimeError("the API did not answer; try again later")


def send(postings: list[dict]) -> dict[str, int]:
    """Send every posting, sending the `failed` ones again, and count the outcomes."""
    counts: dict[str, int] = {}
    for start in range(0, len(postings), BATCH_SIZE):
        batch = postings[start : start + BATCH_SIZE]
        for _ in range(ATTEMPTS):
            results = post_batch(batch)
            retry = [posting for posting, result in zip(batch, results) if result["status"] == "failed"]
            for posting, result in zip(batch, results):
                if result["status"] == "rejected":
                    print(f"rejected {posting.get('external_id')!r}: {result['error']}", file=sys.stderr)
                elif result["status"] != "failed":
                    counts[result["status"]] = counts.get(result["status"], 0) + 1
            if not retry:
                break
            batch = retry
            time.sleep(2)
        else:
            counts["failed"] = counts.get("failed", 0) + len(batch)
    return counts


if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf-8") as file:
        postings_to_send = json.load(file)
    try:
        print(send(postings_to_send))
    except urllib.error.HTTPError as http_error:
        sys.exit(f"{http_error.code}: {http_error.read().decode()}")
