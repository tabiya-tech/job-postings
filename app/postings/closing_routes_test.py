# pylint: disable=missing-function-docstring
from app.db import RANKED_JOBS, SCRAPED_JOBS
from app.postings.test_support import ADMIN_KEY, OTHER_KENYA_KEY, a_posting, find, get, post, write


def _close(client, body, key="kenya-key"):
    return client.post("/job-postings/close", json=body, headers={"x-api-key": key})


def _status(client, job_id, key="kenya-key"):
    return get(client, f"/job-postings/{job_id}", key).json()


def test_a_source_closes_its_posting_by_external_id_and_by_id(client):
    # GIVEN two postings
    first, second = a_posting(), a_posting()
    ids = [r["id"] for r in post(client, [first, second]).json()["results"]]

    # WHEN one is closed by external ID and the other by ID
    by_external = _close(client, {"external_ids": [first["external_id"]]})
    by_id = _close(client, {"ids": [ids[1]]})

    # THEN both are closed, with the source as the reason
    assert by_external.json()["results"] == [
        {"index": 0, "id": ids[0], "external_id": first["external_id"], "status": "closed"}
    ]
    assert by_id.json()["results"][0]["status"] == "closed"
    detail = _status(client, ids[0])
    assert (detail["status"], detail["closed_reason"]) == ("closed", "closed_by_source")
    assert detail["closed_at"] is not None


def test_closing_twice_and_closing_what_does_not_exist(client):
    posting = a_posting()
    post(client, [posting])

    _close(client, {"external_ids": [posting["external_id"]]})
    results = _close(client, {"external_ids": [posting["external_id"], "nope"]}).json()["results"]

    assert [r["status"] for r in results] == ["already_closed", "not_found"]


def test_a_source_cannot_close_another_sources_posting(client):
    # GIVEN a posting of source A
    posting = a_posting()
    job_id = post(client, [posting]).json()["results"][0]["id"]

    # WHEN source B tries to close it
    results = _close(client, {"ids": [job_id]}, key=OTHER_KENYA_KEY).json()["results"]

    # THEN it is `not_found` and still live
    assert results[0]["status"] == "not_found"
    assert _status(client, job_id)["status"] == "active"


def test_admin_keys_cannot_close_and_bad_bodies_are_rejected(client):
    assert _close(client, {"ids": ["x"]}, key=ADMIN_KEY).status_code == 403
    assert _close(client, {"ids": ["x"]}, key="wrong").status_code == 401
    assert _close(client, {}).status_code == 422
    assert _close(client, {"ids": ["x"], "external_ids": ["y"]}).status_code == 422
    assert _close(client, {"ids": []}).status_code == 422


def test_sending_a_closed_posting_again_with_no_or_a_future_closing_date_reopens_it(client):
    # GIVEN a closed posting
    posting = a_posting()
    job_id = post(client, [posting]).json()["results"][0]["id"]
    _close(client, {"ids": [job_id]})
    write(client, "ranked_jobs", {"job_id": job_id}, {"$set": {"is_active": False}}, upsert=True)

    # WHEN the source sends it again, unchanged
    result = post(client, [posting]).json()["results"][0]

    # THEN it is live again, and so is its ranked document
    assert result["status"] == "updated"
    detail = _status(client, job_id)
    assert detail["status"] == "active" and detail["closed_at"] is None
    assert find(client, RANKED_JOBS, {"job_id": job_id})[0]["is_active"] is True


def test_sending_a_posting_whose_closing_date_has_passed_does_not_reopen_it(client):
    posting = a_posting(closing_date="2020-01-01")
    job_id = post(client, [posting]).json()["results"][0]["id"]
    _close(client, {"ids": [job_id]})

    result = post(client, [posting]).json()["results"][0]

    assert result["status"] == "unchanged"
    assert find(client, SCRAPED_JOBS, {"job_id": job_id})[0]["status"] == "closed"


def test_a_posting_that_arrives_after_its_closing_date_is_stored_closed(client):
    # WHEN a posting arrives with a closing date in the past
    job_id = post(client, [a_posting(closing_date="2020-01-01")]).json()["results"][0]["id"]

    # THEN it is closed as expired at once, not at the next expiry pass
    detail = _status(client, job_id)
    assert (detail["status"], detail["closed_reason"]) == ("closed", "expired")


def test_an_update_that_moves_the_closing_date_into_the_past_closes_the_posting(client):
    # GIVEN a live posting
    posting = a_posting(closing_date="2099-01-01")
    job_id = post(client, [posting]).json()["results"][0]["id"]
    assert _status(client, job_id)["status"] == "active"

    # WHEN the source sends it with a closing date that has passed
    result = post(client, [{**posting, "closing_date": "2020-01-01"}]).json()["results"][0]

    # THEN it is updated, closed as expired, and its stage documents are not active
    assert result["status"] == "updated"
    assert _status(client, job_id)["closed_reason"] == "expired"
