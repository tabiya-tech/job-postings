# Getting started: run job-postings on your own machine

This guide takes you from a fresh clone to postings flowing through the whole pipeline on your own computer or server: the API, the worker, MongoDB, and the classifier and reranker. There are two ways to run the last two:

- **[Mock services](#run-with-mock-services)** (start here): small stand-ins for the classifier and the reranker. One command starts everything, nothing is downloaded and nothing costs money. Use it to try the API and to develop.
- **[Real services](#run-with-the-real-classifier-and-reranker)**: the Tabiya classifier and the LLM reranker, with their models. Use it to check what the pipeline really produces.

You will end up with:

- the API running at `http://localhost:8090`,
- postings sent with an API key and followed from `queued` to `done`,
- the classified and ranked documents in MongoDB.

> **Scope.** This is a guide for dev environments, not a hardened production deployment. Tested on macOS (Apple silicon, Docker Desktop). If something fails, please open an issue.
>
> **Run it on a machine you trust.** The classifier and the reranker have no authentication in this setup (the classifier runs in local mode), and the classifier's compose file publishes its ports on every network interface of the host. Do not expose them to the internet or an untrusted network; the reranker's port is bound to `localhost`. The MongoDB of the compose file has no password and is not published. Only the job-postings API is meant to be reached by other machines, and it needs an API key.

## The pieces

- The **API** receives postings from sources, checks their API key, stores them and queues them. It is a FastAPI server.
- The **worker** takes queued tasks one at a time, calls the classifier, then the reranker, and stores each result. It is the same image as the API, started with `worker`.
- **MongoDB** holds the postings (`scraped_jobs`), the results (`classified_jobs`, `ranked_jobs`) and the task queue (`tasks`). Only the API and the worker connect to it.
- The **classifier** finds the occupations and skills in a posting. With Docker it is three containers: `ner`, `nel` and `classify`.
- The **reranker** ranks those skills and estimates job attributes with a language model (Gemini by default). It is the only piece that needs an account outside your machine.

How a posting moves is in the [README](../../README.md#how-it-works).

## Run with mock services

Needs [Docker](https://docs.docker.com/engine/install/) with Compose v2, Git and Python 3 (to create an API key).

1. Clone the repository and create your configuration:

```bash
git clone https://github.com/tabiya-tech/job-postings.git
cd job-postings
cp .env.example .env
```

2. Create an API key. It belongs to one source and lists the countries it may post to:

```bash
python3 scripts/mint_api_key.py --source my-scraper --countries kenya
```

The script prints the key once (keep it: it is not stored anywhere) and a JSON entry. Put the entry in `.env` as one line:

```bash
API_KEYS={"<sha256 of the key>": {"source": "my-scraper", "countries": ["kenya"]}}
```

3. Start everything:

```bash
docker compose up -d --build
```

The first start builds the image and pulls MongoDB, which takes a few minutes. It starts MongoDB, the API, one worker, and the two mock services. Check it:

```bash
curl http://localhost:8090/health
```

You should see `{"status":"ok","mongodb":"ok"}`.

4. Send a posting, with the key from step 2:

```bash
export KEY=<the key>
curl -X POST http://localhost:8090/job-postings \
  -H "x-api-key: $KEY" -H "content-type: application/json" \
  -d '[{"external_id": "A-17", "title": "Electrician", "description": "Install and maintain electrical wiring in homes.", "country": "kenya"}]'
```

The answer has one result per posting, with its `id`:

```json
{"results": [{"index": 0, "id": "5b1f...", "external_id": "A-17", "status": "created"}]}
```

5. Follow it. A posting goes `queued`, `classifying`, `ranking`, `done`, in a few seconds with the mocks:

```bash
curl -H "x-api-key: $KEY" "http://localhost:8090/job-postings?external_id=A-17"
curl -H "x-api-key: $KEY" "http://localhost:8090/job-postings/<id>?include=classification,ranking"
```

The second call shows the posting, how each stage went (`processing`, with attempts and the last error) and, with `include`, what the stages stored. Send the same request again: the result is `unchanged`, and nothing runs twice.

The [integration guide](../integration/README.md) describes the whole API, including closing postings, and is the one to give to the developers of a source; `http://localhost:8090/docs` has the full schema. [examples/client.html](../../examples/client.html) is a small browser client for the same calls.

### What the mocks do

The mocks only keep the pipeline moving; their output is made up.

- **Classifier** ([examples/mock_classifier.py](../../examples/mock_classifier.py)): an occupation named after the title, and a skill for each of ten keywords (`wiring`, `welding`, `nursing`, and so on) found in the text.
- **Reranker** ([examples/mock_reranker.py](../../examples/mock_reranker.py)): a ranked document built from the classifier's skills.
- To see the failure paths, put a trigger word in the title or description: `fail500` makes the mocks answer `500` (the task retries with a backoff and fails after `MAX_ATTEMPTS`), `fail422` a `422` (it fails at once), `nothing_to_rank` makes the reranker answer with nothing to rank (the task is `done`, with no ranked document), and `slow` delays the reranker three seconds.

## Run with the real classifier and reranker

The real services are two other Tabiya repositories. They are cloned next to this one, and `docker-compose.real.yml` replaces the mocks with them.

Needs everything above, plus:

- a **Gemini API key**, which the reranker uses. Create one in [Google AI Studio](https://aistudio.google.com/apikey). It is an API key for the Gemini API, not a Google Cloud or Vertex AI credential. Other model providers work too, see the [reranker's README](https://github.com/tabiya-tech/llm-reranker#choosing-the-model).
- Docker with Compose 2.24 or later (the file includes the classifier's own compose file and uses `!reset`), and about **20 GB of free disk space for Docker**. The images carry the language models: the classifier's are about 8 GB. The first build can take 15 minutes or more, mostly downloading models. We ran it with 16 GB of memory given to Docker.

1. Clone both next to `job-postings`:

```bash
cd ..
git clone https://github.com/tabiya-tech/tabiya-livelihoods-classifier.git
git clone https://github.com/tabiya-tech/llm-reranker.git
cd job-postings
```

2. Configure the classifier. Copy its example and give it a MongoDB to open at startup. Without one it exits with `MongoDB health check failed`, even though `/v1/classify` does not use it. The one in this compose is enough:

```bash
cp ../tabiya-livelihoods-classifier/.env.example ../tabiya-livelihoods-classifier/.env
```

Then, in `../tabiya-livelihoods-classifier/.env`, set these two (delete the placeholder lines of the same names):

```bash
APPLICATION_MONGODB_URI=mongodb://mongodb:27017
MONGODB_DB_NAME=classifier_unused
```

The `HF_TOKEN` line is not needed: the NER model is public. The classifier runs with `TARGET_ENVIRONMENT_TYPE=local`, its compose default: no API key and no user lookup.

3. Configure the reranker. Copy its example and add your key:

```bash
cp ../llm-reranker/.env.example ../llm-reranker/.env
```

Set `GEMINI_API_KEY=` in `../llm-reranker/.env`. Never commit that file.

4. Start the stack with the two compose files:

```bash
docker compose -f docker-compose.yml -f docker-compose.real.yml up -d --build
```

The first start builds the five images and downloads the models. While `docker compose` runs you may see `worker Error pull access denied for job-postings`: it is harmless, because the `api` service builds that image in the same run. The mock classifier of the main file still starts but is not used.

5. Wait until the classifier says it is healthy. NER and NEL take a few minutes to load their models:

```bash
curl http://localhost:5001/v1/health
```

You should see `"status":"healthy"` with `ner_api` and `nel_api` healthy. The reranker is ready when `docker compose logs reranker` ends with `Application startup complete`.

6. Try the classifier on its own, then send postings as in [step 4 above](#run-with-mock-services):

```bash
curl -X POST http://localhost:5001/v1/classify -H "content-type: application/json" \
  -d '{"title": "Electrician", "description": "Install and repair electrical wiring.", "options": {"language": "en"}}'
```

Ranking is the slow stage: about 30 seconds a posting, from two Gemini calls, and the worker runs four at a time (`WORKER_CONCURRENCY`). Start with ten postings, to keep an eye on your Gemini usage.

To see what was stored, look at the collections with `mongosh`:

```bash
docker compose exec mongodb mongosh job_postings --eval "db.ranked_jobs.findOne({}, {classifier_metadata: 1, llm_classified_skills: 1})"
```

## Day to day

| To | Do |
|---|---|
| See the logs | `docker compose logs -f worker` (or `api`, `classify`, `reranker`) |
| Stop, keeping the data | `docker compose down` |
| Wipe everything, including the database | `docker compose down -v` |
| Change the API's port | `JOB_POSTINGS_PORT` in `.env` (default `8090`) |
| Add or change a key | edit `API_KEYS` in `.env`, then `docker compose up -d` |
| Run postings faster | raise `WORKER_CONCURRENCY`, or start more workers: `docker compose up -d --scale worker=3` |
| Start a failed posting again | send the same posting again: a posting whose `stage` is `failed` is queued again, and the result is `updated` |
| Switch between mocks and real services | stop, then `up` with or without `-f docker-compose.real.yml` |

When you change an `.env` value, run `docker compose up -d` again so the containers pick it up.

## Run without Docker

To run the API and the worker as Python processes (Python 3.12, [Poetry](https://python-poetry.org/) 2.1), with a MongoDB of your own and the classifier and reranker already running:

```bash
poetry sync
cp .env.example .env
poetry run python -m app api
poetry run python -m app worker
```

In `.env`, set `MONGODB_URI`, `CLASSIFIER_URL` and `RERANKER_URL`, and `API_KEYS` as in [step 2](#run-with-mock-services). The API listens on `API_PORT` (default `8080`).

## Configuration

Everything is read from the environment, or from `.env`. Only `MONGODB_URI` is required (the compose file sets it).

| Variable | Default | Meaning |
|---|---|---|
| `MONGODB_URI` | required | MongoDB connection string |
| `MONGODB_DATABASE_NAME` | `job_postings` | Database for every collection |
| `MONGODB_SERVER_SELECTION_TIMEOUT_MS` | `5000` | How long a call waits for a reachable MongoDB |
| `API_HOST`, `API_PORT` | `0.0.0.0`, `8080` | Where the API listens |
| `API_KEYS` | `{}` | Keys as JSON: `{"<sha256 of key>": {"source": "...", "countries": ["..."]}}`. An entry can also have `"admin": true` (reads every source, cannot post) and `"rate_limit": {...}` |
| `API_KEYS_FILE` | none | A JSON file of the same shape, merged with `API_KEYS` |
| `MAX_POSTINGS_PER_REQUEST` | `500` | Largest array accepted |
| `CORS_ALLOW_ORIGINS` | `[]` | Browser origins allowed to call the API, as JSON |
| `DEFAULT_LANGUAGE` | `en` | Language of a posting that does not give one |
| `COUNTRY_LANGUAGES` | `{}` | Language by country as JSON, e.g. `{"argentina": "es"}` |
| `MAX_ATTEMPTS` | `5` | Attempts per task before it is marked failed |
| `POLL_INTERVAL_SECONDS` | `5` | How long a worker loop sleeps when there is nothing to do |
| `WORKER_CONCURRENCY` | `4` | Tasks one worker process runs at the same time |
| `LOCK_TIMEOUT_SECONDS` | `600` | After this long, a task a worker took and did not finish can be taken by another. Keep it longer than the slowest stage |
| `CLASSIFIER_URL` | `http://localhost:8000` | Where `POST /v1/classify` is. The compose files set it |
| `CLASSIFIER_API_KEY` | none | Sent as `x-api-key` when set |
| `CLASSIFIER_TIMEOUT_SECONDS` | `60` | How long one classifier call may take |
| `RERANKER_URL` | `http://localhost:8001` | Where `POST /rank` is. The compose files set it |
| `RERANKER_API_KEY` | none | Sent as `x-api-key` when set |
| `RERANKER_TIMEOUT_SECONDS` | `180` | How long one reranker call may take |
| `EXPIRY_INTERVAL_MINUTES` | `60` | How often the worker closes postings past their `closing_date` |
| `RATE_LIMIT_REQUESTS_PER_MINUTE` | `60` | Requests per API key and minute |
| `RATE_LIMIT_POSTINGS_PER_MINUTE` | `1000` | Postings sent per API key and minute |
| `LOG_LEVEL` | `INFO` | |

The classifier and the reranker have settings of their own; see their repositories.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `401` from the API | No `x-api-key`, or its SHA-256 is not in `API_KEYS` | Send the key that `mint_api_key.py` printed, and put its entry in `.env`; then `docker compose up -d` |
| A posting is `rejected` with `country: this API key may not post to ...` | The key does not list that country | Add the country to the key's `countries` |
| `429` from the API | The key passed its rate limit | Wait for `Retry-After` seconds; raise the limits for that key |
| A posting stays `queued` | The worker is not running, or has not claimed it yet | `docker compose ps`; `docker compose logs worker` |
| `processing.classify.last_error` is `classifier unreachable` | The classifier is still loading its models, or `CLASSIFIER_URL` is wrong | Wait for `curl localhost:5001/v1/health` to say `healthy`; the task retries by itself |
| `last_error` is `reranker error (500)` | The reranker failed, for example on its call to Gemini | `docker compose logs reranker` shows the cause |
| Reranker log: `JSON mode is not enabled for api version v1` | A fresh build of the reranker gets a newer Gemini SDK that calls the `v1` API | Already set in `docker-compose.real.yml` (`GOOGLE_GENAI_API_VERSION=v1beta`); keep it if you write your own compose file |
| Reranker log about the API key | `GEMINI_API_KEY` is empty or wrong | Fix `../llm-reranker/.env`, then `docker compose up -d reranker` |
| A posting's `stage` is `failed` | A task used all its attempts (about eight minutes of retries) | Fix the cause in the logs, then send the posting again |
| `classify` exits with `MongoDB health check failed` | The classifier has no MongoDB to open at startup | Set `APPLICATION_MONGODB_URI` and `MONGODB_DB_NAME` in its `.env` ([step 2](#run-with-the-real-classifier-and-reranker)) |
| Build fails with `no space left on device` | Docker's disk is full | Raise the disk limit in Docker Desktop (Settings, Resources), or run `docker builder prune` |
| `port is already allocated` | Another service uses 8090, 5001 or 8001 | Set `JOB_POSTINGS_PORT` in `.env`, or stop the other service |
| The reranker image runs slowly on Apple silicon | Its image is for `linux/amd64` and runs emulated | Expected; allow more time per posting |
