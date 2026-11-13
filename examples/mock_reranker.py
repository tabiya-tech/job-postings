"""A stand-in for the LLM reranker, for running the stack locally without its models and LLM.

Answers `POST /rank` with a ranked document in the shape the real one returns (`ranked_document`, timings, ...), built
from the classified document it was sent: the classifier's skills become the "ranked" skills. No authentication. A
posting whose text contains `nothing_to_rank` gets `ranked_document: null`, `fail500` or `fail422` that error, and
`slow` takes three seconds, to try out the outcomes a real reranker has.

Standard library only: `python examples/mock_reranker.py` (port 8001, or the first argument).
"""
# pylint: disable=duplicate-code

import json
import sys
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def ranked_document(job: dict) -> dict:
    """The part of the real ranked document that matters for tracing a posting through the service."""
    entities = (job.get("classification") or {}).get("entities") or []
    skills = [{"label": e["label"], "uri": e["uri"]} for e in entities if e.get("type") == "skill"]
    return {
        "job_id": job.get("job_id"),
        "is_active": True,  # the real one always says this; the worker sets it from the posting's status
        "processed_at": datetime.now(UTC).isoformat(),
        "llm_classified_skills": {"essential": skills[:2], "optional": skills[2:], "discarded": []},
        "llm_job_attributes": {"summary": f"Mock summary of {job.get('title')}", "estimates": [], "attributes": {}},
        "classifier_metadata": {
            "title": job.get("title"),
            "employer": job.get("employer"),
            "city": job.get("city"),
            "province": job.get("province"),
            "sector": job.get("sector"),
            "zqf_min": job.get("zqf_min"),
        },
        "llm_reranker_meta": {"schema_version": "mock", "llm_provider": "mock"},
    }


class Handler(BaseHTTPRequestHandler):
    """Answers `/rank` and `/health`."""

    def _send(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # pylint: disable=invalid-name
        """Health check."""
        if self.path == "/health":
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {})

    def do_POST(self) -> None:  # pylint: disable=invalid-name
        """A made-up ranking of the classified posting."""
        if self.path.split("?")[0] != "/rank":
            self._send(404, {})
            return
        job = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        text = f"{job.get('title')} {job.get('description')}".lower()
        if "fail500" in text:
            self._send(500, {"detail": "mock failure"})
        elif "fail422" in text:
            self._send(422, {"detail": "mock rejection"})
        else:
            if "slow" in text:
                time.sleep(3)
            document = None if "nothing_to_rank" in text else ranked_document(job)
            self._send(
                200,
                {
                    "ranked_document": document,
                    "timings": {"total_s": 0.0},
                    "ran_llm1": document is not None,
                    "ran_llm2": document is not None,
                    "ran_llm3": False,
                    "llm3_outcome": "disabled",
                },
            )


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(sys.argv[1]) if len(sys.argv) > 1 else 8001), Handler).serve_forever()
