# pylint: disable=missing-function-docstring
import asyncio
from datetime import date

from app.db import CLASSIFIED_JOBS, RANKED_JOBS, SCRAPED_JOBS
from app.postings.closing import close_for_source, close_postings, expire_postings, is_live, reopen_posting


async def _store(db, job_id, source="source-a", closing_date=None, status="active"):
    await db[SCRAPED_JOBS].insert_one(
        {
            "job_id": job_id,
            "external_id": f"ext-{job_id}",
            "source_platform": source,
            "status": status,
            "closing_date": closing_date,
        }
    )
    for collection in (CLASSIFIED_JOBS, RANKED_JOBS):
        await db[collection].insert_one({"job_id": job_id, "is_active": status == "active"})


async def _is_active(db, collection, job_id):
    return (await db[collection].find_one({"job_id": job_id}))["is_active"]


async def test_closing_marks_the_posting_and_hides_its_stage_documents(db):
    # GIVEN a live posting with classified and ranked documents
    await _store(db, "a")

    # WHEN it is closed
    assert await close_postings(db, ["a"], "closed_by_source") == 1

    # THEN it says when and why, and the matching engine's `is_active` is false on both documents
    posting = await db[SCRAPED_JOBS].find_one({"job_id": "a"})
    assert (posting["status"], posting["closed_reason"]) == ("closed", "closed_by_source")
    assert posting["closed_at"] is not None
    assert not await _is_active(db, CLASSIFIED_JOBS, "a")
    assert not await _is_active(db, RANKED_JOBS, "a")


async def test_an_expired_posting_closes_once_even_when_two_workers_run_the_pass(db):
    # GIVEN postings of which three are past their closing date, one is open, one has no date
    dates = {"1": "2026-11-01", "2": "2026-11-12", "3": "2026-01-01", "4": "2026-11-13", "5": None}
    for job_id, closing in dates.items():
        await _store(db, job_id, closing_date=closing)

    # WHEN two workers run the expiry pass at the same time
    counts = await asyncio.gather(
        expire_postings(db, date(2026, 11, 13)), expire_postings(db, date(2026, 11, 13))
    )

    # THEN each posting was closed exactly once, with the reason `expired`
    assert sum(counts) == 3
    closed = {p["job_id"]: p async for p in db[SCRAPED_JOBS].find({"status": "closed"})}
    assert set(closed) == {"1", "2", "3"}
    assert {p["closed_reason"] for p in closed.values()} == {"expired"}
    # AND the one closing today, and the one without a date, stay live
    assert await _is_active(db, RANKED_JOBS, "4") and await _is_active(db, RANKED_JOBS, "5")


async def test_a_pass_that_was_cut_short_is_finished_by_the_next(db):
    # GIVEN a posting whose stage documents were hidden but whose status never changed (a crash in between)
    await _store(db, "a", closing_date="2026-01-01")
    await db[RANKED_JOBS].update_one({"job_id": "a"}, {"$set": {"is_active": False}})

    # WHEN the pass runs
    assert await expire_postings(db, date(2026, 11, 13)) == 1

    # THEN the posting is closed
    assert (await db[SCRAPED_JOBS].find_one({"job_id": "a"}))["status"] == "closed"


async def test_a_source_closes_only_its_own_postings(db):
    # GIVEN postings of two sources
    await _store(db, "mine", source="source-a")
    await _store(db, "theirs", source="source-b")

    # WHEN source A tries to close both, by ID and by external ID, and one twice
    by_id = await close_for_source(db, "source-a", "job_id", ["mine", "theirs", "missing"])
    again = await close_for_source(db, "source-a", "external_id", ["ext-mine"])

    # THEN only its own is closed, and the others are `not_found`
    assert [outcome for _, _, outcome in by_id] == ["closed", "not_found", "not_found"]
    assert again == [("mine", "ext-mine", "already_closed")]
    assert (await db[SCRAPED_JOBS].find_one({"job_id": "theirs"}))["status"] == "active"


async def test_reopening_makes_the_posting_and_its_documents_live_again(db):
    await _store(db, "a")
    await close_postings(db, ["a"], "expired")

    await reopen_posting(db, "a")

    posting = await db[SCRAPED_JOBS].find_one({"job_id": "a"})
    assert posting["status"] == "active" and "closed_at" not in posting and "closed_reason" not in posting
    assert await _is_active(db, RANKED_JOBS, "a")


def test_a_posting_is_live_until_its_closing_day_is_over():
    today = date(2026, 11, 13)
    assert is_live(None, today) and is_live("2026-11-13", today) and is_live("2027-01-01", today)
    assert not is_live("2026-11-12", today)
