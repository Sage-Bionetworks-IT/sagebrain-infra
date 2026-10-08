class QueryRejected(Exception):
    """Client error in the query/question itself. `message` is the contract's error string."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


class RateLimited(Exception):
    """A token bucket is empty. `scope` is "global" or "principal"."""

    def __init__(self, scope: str, retry_after: float):
        super().__init__(f"rate limited ({scope}); retry after {retry_after:.3f}s")
        self.scope = scope
        self.retry_after = retry_after
