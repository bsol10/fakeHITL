"""The mock API's fixtures, and the script that extracts them from the prototype.

BACKEND_PLAN.md phase 0: fixtures are read out of the approved prototype
programmatically, never retyped, so they can be regenerated when it changes.
Three things are pinned here:

- the JS-literal parser reads what the prototype's arrays contain, and refuses
  what it does not understand rather than skipping past it;
- the committed fixtures still match the prototype (skipped when the prototype
  is not in the checkout);
- design rule 2 survives extraction. The prototype writes placeholder strings
  where a value does not exist; the fixtures hold null, so the contract never
  asks a later stage to emit text that looks like a finding and is not one.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "extract_prototype_fixtures.py"
PROTOTYPE = ROOT / "docs" / "frontend" / "asil-dashboard-DRAFT-FINAL.html"
FIXTURES = ROOT / "src" / "updater" / "api" / "fixtures"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("extract_prototype_fixtures", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


extract = _load_script()


def parse(text: str) -> Any:
    value, end = extract.parse_js_literal(text)
    assert end == len(text), f"stopped at {end} of {len(text)}"
    return value


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# --- the parser ------------------------------------------------------------


def test_parser_reads_the_prototypes_quoting() -> None:
    # Assembled rather than written out, so that the JS parser, not Python's
    # own string handling, is what decodes the escape.
    unicode_escape = "\\" + "u2019"
    assert parse(
        r"""{ before:'Not in last year\'s report', q:"say \"so\"", """
        f"curly:'department{unicode_escape}s', "
        """mixed:'"quoted" inside', approx:'n ≈ 4,000' }"""
    ) == {
        "before": "Not in last year's report",
        "q": 'say "so"',
        "curly": "department\N{RIGHT SINGLE QUOTATION MARK}s",
        "mixed": '"quoted" inside',
        "approx": "n ≈ 4,000",
    }


def test_numeric_keys_come_back_as_strings_as_in_javascript() -> None:
    assert parse("{ 1: { sent: 'a' }, 10: {}, 'x y': 2 }") == {
        "1": {"sent": "a"},
        "10": {},
        "x y": 2,
    }


def test_nested_arrays_keywords_numbers_and_trailing_commas() -> None:
    assert parse(
        "[ { id:1, conf:92.5, d:-3, unread:true, off:false, x:null,"
        " acts:[['Open','draft','primary'],], }, ]"
    ) == [
        {
            "id": 1,
            "conf": 92.5,
            "d": -3,
            "unread": True,
            "off": False,
            "x": None,
            "acts": [["Open", "draft", "primary"]],
        }
    ]


def test_parsing_stops_at_the_end_of_the_literal() -> None:
    text = "[1, 2];\n  const next = 3;"
    value, end = extract.parse_js_literal(text)
    assert value == [1, 2]
    assert text[end] == ";"


@pytest.mark.parametrize(
    "bad",
    [
        "{ a: undefined }",
        "[1 2]",
        "{ a: 'unterminated }",
        "{ a: 'line\nbreak' }",
        r"'\x41'",
        "{ a: 1, a: 2 }",
        "{ a: () => 1 }",
        "[1, 2",
    ],
)
def test_parser_refuses_what_it_does_not_understand(bad: str) -> None:
    with pytest.raises(extract.JsLiteralError):
        extract.parse_js_literal(bad)


# --- drift -----------------------------------------------------------------


@pytest.mark.skipif(not PROTOTYPE.exists(), reason="prototype not in this checkout")
def test_committed_fixtures_match_the_prototype(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert extract.main(["--check"]) == 0, capsys.readouterr().out


# --- what the transforms promise -------------------------------------------


def test_no_placeholder_string_survives_extraction() -> None:
    placeholders = set().union(*extract.PLACEHOLDERS.values())
    for name in ("claims.json", "draft.json"):
        for record in fixture(name):
            leaked = placeholders & {v for v in record.values() if isinstance(v, str)}
            assert not leaked, f"{name}: {leaked}"


def test_a_value_the_sources_do_not_state_is_null() -> None:
    claims = {claim["id"]: claim for claim in fixture("claims.json")}
    unverified = [c for c in claims.values() if c["status"] == "cannot"]
    assert unverified, "the prototype's cannot-verify claim went missing"
    for claim in unverified:
        assert claim["after"] is None
        assert claim["page"] is None
        # What last year's report said is still known, so it is not nulled.
        assert claim["before"] is not None

    new = [c for c in claims.values() if c["status"] == "new"]
    assert new
    assert all(claim["before"] is None for claim in new)
    for item in fixture("draft.json"):
        if claims[item["claim_id"]]["status"] == "new":
            assert item["prev"] is None


def test_a_claim_that_cannot_be_verified_has_no_draft_text() -> None:
    """A draft passage is source text at a verified anchor. An unverified claim
    has none, so no draft entry may exist for it to fall back on."""
    claims = {claim["id"]: claim for claim in fixture("claims.json")}
    for item in fixture("draft.json"):
        assert claims[item["claim_id"]]["status"] != "cannot"
        assert item["passage"]


def test_registry_rows_are_normalised_to_the_contract() -> None:
    sources = {source["id"]: source for source in fixture("sources.json")}
    outlook = sources["youth-employment-outlook-2026"]
    assert outlook["url"] == "https://abs.gov.au/employment"
    assert outlook["status"] == "approved"
    assert outlook["last_checked"] == "2026-09-13T09:00:00+10:00"  # "3 days ago"

    never = [s for s in sources.values() if s["status"] == "pending"]
    assert never
    assert all(source["last_checked"] is None for source in never)  # "Not yet"
    assert {s["status"] for s in sources.values()} == {
        "approved",
        "pending",
        "url_changed",
        "paused",
    }
