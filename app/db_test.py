# pylint: disable=missing-function-docstring
import pytest
from pymongo.errors import DuplicateKeyError

from app import db as database


@pytest.fixture(name="db")
async def fixture_db(settings):
    client = database.create_client(settings)
    yield client[settings.mongodb_database_name]
    await client.drop_database(settings.mongodb_database_name)
    await client.close()


async def test_ensure_indexes_creates_every_index(db):
    # WHEN the indexes are ensured
    await database.ensure_indexes(db)

    # THEN every collection has the indexes the service relies on
    for collection, indexes in database.INDEXES.items():
        actual = {index["name"] async for index in await db[collection].list_indexes()}
        assert {index.document["name"] for index in indexes} <= actual, collection


async def test_ensure_indexes_can_run_twice(db):
    # GIVEN the indexes already exist
    await database.ensure_indexes(db)

    # WHEN they are ensured again, as on every restart
    # THEN nothing fails
    await database.ensure_indexes(db)


async def test_external_id_is_unique_per_source(db):
    # GIVEN the indexes and a posting from a source with its own ID
    await database.ensure_indexes(db)
    postings = db[database.SCRAPED_JOBS]
    await postings.insert_one({"job_id": "1", "source_platform": "source-a", "external_id": "A-17"})

    # WHEN another source uses the same ID
    # THEN it is a different posting
    await postings.insert_one({"job_id": "2", "source_platform": "source-b", "external_id": "A-17"})

    # AND WHEN the same source sends the same ID as a new posting
    # THEN it is refused
    with pytest.raises(DuplicateKeyError):
        await postings.insert_one({"job_id": "3", "source_platform": "source-a", "external_id": "A-17"})


async def test_postings_without_external_id_never_collide(db):
    # GIVEN the indexes
    await database.ensure_indexes(db)
    postings = db[database.SCRAPED_JOBS]

    # WHEN a source sends two postings without its own ID
    await postings.insert_one({"job_id": "1", "source_platform": "source-a"})
    await postings.insert_one({"job_id": "2", "source_platform": "source-a"})

    # THEN both are stored
    assert await postings.count_documents({"source_platform": "source-a"}) == 2


async def test_documents_without_job_id_do_not_collide(db):
    # GIVEN the indexes
    await database.ensure_indexes(db)
    ranked = db[database.RANKED_JOBS]

    # WHEN documents left by the old batch jobs, which have no job_id, are stored
    await ranked.insert_one({"job_fingerprint": "a"})
    await ranked.insert_one({"job_fingerprint": "b"})

    # THEN both are kept
    assert await ranked.count_documents({}) == 2


async def test_ping_is_true_when_mongodb_answers(db):
    assert await database.ping(db) is True
