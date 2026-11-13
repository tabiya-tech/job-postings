# pylint: disable=missing-function-docstring
from datetime import UTC, datetime, timedelta

import pytest

from app.db import CLASSIFIED_JOBS, RANKED_JOBS, SCRAPED_JOBS
from app.postings.test_support import ADMIN_KEY, OTHER_KENYA_KEY, a_posting, get, post, write


def create(client, key=None, **fields):
    kwargs = {"key": key} if key else {}
    return post(client, [a_posting(**fields)], **kwargs).json()["results"][0]["id"]


def listed_ids(response):
    return [item["id"] for item in response.json()["items"]]


def test_a_stage_that_found_nothing_to_rank_says_so(client):
    # GIVEN a posting whose rank task finished with nothing to rank
    job_id = create(client)
    done = {"$set": {"status": "done", "outcome": "classified"}}
    write(client, "tasks", {"job_id": job_id, "stage": "classify"}, done)
    write(
        client,
        "tasks",
        {"job_id": job_id, "stage": "rank"},
        {"$set": {"status": "done", "attempts": 1, "outcome": "nothing_to_rank", "updated_at": datetime.now(UTC)}},
        upsert=True,
    )

    # THEN the posting's processing shows the outcome
    processing = get(client, f"/job-postings/{job_id}").json()["processing"]
    assert processing["rank"]["outcome"] == "nothing_to_rank"
    assert processing["classify"]["outcome"] == "classified"


# ── Access ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["/job-postings", "/job-postings/some-id"])
def test_reading_needs_a_valid_key(client, path):
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"x-api-key": "not-a-key"}).status_code == 401


def test_a_source_only_sees_its_own_postings(client):
    # GIVEN a posting from source A and one from source B
    mine = create(client)
    theirs = create(client, key=OTHER_KENYA_KEY)

    # THEN A lists only its own
    assert listed_ids(get(client, "/job-postings")) == [mine]

    # AND B's posting does not exist for A
    response = get(client, f"/job-postings/{theirs}")
    assert response.status_code == 404


def test_an_admin_key_sees_every_source_and_can_filter_by_source(client):
    mine = create(client)
    theirs = create(client, key=OTHER_KENYA_KEY)

    assert set(listed_ids(get(client, "/job-postings", key=ADMIN_KEY))) == {mine, theirs}
    assert listed_ids(get(client, "/job-postings", key=ADMIN_KEY, source="source-b")) == [theirs]
    assert get(client, f"/job-postings/{mine}", key=ADMIN_KEY).json()["source"] == "source-a"


def test_the_source_filter_does_not_widen_a_normal_key(client):
    create(client, key=OTHER_KENYA_KEY)

    assert listed_ids(get(client, "/job-postings", source="source-b")) == []


def test_documents_that_are_not_postings_of_this_service_are_not_listed(client):
    # GIVEN a posting, and a document an old batch job wrote for the same source (no job_id, no status)
    mine = create(client)
    write(client, SCRAPED_JOBS, {"job_fingerprint": "f-1"}, {"$set": {"source_platform": "source-a"}}, upsert=True)

    # THEN the source and the admin list only the posting
    assert listed_ids(get(client, "/job-postings")) == [mine]
    assert listed_ids(get(client, "/job-postings", key=ADMIN_KEY)) == [mine]


def test_an_unknown_posting_is_404(client):
    assert get(client, "/job-postings/no-such-id").status_code == 404


# ── One posting ─────────────────────────────────────────────────────────────


def test_a_posting_comes_back_as_stored_with_its_processing(client):
    # GIVEN a posting that was just received
    job_id = create(client, external_id="A-17", employer="Acme Ltd", closing_date="2026-12-31")

    # WHEN it is read
    response = get(client, f"/job-postings/{job_id}")

    # THEN it has what the source sent and what the service set
    assert response.status_code == 200
    posting = response.json()
    assert posting["id"] == job_id
    assert posting["source"] == "source-a"
    assert posting["external_id"] == "A-17"
    assert posting["employer"] == "Acme Ltd"
    assert posting["closing_date"] == "2026-12-31"
    assert posting["language"] == "en"
    assert posting["status"] == "active"
    assert posting["stage"] == "queued"

    # AND its classification is queued, with no attempts yet
    assert posting["processing"]["classify"]["status"] == "pending"
    assert posting["processing"]["classify"]["attempts"] == 0

    # AND internal fields stay internal, and stage results are not included unless asked for
    for absent in ("_id", "job_id", "source_platform", "content_hash", "classification", "ranking"):
        assert absent not in posting


def test_stage_results_are_null_until_the_stages_have_run(client):
    job_id = create(client)

    posting = get(client, f"/job-postings/{job_id}", include="classification,ranking").json()

    assert posting["classification"] is None
    assert posting["ranking"] is None


