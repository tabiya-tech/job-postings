# pylint: disable=missing-function-docstring
import pytest
from pymongo.errors import AutoReconnect

from app.api_keys import hash_api_key
from app.config import ApiKey
from app.db import SCRAPED_JOBS, TASKS
from app.postings import service
from app.postings.models import MAX_EXTRA_LENGTH, MAX_LONG_TEXT_LENGTH, JobPosting
from app.postings.test_support import ADMIN_KEY, OTHER_KENYA_KEY, SECOND_KEY_OF_SOURCE_A, a_posting, find, get, post


def set_task_status(client, job_id, status):
    async def _set():
        await client.app.state.db[TASKS].update_one(
            {"job_id": job_id, "stage": "classify"}, {"$set": {"status": status}}
        )

    client.portal.call(_set)


# ── API keys ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("headers", [{}, {"x-api-key": "not-a-key"}])
def test_requests_without_a_valid_key_get_401(client, headers):
    response = client.post("/job-postings", json=[a_posting()], headers=headers)

    assert response.status_code == 401
    assert find(client, SCRAPED_JOBS) == []


def test_admin_keys_cannot_post(client):
    response = post(client, [a_posting()], key=ADMIN_KEY)

    assert response.status_code == 403
    assert find(client, SCRAPED_JOBS) == []


def test_a_key_cannot_post_to_a_country_it_does_not_allow(client):
    # WHEN a Kenya-only key posts to another country
    response = post(client, [a_posting(country="argentina")])

    # THEN that posting is rejected and nothing is stored
    assert response.status_code == 202
    [result] = response.json()["results"]
    assert result["status"] == "rejected"
    assert result["error"].startswith("country:")
    assert find(client, SCRAPED_JOBS) == []


def test_a_key_may_list_its_countries_as_people_write_them(client):
    # GIVEN a key whose countries were configured as "Kenya" and "South Africa"
    key = ApiKey(source="source-c", countries=["Kenya", "South Africa"])
    client.app.state.settings.api_keys[hash_api_key("source-c-key")] = key

    # WHEN it posts to both
    response = post(client, [a_posting(), a_posting(country="south africa")], key="source-c-key")

    # THEN both are accepted
    assert [result["status"] for result in response.json()["results"]] == ["created", "created"]


# ── Creating ────────────────────────────────────────────────────────────────


def test_a_new_posting_is_stored_and_queued_for_classification(client):
    # WHEN a source sends a posting
    response = post(client, [a_posting(external_id="A-17", posted_date="2026-11-01")])

    # THEN it is accepted with an ID
    assert response.status_code == 202
    [result] = response.json()["results"]
    assert result["status"] == "created"
    assert result["external_id"] == "A-17"
    job_id = result["id"]

    # AND it is stored with the source of the key, the country's language and the service's fields
    [stored] = find(client, SCRAPED_JOBS)
    assert stored["job_id"] == job_id
    assert stored["source_platform"] == "source-a"
    assert stored["language"] == "en"
    assert stored["status"] == "active"
    assert stored["posted_date"] == "2026-11-01"

    # AND a classify task is queued for it
    [task] = find(client, TASKS)
    assert task["job_id"] == job_id
    assert task["stage"] == "classify"
    assert task["status"] == "pending"
    assert task["attempts"] == 0


def test_the_language_defaults_to_the_country_language(client):
    post(client, [a_posting(country="argentina")], key=OTHER_KENYA_KEY)

    [stored] = find(client, SCRAPED_JOBS)
    assert stored["language"] == "es"


def test_country_and_language_case_and_spaces_are_normalised(client):
    # WHEN a source writes the country and language as people do
    response = post(client, [a_posting(country=" Argentina ", language="ES")], key=OTHER_KENYA_KEY)

    # THEN they are accepted and stored as slugs
    assert response.json()["results"][0]["status"] == "created"
    [stored] = find(client, SCRAPED_JOBS)
    assert (stored["country"], stored["language"]) == ("argentina", "es")


def test_a_country_name_with_spaces_becomes_a_slug():
    assert JobPosting(external_id="A-1", title="t", description="d", country="South  Africa").country == "south_africa"


def test_two_postings_with_the_same_title_and_location_stay_two(client):
    # WHEN two real jobs that read the same are sent
    response = post(client, [a_posting(location="Nairobi"), a_posting(location="Nairobi")])

    # THEN both are stored, with different IDs
    ids = [result["id"] for result in response.json()["results"]]
    assert len(set(ids)) == 2
    assert len(find(client, SCRAPED_JOBS)) == 2


