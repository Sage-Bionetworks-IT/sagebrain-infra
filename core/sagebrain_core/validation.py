from . import limits
from .errors import QueryRejected


def _validate_text(value, field: str, label: str, max_chars: int) -> str:
    if value is None:
        raise QueryRejected(f"Missing '{field}' field")
    if not isinstance(value, str):
        raise QueryRejected(f"'{field}' must be a string")
    text = value.strip()
    if not text:
        raise QueryRejected(f"Missing '{field}' field")
    if len(text) > max_chars:
        raise QueryRejected(f"{label} exceeds maximum length of {max_chars} characters")
    return text


def validate_query(value) -> str:
    """Return the stripped SPARQL query or raise QueryRejected (messages per api/openapi.yaml)."""
    return _validate_text(value, "query", "Query", limits.QUERY_MAX_CHARS)


def validate_question(value) -> str:
    """Return the stripped question or raise QueryRejected (messages per api/openapi.yaml)."""
    return _validate_text(value, "question", "Question", limits.QUESTION_MAX_CHARS)
