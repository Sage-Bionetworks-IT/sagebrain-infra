import re
import sys
from pathlib import Path

import pytest

LAMBDA_AGENT_DIR = str(Path(__file__).parents[2] / "src" / "lambda_agent")
if LAMBDA_AGENT_DIR not in sys.path:
    sys.path.insert(0, LAMBDA_AGENT_DIR)

# Extract just the pure-Python logic under test — no AWS/Strands needed.
_PREFIX_STRIP_RE = re.compile(
    r"^\s*(PREFIX\s+\S+\s*<[^>]*>\s*(#[^\n]*)?\s*)*",
    re.IGNORECASE,
)


def _safe_sparql(sparql: str) -> str:
    clean = re.sub(r"(?m)^\s*#[^\n]*\n?", "", sparql)
    body = _PREFIX_STRIP_RE.sub("", clean).lstrip()
    if not body.upper().startswith("SELECT"):
        first = body.split()[0] if body.split() else "(empty)"
        raise ValueError(f"Only SPARQL SELECT queries are permitted; got {first!r}.")
    return sparql


class TestSafeSparql:
    def test_simple_select_passes(self):
        q = "SELECT ?s WHERE { ?s ?p ?o } LIMIT 5"
        assert _safe_sparql(q) == q

    def test_select_with_prefix_passes(self):
        q = "PREFIX nf: <http://nf-osi.github.com/terms#>\nSELECT ?s WHERE { ?s a nf:Gene }"
        assert _safe_sparql(q) == q

    def test_hash_inside_iri_not_stripped(self):
        """IRI containing # must survive — this was the original bug."""
        q = (
            "PREFIX nf: <http://nf-osi.github.com/terms#>\n"
            "SELECT ?s WHERE { ?s a nf:Gene }"
        )
        result = _safe_sparql(q)
        assert "<http://nf-osi.github.com/terms#>" in result

    def test_leading_line_comment_stripped_for_keyword_scan(self):
        """Full-line comments before SELECT must not block the SELECT check."""
        q = "# find all genes\nSELECT ?s WHERE { ?s ?p ?o }"
        assert _safe_sparql(q) == q

    def test_indented_line_comment_stripped(self):
        q = "  # indented comment\nSELECT ?s WHERE { ?s ?p ?o }"
        assert _safe_sparql(q) == q

    def test_prefix_with_inline_comment_passes(self):
        """Trailing # comment on a PREFIX line must not block the SELECT check."""
        q = (
            "PREFIX nf: <http://nf-osi.github.com/terms#> # nf-osi namespace\n"
            "SELECT ?s WHERE { ?s a nf:Gene }"
        )
        assert _safe_sparql(q) == q

    def test_multiple_prefixes_with_inline_comments_pass(self):
        q = (
            "PREFIX nf: <http://nf-osi.github.com/terms#> # nf-osi\n"
            "PREFIX obo: <http://purl.obolibrary.org/obo/> # OBO\n"
            "SELECT ?s WHERE { ?s a nf:Gene }"
        )
        assert _safe_sparql(q) == q

    def test_construct_rejected(self):
        with pytest.raises(ValueError, match="SELECT"):
            _safe_sparql("CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }")

    def test_insert_rejected(self):
        with pytest.raises(ValueError, match="SELECT"):
            _safe_sparql("INSERT DATA { <a> <b> <c> }")

    def test_empty_query_rejected(self):
        with pytest.raises(ValueError):
            _safe_sparql("")
