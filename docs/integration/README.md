# Sending job postings to job-postings

This guide is for the developers of a **source**: a scraper, an importer or any system that has job postings and wants them classified and ranked. Your server sends postings to the job-postings API with an API key. The service stores them, runs them through the classifier and the reranker, and lets you follow how each one went.

The full API specification is at `<API_URL>/docs` and `<API_URL>/openapi.json`. To run the service yourself, for example to try your integration, use the [getting-started guide](../getting-started/README.md): its mock services are enough.

To try the flow, run the [example script](#example-script) at the end of this guide.

## How it works

1. Your server sends an array of postings to `POST /job-postings`, with your API key.
2. The service checks each posting. It answers with one result per posting: `created`, `updated`, `unchanged`, `rejected` or `failed`. Valid postings are stored and queued; one bad posting does not stop the others.
3. A worker takes each posting through the classifier and the reranker. You can follow its `stage`: `queued`, `classifying`, `ranking`, `done` or `failed`.
4. When a posting is no longer open, you close it (or its `closing_date` passes).

You send what the posting says. You do not send IDs of the service, scores, or anything derived from the text (sector, occupation groups, skills): the pipeline produces those.

## Before you start

Ask the operator of the service for:

- **The API URL** (`API_URL`), for example `https://job-postings.example.org`.
- **An API key**, to send in the `x-api-key` header. It belongs to one source (yours) and lists the countries you may post to.

Call the API from your server only. Never put the key in a web page, a mobile app or a URL. A source can have several keys, so a key can be replaced without stopping your integration.

## Send postings: `POST /job-postings`

The body is an array of postings, at most 500. To send one posting, send an array of one.

| Field | Required | Description |
|---|---|---|
| `external_id` | yes | Your own ID for the posting, up to 256 characters: the job board's listing ID or URL, for example. The same `external_id` from your source is always the same posting. See [Resending is safe](#resending-is-safe). |
| `title` | yes | The job title. |
| `description` | yes | The full description. The classifier reads the title and this text, so send all of it. |
| `country` | yes | The country, as a slug: `kenya`, `south_africa`. Case and spaces are normalised (`South Africa` becomes `south_africa`). It must be one of the countries of your key. |
| `language` | no | ISO 639-1 code, for example `es`. Left out, it is the usual language of the country. |
| `requirements` | no | Requirements text. |
| `education`, `experience` | no | As published, free text. |
| `employer` | no | |
| `location` | no | As published, for example `Westlands, Nairobi`. |
| `city`, `region` | no | `region` is the province, county or state. |
| `category` | no | Your own category for the posting. |
| `employment_type` | no | One of `full_time`, `part_time`, `contract`, `temporary`, `internship`, `volunteer`, `other`. |
| `salary` | no | As published, free text. |
| `posted_date`, `closing_date` | no | ISO dates, for example `2026-12-01`. The `closing_date` closes the posting automatically, see [Close postings](#close-postings). |
| `application_url` | no | A URL. |
| `extra` | no | An object with anything specific to your source. It is stored as it is. The service does not interpret it, with one exception: `sector` and the Zambia qualification fields (`zqf_min`, `zqf_max`, `zqf_min_label`, `zqf_max_label`) are passed on to the ranked document. |

A field sent as empty text (`""`) is the same as leaving it out. Text fields are limited to 10,000 characters, `description` and `requirements` to 200,000, and `extra` to 100,000 characters as JSON. Any other top-level field is rejected: put it in `extra`.

```bash
curl -i -X POST "$API_URL/job-postings" \
  -H "x-api-key: $API_KEY" -H "content-type: application/json" \
  -d '[{"external_id": "A-17", "title": "Electrician",
        "description": "Install and maintain electrical wiring in homes.",
        "country": "kenya", "employer": "Acme Ltd", "city": "Nairobi",
        "employment_type": "full_time", "closing_date": "2026-12-01",
        "application_url": "https://jobs.example.org/a-17"}]'
```

The answer is `202 Accepted` with one result per posting, in the order you sent them (`index`):

```json
{
  "results": [
    {"index": 0, "id": "8526e53c-4acd-4dfd-9795-dafd7e104f8d", "external_id": "A-17", "status": "created"},
    {"index": 1, "status": "rejected", "error": "description: Field required"},
    {"index": 2, "external_id": "A-09", "status": "rejected",
     "error": "country: this API key may not post to 'uganda' (allowed: kenya)"}
  ]
}
```

Keep the `id`: it is how you read the posting later. It never changes, even when the posting does.

| `status` | Meaning | What to do |
|---|---|---|
| `created` | A new posting. | Nothing. |
| `updated` | You sent this `external_id` before, and the posting changed, was closed and is open again, or failed processing and is queued again. It keeps its `id`. | Nothing. |
| `unchanged` | You sent this `external_id` before, with the same content. Nothing runs again. | Nothing. |
| `rejected` | The posting is invalid. `error` says why: a missing field, a wrong type or value, a field over its limit, an unknown field, a country your key may not post to, or the same `external_id` twice in the request. The other postings of the request are not affected. | Fix the posting. Sending it again unchanged gives the same result. |
| `failed` | The service could not store this posting (a database error). | Send it again. |

### Resending is safe

The pair of your source and the `external_id` identifies a posting. Sending it again never creates a second one: it updates the stored posting, or answers `unchanged`. So when a request times out, fails, or you do not know whether it arrived, **send it again**. You can also send all your postings on a schedule: only the ones that changed are processed again.

Fields you leave out of an update are kept; fields you send as `null` are cleared. A change to any field sends the posting through classification and ranking again.

## Follow a posting

Everything you read is scoped to your source: you only see your own postings, and the `id` of another source's posting answers `404`.

**`GET /job-postings`** lists your postings, newest first.

```bash
curl -H "x-api-key: $API_KEY" "$API_URL/job-postings?stage=failed&limit=50"
```

```json
{
  "items": [
    {"id": "8526e53c-...", "external_id": "A-17", "title": "Electrician", "country": "kenya",
     "status": "active", "stage": "done", "created_at": "2026-11-13T20:01:27Z", "updated_at": "2026-11-13T20:01:27Z"}
  ],
  "next": null
}
```

Filter by `country`, `status` (`active` or `closed`), `stage`, `external_id` or `updated_since` (an ISO time: what changed since you last looked). `limit` is 50 by default and at most 200. When `next` is not null, pass it as `cursor` to get the following page; postings that arrive meanwhile never make you miss or repeat one.

**`GET /job-postings/{id}`** returns one posting as stored, plus how each stage went:

```json
{
  "id": "8526e53c-...", "external_id": "A-17", "title": "Electrician", "status": "active", "stage": "failed",
  "processing": {
    "classify": {"status": "done", "attempts": 1, "last_error": null, "outcome": "classified", "updated_at": "2026-11-13T20:01:28Z"},
    "rank": {"status": "failed", "attempts": 5, "last_error": "reranker error (500)", "outcome": null, "updated_at": "2026-11-13T20:09:51Z"}
  }
}
```

Add `?include=classification,ranking` to get what the stages stored (`classification` from the classifier, `ranking` from the reranker). They are large, and the services that use them read them from the database, so ask for them only when you want to look.

`stage` tells you where a posting is:

| `stage` | Meaning |
|---|---|
| `queued` | Waiting for the classifier. |
| `classifying` | The classifier is working on it. |
| `ranking` | Classified; the reranker is working on it, or waiting for its turn. Ranking is the slow step (about half a minute with the real reranker). |
| `done` | Classified and ranked. |
| `failed` | A stage used all its attempts. `processing` shows which, and `last_error` the reason. A stage is tried up to five times, with a longer wait each time, over about eight minutes. **Send the posting again** to queue the failed stage again. |

Some postings finish `done` with nothing ranked: when the reranker finds nothing to rank, the posting is `done` and `processing.rank.outcome` is `nothing_to_rank`. That is not a failure.

## Close postings

A closed posting stays stored but is no longer live: it is not offered to jobseekers. A posting closes in three ways:

- **Its `closing_date` passes.** The posting is closed automatically, with `closed_reason` `expired`, once the closing date has passed (UTC; the service checks every hour). A posting that arrives with a `closing_date` already in the past is stored closed.
- **You close it:** `POST /job-postings/close`, with the `external_ids` of your postings, or their `ids`, not both. At most 500.

  ```bash
  curl -X POST "$API_URL/job-postings/close" \
    -H "x-api-key: $API_KEY" -H "content-type: application/json" \
    -d '{"external_ids": ["A-17", "A-09"]}'
  ```

  ```json
  {"results": [
    {"index": 0, "id": "8526e53c-...", "external_id": "A-17", "status": "closed"},
    {"index": 1, "external_id": "A-09", "status": "not_found"}
  ]}
  ```

  A result is `closed`, `already_closed`, or `not_found` (it does not exist, or it is another source's). The posting then shows `status: closed`, `closed_at` and `closed_reason: closed_by_source`.
- **Reopening.** Sending a closed posting again, with a `closing_date` in the future or none, makes it active again.

The service does not guess that a posting is gone because you stopped sending it. If your source sends all its live postings each time, close the ones that are no longer in your list yourself.

## Limits and errors

Each API key may send **60 requests and 1,000 postings per minute** (the operator can change both for your key). Over the limit, the answer is `429` with a `Retry-After` header, in seconds: wait that long and send the same request again. A request refused for its rate stores nothing.

| Status | Meaning |
|---|---|
| `202` | Accepted. Read each posting's own result. |
| `401` | No `x-api-key`, or an unknown key. |
| `403` | The key is an admin key, which can read every source's postings but cannot post or close. |
| `404` | No such posting for your key. |
| `422` | The request itself is wrong: the body is not an array, the array is empty or longer than 500, `include` has an unknown value, the close request has neither or both of `external_ids` and `ids`. A single bad posting is not a `422`: it is `rejected` in the results. |
| `429` | Too many requests or postings this minute. |
| `5xx` | The service had a problem. Send the request again. |

## Not in this version

- **Duplicates across sources** (the same job on two job boards) are not detected or merged. Each source's postings are separate.
- **No content filtering.** The service checks the shape of a posting, not whether it is worth classifying. Skipping postings with a generic title or too short a description is up to your source.
- **No translation.** A posting is classified and ranked in the language it came in.
- **No webhooks.** To learn that a posting finished or failed, read it, or list with `updated_since`.

## Example script

[examples/send_postings.py](../../examples/send_postings.py) sends the postings of a JSON file the way this guide recommends: in batches, sending a request again when it fails, answers `429` or `5xx` (waiting `Retry-After`), and sending the postings the service answered `failed` again. It does not resend `rejected` postings, which need fixing. It uses only the Python standard library.

```bash
cat > postings.json <<'JSON'
[
  {"external_id": "S-1", "title": "Cook", "description": "Cooking meals for a restaurant of fifty seats.", "country": "kenya"},
  {"external_id": "S-2", "title": "Driver", "description": "Driving a delivery van on city routes.", "country": "kenya"}
]
JSON
API_URL=http://localhost:8090 API_KEY=<your key> python3 examples/send_postings.py postings.json
```

```
{'created': 2}
```

Run it twice and the second answer is `{'unchanged': 2}`.
