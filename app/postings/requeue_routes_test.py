# pylint: disable=missing-function-docstring
from datetime import UTC, datetime

from app.db import SCRAPED_JOBS, TASKS
from app.postings.test_support import a_posting, find, get, post, write


def _fail_rank(client, job_id):
    """Put a posting in the state where classify worked and rank ran out of attempts."""
    write(client, TASKS, {"job_id": job_id, "stage": "classify"}, {"$set": {"status": "done"}})
    write(
        client,
        TASKS,
        {"job_id": job_id, "stage": "rank"},
        {
            "$set": {
                "status": "failed",
                "attempts": 5,
                "max_attempts": 5,
                "last_error": "reranker error (500)",
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            }
        },
        upsert=True,
    )
    write(client, SCRAPED_JOBS, {"job_id": job_id}, {"$set": {"stage": "failed"}})


def test_sending_a_failed_posting_again_queues_the_failed_stage_again(client):
    # GIVEN a posting whose ranking ran out of attempts
    posting = a_posting()
    job_id = post(client, [posting]).json()["results"][0]["id"]
    _fail_rank(client, job_id)
    assert get(client, f"/job-postings/{job_id}").json()["stage"] == "failed"

    # WHEN the source sends the same posting again
    result = post(client, [posting]).json()["results"][0]

    # THEN it is queued again, and says so
    assert result["status"] == "updated"
    detail = get(client, f"/job-postings/{job_id}").json()
    assert detail["stage"] == "ranking"
    assert detail["processing"]["rank"]["status"] == "pending"
    assert detail["processing"]["rank"]["attempts"] == 0
    # AND classify, which had worked, was not queued again
    assert detail["processing"]["classify"]["status"] == "done"


def test_sending_the_same_posting_again_when_nothing_failed_is_still_unchanged(client):
    posting = a_posting()
    job_id = post(client, [posting]).json()["results"][0]["id"]

    result = post(client, [posting]).json()["results"][0]

    assert result["status"] == "unchanged"
    assert find(client, TASKS, {"job_id": job_id})[0]["attempts"] == 0
