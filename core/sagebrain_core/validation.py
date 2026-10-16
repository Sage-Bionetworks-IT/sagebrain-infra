import re

from . import limits
from .errors import QueryRejected

SERVICE_REJECTED = "SERVICE (federated query) is not permitted"

# Tokens that can contain the word SERVICE without it being the keyword. One alternation, scanned
# left to right like the SPARQL lexer, so a '#' inside an IRI or string isn't read as a comment.
_NON_KEYWORD_TOKENS = re.compile(
    r"<[^<>\"{}|^`\\\x00-\x20]*>"  # IRIREF (excluding the grammar's excluded characters)
    r'|"""(?:[^"\\]|\\.|"(?!""))*"""'  # long strings
    r"|'''(?:[^'\\]|\\.|'(?!''))*'''"
    r'|"(?:[^"\\\n\r]|\\.)*"'  # short strings
    r"|'(?:[^'\\\n\r]|\\.)*'"
    r"|#[^\n\r]*"  # comments
)
# The keyword, not part of a prefixed name (ex:service, service:x) or a variable (?service).
_SERVICE_KEYWORD = re.compile(r"(?<![\w:?$\-])SERVICE(?![\w:\-])", re.IGNORECASE)
_CODEPOINT_ESCAPE = re.compile(r"\\u([0-9A-Fa-f]{4})|\\U([0-9A-Fa-f]{8})")


def _unescape_codepoints(query: str) -> str:
    # SPARQL allows \uXXXX escapes anywhere in a query, processed before parsing.
    return _CODEPOINT_ESCAPE.sub(
        lambda m: chr(min(int(m.group(1) or m.group(2), 16), 0x10FFFF)), query
    )


def has_service_keyword(query: str) -> bool:
    """True if the query uses SERVICE outside comments, strings and IRIs (spec 004 FR-15).

    Checked on the query as written and with codepoint escapes decoded, so `\\u0053ERVICE`
    can't slip through whichever way Neptune reads it.
    """
    return any(
        _SERVICE_KEYWORD.search(_NON_KEYWORD_TOKENS.sub(" ", text))
        for text in (query, _unescape_codepoints(query))
    )


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
    """Return the stripped SPARQL query or raise QueryRejected (messages per api/openapi.yaml).

    SERVICE is rejected: a federated query is an outbound request path, and a self-SERVICE
    probe exhausted dev Neptune's memory for ~7 minutes (spec 004 D-3).
    """
    query = _validate_text(value, "query", "Query", limits.QUERY_MAX_CHARS)
    if has_service_keyword(query):
        raise QueryRejected(SERVICE_REJECTED)
    return query


def validate_question(value) -> str:
    """Return the stripped question or raise QueryRejected (messages per api/openapi.yaml)."""
    return _validate_text(value, "question", "Question", limits.QUESTION_MAX_CHARS)


DEFAULT_SOURCE = "direct"


def validate_source(value) -> str:
    """Return the caller tag (X-Source) or raise QueryRejected. Not stripped: logged verbatim."""
    if value is None:
        return DEFAULT_SOURCE
    if len(value) > limits.SOURCE_MAX_CHARS:
        raise QueryRejected(
            f"'X-Source' exceeds maximum length of {limits.SOURCE_MAX_CHARS} characters"
        )
    return value