def test_a_blank_optional_field_is_stored_as_null(client):
    # WHEN a source sends empty text for fields the posting does not have
    response = post(client, [a_posting(employer="", salary="   ")])

    # THEN the posting is accepted, with those fields empty
    assert response.json()["results"][0]["status"] == "created"
    [stored] = find(client, SCRAPED_JOBS)
    assert stored["employer"] is None
    assert stored["salary"] is None


# ── Sending again ───────────────────────────────────────────────────────────


def test_a_request_sent_again_does_not_duplicate_postings(client):
    # GIVEN a request whose answer the source never saw
    postings = [a_posting(), a_posting()]
    post(client, postings)

    # WHEN the source sends the same request again
    response = post(client, postings)

    # THEN nothing is stored twice
    assert [result["status"] for result in response.json()["results"]] == ["unchanged", "unchanged"]
    assert len(find(client, SCRAPED_JOBS)) == 2


def test_a_posting_that_could_not_be_stored_is_failed_and_the_others_still_go_in(client, monkeypatch):
    # GIVEN the database fails while the second posting is stored
    save_posting = service.save_posting

    async def failing_for_a_2(db, settings, source, posting):
        if posting.external_id == "A-2":
            raise AutoReconnect("connection lost")
        return await save_posting(db, settings, source, posting)

    monkeypatch.setattr("app.postings.routes.save_posting", failing_for_a_2)

    # WHEN three postings are sent
    response = post(client, [a_posting(external_id=f"A-{n}") for n in (1, 2, 3)])

    # THEN the request is answered, with the second one failed and no database detail in the error
    assert response.status_code == 202
    first, second, third = response.json()["results"]
    assert (first["status"], second["status"], third["status"]) == ("created", "failed", "created")
    assert "id" not in second
    assert "connection lost" not in second["error"]


def test_a_posting_stored_but_not_queued_is_queued_when_sent_again(client, monkeypatch):
    # GIVEN a posting that was stored, but the service failed before queueing it
    async def failing(*_args, **_kwargs):
        raise AutoReconnect("connection lost")

    with monkeypatch.context() as patch:
        patch.setattr(service, "enqueue", failing)
        [result] = post(client, [a_posting(external_id="A-17")]).json()["results"]
    assert result["status"] == "failed"
    assert find(client, TASKS) == []

    # THEN it can already be listed (it is never seen half-stored)
    [listed] = get(client, "/job-postings").json()["items"]
    assert listed["stage"] == "queued"

    # AND WHEN the source sends it again
    [result] = post(client, [a_posting(external_id="A-17")]).json()["results"]

    # THEN it is queued, as the same posting
    assert result["id"] == listed["id"]
    assert result["status"] == "updated"
    [task] = find(client, TASKS)
    assert task["status"] == "pending"


# ── Updating ────────────────────────────────────────────────────────────────


def test_a_title_edit_with_the_same_external_id_keeps_the_id_and_queues_classification_again(client):
    # GIVEN a posting that was already classified
    job_id = post(client, [a_posting(external_id="A-17")]).json()["results"][0]["id"]
    set_task_status(client, job_id, "done")

    # WHEN the source sends it again with another title
    response = post(client, [a_posting(external_id="A-17", title="Senior electrician")])

    # THEN it is the same posting, updated
    [result] = response.json()["results"]
    assert result == {"index": 0, "id": job_id, "external_id": "A-17", "status": "updated"}
    [stored] = find(client, SCRAPED_JOBS)
    assert stored["title"] == "Senior electrician"

    # AND it is queued for classification again
    [task] = find(client, TASKS)
    assert task["status"] == "pending"


def test_an_unchanged_resend_is_not_queued_again(client):
    # GIVEN a posting that was already classified
    job_id = post(client, [a_posting(external_id="A-17")]).json()["results"][0]["id"]
    set_task_status(client, job_id, "done")

    # WHEN the source sends exactly the same posting again
    response = post(client, [a_posting(external_id="A-17")])

    # THEN nothing changes and nothing is queued
    assert response.json()["results"][0] == {"index": 0, "id": job_id, "external_id": "A-17", "status": "unchanged"}
    [task] = find(client, TASKS)
    assert task["status"] == "done"


