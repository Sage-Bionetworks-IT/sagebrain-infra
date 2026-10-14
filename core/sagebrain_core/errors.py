class QueryRejected(Exception):
    """Client error in the query/question itself. `message` is the contract's error string."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status
