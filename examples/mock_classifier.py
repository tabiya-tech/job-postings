"""A stand-in for the classifier, for running the stack locally without its models.

Answers `POST /v1/classify` with a made-up classification: an occupation named after the title and a skill for each
keyword of `SKILLS` found in the text. No authentication, like the real classifier in local mode. A posting that
contains `fail500` or `fail422` makes it answer with that error, to try out retries and failures.

Standard library only: `python examples/mock_classifier.py` (port 8000, or the first argument).
"""
# pylint: disable=duplicate-code
# (The two mocks are mounted into containers one file each, so they cannot share a module.)

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Skills the mock "finds", by a word in the posting, so different postings get different results.
SKILLS = {
    "wiring": "install electrical wiring",
    "welding": "weld metal parts",
    "nursing": "provide nursing care",
    "driving": "drive vehicles",
    "excel": "use spreadsheets software",
    "bookkeeping": "perform bookkeeping",
    "teaching": "teach students",
    "cooking": "prepare food",
    "selling": "sell products",
    "python": "use programming languages",
}


class Handler(BaseHTTPRequestHandler):
    """Answers `/v1/classify` and `/health`."""

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
        """A made-up classification of the posting."""
        if self.path != "/v1/classify":
            self._send(404, {})
            return
        request = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        title = request.get("title") or request.get("text") or ""
        text = f"{title} {request.get('description') or ''}".lower()
        if "fail500" in text:
            self._send(500, {"detail": "mock failure"})
            return
        if "fail422" in text:
            self._send(422, {"detail": "mock rejection"})
            return
        entities = [
            {
                "type": "occupation",
                "label": title,
                "uri": "http://data.europa.eu/esco/occupation/mock",
                "similarity_score": 0.9,
            }
        ] + [
            {
                "type": "skill",
                "label": label,
                "uri": f"http://data.europa.eu/esco/skill/mock-{word}",
                "similarity_score": 0.8,
            }
            for word, label in SKILLS.items()
            if word in text
        ]
        self._send(
            200,
            {
                "classification": {"entities": entities},
                "metadata": {"mock": True, "language": (request.get("options") or {}).get("language")},
            },
        )


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(sys.argv[1]) if len(sys.argv) > 1 else 8000), Handler).serve_forever()