def test_an_update_keeps_the_fields_not_sent_and_clears_the_ones_sent_as_null(client):
    # GIVEN a posting with an employer and a salary
    post(client, [a_posting(external_id="A-17", employer="Acme Ltd", salary="KES 40,000")])

    # WHEN the source sends it again without the employer and with the salary as null
    post(client, [a_posting(external_id="A-17", salary=None)])

    # THEN the employer is kept and the salary is cleared
    [stored] = find(client, SCRAPED_JOBS)
    assert stored["employer"] == "Acme Ltd"
    assert stored["salary"] is None


def test_another_key_of_the_same_source_updates_the_same_posting(client):
    job_id = post(client, [a_posting(external_id="A-17")]).json()["results"][0]["id"]

    response = post(client, [a_posting(external_id="A-17", title="Senior electrician")], key=SECOND_KEY_OF_SOURCE_A)

    assert response.json()["results"][0]["id"] == job_id
    assert response.json()["results"][0]["status"] == "updated"


def test_a_source_cannot_update_another_sources_posting(client):
    # GIVEN a posting from source A
    job_id = post(client, [a_posting(external_id="A-17")]).json()["results"][0]["id"]

    # WHEN source B sends a posting with the same external_id
    response = post(client, [a_posting(external_id="A-17", title="Something else")], key=OTHER_KENYA_KEY)

    # THEN it is a separate posting of source B, and A's posting is untouched
    [result] = response.json()["results"]
    assert result["status"] == "created"
    assert result["id"] != job_id
    [original] = find(client, SCRAPED_JOBS, {"job_id": job_id})
    assert original["title"] == "Electrician"


def test_the_same_external_id_twice_in_one_request_rejects_the_second(client):
    response = post(client, [a_posting(external_id="A-17"), a_posting(external_id="A-17", title="Other")])

    first, second = response.json()["results"]
    assert first["status"] == "created"
    assert second["status"] == "rejected"
    assert second["error"].startswith("external_id:")
    [stored] = find(client, SCRAPED_JOBS)
    assert stored["title"] == "Electrician"


# ── Validation ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "posting, error_field",
    [
        ({"external_id": "A-17", "title": "Electrician", "country": "kenya"}, "description"),
        ({"title": "Electrician", "description": "Wiring.", "country": "kenya"}, "external_id"),
        (a_posting(description="x" * (MAX_LONG_TEXT_LENGTH + 1)), "description"),
        (a_posting(extra={"notes": "x" * MAX_EXTRA_LENGTH}), "extra"),
        (a_posting(closing_date="next week"), "closing_date"),
        (a_posting(employment_type="permanent"), "employment_type"),
        (a_posting(sector="Construction"), "sector"),
        (a_posting(title="   "), "title"),
        (a_posting(application_url="not a url"), "application_url"),
        (a_posting(country="Kenya!"), "country"),
        (a_posting(language="spanish"), "language"),
        ("not an object", "posting"),
    ],
    ids=[
        "missing-required",
        "missing-external-id",
        "description-too-long",
        "extra-too-large",
        "bad-date",
        "unknown-employment-type",
        "unknown-field",
        "blank-title",
        "bad-url",
        "country-not-a-slug",
        "language-not-a-code",
        "not-an-object",
    ],
)
def test_an_invalid_posting_is_rejected_with_the_reason(client, posting, error_field):
    response = post(client, [posting])

    assert response.status_code == 202
    [result] = response.json()["results"]
    assert result["status"] == "rejected"
    assert result["error"].startswith(f"{error_field}:")
    assert "id" not in result


def test_one_invalid_posting_does_not_reject_the_others(client):
    response = post(client, [a_posting(), {"title": "No description", "country": "kenya"}, a_posting()])

    assert [result["status"] for result in response.json()["results"]] == ["created", "rejected", "created"]
    assert [result["index"] for result in response.json()["results"]] == [0, 1, 2]
    assert len(find(client, SCRAPED_JOBS)) == 2


def test_source_specific_fields_go_in_extra(client):
    post(client, [a_posting(extra={"sector": "Construction", "isco_major_group": "7"})])

    [stored] = find(client, SCRAPED_JOBS)
    assert stored["extra"] == {"sector": "Construction", "isco_major_group": "7"}


@pytest.mark.parametrize("body", [a_posting(), [], "postings"], ids=["object-not-array", "empty", "string"])
def test_a_body_that_is_not_a_non_empty_array_is_422(client, body):
    assert post(client, body).status_code == 422


def test_more_postings_than_the_limit_is_422(client):
    client.app.state.settings.max_postings_per_request = 2

    response = post(client, [a_posting(), a_posting(), a_posting()])

    assert response.status_code == 422
    assert find(client, SCRAPED_JOBS) == []