def test_stage_results_are_included_when_asked_for(client):
    # GIVEN a posting the stages have processed
    job_id = create(client)
    write(client, CLASSIFIED_JOBS, {"job_id": job_id}, {"$set": {"classification": {"entities": []}}}, upsert=True)
    write(client, RANKED_JOBS, {"job_id": job_id}, {"$set": {"essential_skills": ["wiring"]}}, upsert=True)

    # WHEN only the classification is asked for
    posting = get(client, f"/job-postings/{job_id}", include="classification").json()

    # THEN only it is included
    assert posting["classification"] == {"entities": []}
    assert "ranking" not in posting

    # AND WHEN the ranking is asked for, the ranked document comes back
    ranking = get(client, f"/job-postings/{job_id}", include="ranking").json()["ranking"]
    assert ranking["essential_skills"] == ["wiring"]


def test_an_unknown_include_is_422(client):
    job_id = create(client)

    assert get(client, f"/job-postings/{job_id}", include="matches").status_code == 422


# ── Stage ───────────────────────────────────────────────────────────────────


def test_a_changed_posting_goes_back_to_queued(client):
    # GIVEN a posting that went through every stage
    job_id = create(client, external_id="A-17")
    write(client, SCRAPED_JOBS, {"job_id": job_id}, {"$set": {"stage": "done"}})

    # WHEN an unchanged resend arrives, it stays done
    post(client, [a_posting(external_id="A-17")])
    assert get(client, f"/job-postings/{job_id}").json()["stage"] == "done"

    # AND WHEN a changed version arrives, it is queued again
    post(client, [a_posting(external_id="A-17", title="Senior electrician")])
    assert get(client, f"/job-postings/{job_id}").json()["stage"] == "queued"


def test_a_posting_stored_without_a_stage_reads_as_queued(client):
    job_id = create(client)
    write(client, SCRAPED_JOBS, {"job_id": job_id}, {"$unset": {"stage": ""}})

    assert get(client, f"/job-postings/{job_id}").json()["stage"] == "queued"
    assert listed_ids(get(client, "/job-postings", stage="queued")) == [job_id]


# ── List filters ────────────────────────────────────────────────────────────


def test_the_list_has_the_summary_fields(client):
    job_id = create(client, external_id="A-17")

    [item] = get(client, "/job-postings").json()["items"]

    assert set(item) == {"id", "external_id", "title", "country", "status", "stage", "created_at", "updated_at"}
    assert item["id"] == job_id


@pytest.mark.parametrize(
    "params, expected",
    [
        ({"country": "argentina"}, ["argentina"]),
        ({"country": " Argentina "}, ["argentina"]),
        ({"status": "closed"}, ["closed"]),
        ({"stage": "done"}, ["done"]),
        ({"external_id": "A-17"}, ["external"]),
    ],
)
def test_the_list_filters(client, params, expected):
    # GIVEN postings that differ in one way each (all from source B, which may post to Argentina)
    ids = {
        "argentina": create(client, key=OTHER_KENYA_KEY, country="argentina"),
        "closed": create(client, key=OTHER_KENYA_KEY),
        "done": create(client, key=OTHER_KENYA_KEY),
        "external": create(client, key=OTHER_KENYA_KEY, external_id="A-17"),
    }
    write(client, SCRAPED_JOBS, {"job_id": ids["closed"]}, {"$set": {"status": "closed"}})
    write(client, SCRAPED_JOBS, {"job_id": ids["done"]}, {"$set": {"stage": "done"}})

    response = get(client, "/job-postings", key=OTHER_KENYA_KEY, **params)

    assert listed_ids(response) == [ids[name] for name in expected]


def test_updated_since_lists_only_what_changed(client):
    # GIVEN an old posting and a recent one
    old = create(client)
    recent = create(client)
    write(client, SCRAPED_JOBS, {"job_id": old}, {"$set": {"updated_at": datetime.now(UTC) - timedelta(days=2)}})

    # WHEN asking for what changed in the last day
    since = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    response = get(client, "/job-postings", updated_since=since)

    # THEN only the recent one is listed
    assert listed_ids(response) == [recent]


# ── Paging ──────────────────────────────────────────────────────────────────


def test_pages_list_every_posting_once_newest_first_while_new_ones_arrive(client):
    # GIVEN five postings
    created = [create(client, title=f"Job {n}") for n in range(5)]

    # WHEN reading two at a time, with new postings arriving between pages
    seen, cursor = [], None
    while True:
        params = {"limit": 2} | ({"cursor": cursor} if cursor else {})
        page = get(client, "/job-postings", **params).json()
        seen += page["items"]
        cursor = page["next"]
        if cursor is None:
            break
        create(client, title="Arrived while paging")

    # THEN each of the five came exactly once, and the newcomers did not shift the pages
    assert sorted(item["id"] for item in seen) == sorted(created)
    # AND newest first (postings created in the same millisecond may come in either order)
    created_at = [item["created_at"] for item in seen]
    assert created_at == sorted(created_at, reverse=True)


def test_the_last_page_has_no_next(client):
    create(client)

    assert get(client, "/job-postings", limit=1).json()["next"] is None


def test_an_invalid_cursor_is_400(client):
    assert get(client, "/job-postings", cursor="not-a-cursor").status_code == 400


@pytest.mark.parametrize("limit", [0, 201])
def test_limit_must_be_between_1_and_200(client, limit):
    assert get(client, "/job-postings", limit=limit).status_code == 422
