"""Keys and helpers shared by the job-postings endpoint tests (the `client` fixture is in conftest.py)."""

import uuid

KENYA_KEY = "kenya-key"
OTHER_KENYA_KEY = "other-kenya-key"
SECOND_KEY_OF_SOURCE_A = "source-a-second-key"
ADMIN_KEY = "admin-key"


def a_posting(**fields):
    """A valid posting for Kenya, with `fields` added or replaced. Each one is a different posting unless told."""
    return {
        "external_id": f"ID-{uuid.uuid4().hex[:8]}",
        "title": "Electrician",
        "description": "Install and maintain wiring.",
        "country": "kenya",
        **fields,
    }


def post(client, postings, key=KENYA_KEY):
    """`POST /job-postings` with `key`."""
    return client.post("/job-postings", json=postings, headers={"x-api-key": key})


def get(client, path, key=KENYA_KEY, **params):
    """A `GET` with `key`."""
    return client.get(path, params=params, headers={"x-api-key": key})


def find(client, collection, query=None):
    """The documents of `collection` in the app's database, without `_id`."""

    async def _find():
        return [doc async for doc in client.app.state.db[collection].find(query or {}, {"_id": 0})]

    return client.portal.call(_find)


def write(client, collection, filter_, update, upsert=False):
    """Change documents in the app's database directly, to set up a state the API cannot produce yet."""

    async def _write():
        await client.app.state.db[collection].update_one(filter_, update, upsert=upsert)

    client.portal.call(_write)
