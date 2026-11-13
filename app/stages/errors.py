class StageError(Exception):
    """A stage could not finish for this posting.

    `reason` is what the source sees as the task's `last_error` (see `GET /job-postings/{id}`), so it is a status code
    and a fixed text, never what the other service answered.
    """

    def __init__(self, reason: str, *, retryable: bool = True, busy: bool = False, retry_after: float | None = None):
        super().__init__(reason)
        self.reason = reason
        # False: the same request would fail again (the classifier answered `422`), so the task fails at once.
        self.retryable = retryable
        # True: the other service is overloaded (`429`, `503`). The task goes back without using up an attempt.
        self.busy = busy
        # Seconds the other service asked us to wait, if it said.
        self.retry_after = retry_after
