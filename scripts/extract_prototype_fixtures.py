"""Extract the dashboard prototype's hardcoded data into the mock API's fixtures.

The approved prototype, ``docs/frontend/asil-dashboard-DRAFT-FINAL.html``, is
the source of truth for every shape the API returns (BACKEND_PLAN.md). It makes
no network calls: its data is four arrays in its ``<script>`` and the rest is
written into its markup. This reads all of it out, so the phase 0 mock serves
exactly what the prototype shows and nobody retypes it by hand::

    uv run python scripts/extract_prototype_fixtures.py           # rewrite
    uv run python scripts/extract_prototype_fixtures.py --check   # drift check

``--check`` exits 1 when the committed fixtures no longer match the prototype.
The test suite runs it, so a revised prototype fails the build until the
fixtures are regenerated and the diff has been read.

Everything is copied verbatim except for four transforms, each deliberate:

- **Placeholder text becomes null.** The prototype writes "Not retrieved",
  "unknown" and "Not in last year's report" where a value does not exist.
  Design rule 2: a value the sources do not state is null, never a placeholder.
  A contract carrying those strings would oblige the compare stage to emit them,
  and a missing value would then read exactly like a real one. Only the strings
  in ``PLACEHOLDERS`` are mapped, and only in the field named; the frontend
  renders the label.
- **Display labels become the contract's enum values** (source status, scan
  outcome state, settings selections). An unrecognised label raises rather than
  being guessed at. Every option of a settings ``<select>`` is mapped, not only
  the selected one, so a new option in a revised prototype fails loudly here.
- **Relative dates become timestamps.** "3 days ago" is counted back from the
  date the prototype's dashboard shows, at 9:00 am AEST — the scheduled scan
  time in its settings. "Not yet" is null.
- **Registry URLs gain a scheme.** The table shows ``abs.gov.au/employment``.

The JS parser below is deliberately narrow. It reads what the prototype's
arrays contain and refuses anything else, because a parser that skips what it
does not understand would let a revised prototype be half-extracted without
anyone noticing.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from lxml import html as lxml_html

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HTML = ROOT / "docs" / "frontend" / "asil-dashboard-DRAFT-FINAL.html"
DEFAULT_OUT = ROOT / "src" / "updater" / "api" / "fixtures"

# September in Sydney is before daylight saving starts, so +10:00 throughout.
AEST = timezone(timedelta(hours=10))
SCAN_TIME = time(9, 0)
"""The prototype's scheduled scan time ("Run on: Monday, 9:00 am")."""

PLACEHOLDERS: dict[str, frozenset[str]] = {
    "before": frozenset({"Not in last year's report"}),
    "after": frozenset({"Not retrieved"}),
    "page": frozenset({"unknown"}),
    "prev": frozenset({"Not in the 2025 report."}),
}

# Field sets, checked exactly. The null transform above is keyed by field name,
# so a renamed field would silently escape it; failing here is what forces the
# API schema and this script to be updated together.
CLAIM_KEYS = frozenset(
    {"id", "type", "status", "title", "src", "page", "before", "after", "conf"}
    | {"note", "section", "lastYear"}
)
DRAFT_KEYS = frozenset({"sent", "passage", "prev"})
OUTCOME_KEYS = frozenset({"st", "d"})
NOTIFICATION_KEYS = frozenset(
    {"id", "kind", "ico", "when", "t", "title", "unread", "body", "acts"}
)
REGISTRY_COLUMNS = (
    "",
    "SOURCE",
    "OWNER",
    "TOPIC",
    "FORMAT",
    "UPDATE FREQ.",
    "STATUS",
    "LAST CHECKED",
    "",
)

SOURCE_STATUS = {
    "Approved": "approved",
    "Pending sign-off": "pending",
    "URL changed": "url_changed",
    "Paused": "paused",
}
OUTCOME_STATE = {"ok": "complete", "attn": "attention"}
SCAN_FREQUENCY = {
    "Weekly": "weekly",
    "Fortnightly": "fortnightly",
    "Monthly": "monthly",
    "Manual only": "manual",
}
CHANGE_SENSITIVITY = {
    "Balanced": "balanced",
    "Only major changes": "major",
    "Every change": "every",
}


class FixtureError(ValueError):
    """The prototype does not have the shape this script expects."""


class JsLiteralError(FixtureError):
    """The script text is not a literal this parser understands."""


# --------------------------------------------------------------------------
# JavaScript literals
# --------------------------------------------------------------------------

_IDENTIFIER = re.compile(r"[A-Za-z_$][\w$]*")
_NUMBER = re.compile(r"-?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_KEYWORDS: dict[str, Any] = {"true": True, "false": False, "null": None}
_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "b": "\b",
    "f": "\f",
    "v": "\v",
    "0": "\0",
    "'": "'",
    '"': '"',
    "\\": "\\",
    "/": "/",
}


def parse_js_literal(text: str, pos: int = 0) -> tuple[Any, int]:
    """Parse one JavaScript literal starting at ``pos``; return it and its end.

    Objects with bare, quoted or numeric keys; arrays; single- or double-quoted
    strings; numbers; ``true``, ``false`` and ``null``; trailing commas. That is
    everything the prototype's data arrays use. A JSON5 library is the obvious
    alternative and cannot read ``draftText``, whose keys are numbers. Numeric
    keys come back as strings, as they would in JavaScript.
    """
    parser = _Parser(text, pos)
    return parser.value(), parser.pos


class _Parser:
    def __init__(self, text: str, pos: int) -> None:
        self.text = text
        self.pos = pos

    def fail(self, expected: str) -> JsLiteralError:
        line = self.text.count("\n", 0, self.pos) + 1
        found = self.text[self.pos : self.pos + 24] or "end of text"
        return JsLiteralError(f"line {line}: expected {expected}, found {found!r}")

    def peek(self) -> str:
        while self.pos < len(self.text) and self.text[self.pos].isspace():
            self.pos += 1
        return self.text[self.pos : self.pos + 1]

    def value(self) -> Any:
        char = self.peek()
        if char == "{":
            return self.object()
        if char == "[":
            return self.array()
        if char in ("'", '"'):
            return self.string()
        if number := _NUMBER.match(self.text, self.pos):
            self.pos = number.end()
            literal = number.group()
            return float(literal) if any(c in literal for c in ".eE") else int(literal)
        word = _IDENTIFIER.match(self.text, self.pos)
        if word and word.group() in _KEYWORDS:
            self.pos = word.end()
            return _KEYWORDS[word.group()]
        raise self.fail("a value")

    def object(self) -> dict[str, Any]:
        self.pos += 1
        result: dict[str, Any] = {}
        while self.peek() != "}":
            key = self.key()
            if key in result:
                raise self.fail(f"a key other than the duplicate {key!r}")
            if self.peek() != ":":
                raise self.fail("':'")
            self.pos += 1
            result[key] = self.value()
            if not self.separator("}"):
                break
        self.close("}")
        return result

    def array(self) -> list[Any]:
        self.pos += 1
        result: list[Any] = []
        while self.peek() != "]":
            result.append(self.value())
            if not self.separator("]"):
                break
        self.close("]")
        return result

    def key(self) -> str:
        char = self.peek()
        if char in ("'", '"'):
            return self.string()
        found = re.compile(r"\d+").match(self.text, self.pos) or _IDENTIFIER.match(
            self.text, self.pos
        )
        if found is None:
            raise self.fail("a key")
        self.pos = found.end()
        return found.group()

    def separator(self, close: str) -> bool:
        """Consume a comma and return True, or find ``close`` and return False."""
        char = self.peek()
        if char == ",":
            self.pos += 1
            return True
        if char == close:
            return False
        raise self.fail(f"',' or {close!r}")

    def close(self, char: str) -> None:
        if self.peek() != char:
            raise self.fail(repr(char))
        self.pos += 1

    def string(self) -> str:
        quote = self.text[self.pos]
        self.pos += 1
        out: list[str] = []
        while True:
            char = self.text[self.pos : self.pos + 1]
            if char in ("", "\n"):
                raise self.fail(f"a closing {quote}")
            if char == quote:
                self.pos += 1
                return "".join(out)
            if char != "\\":
                out.append(char)
                self.pos += 1
                continue
            escape = self.text[self.pos + 1 : self.pos + 2]
            if escape == "u":
                digits = self.text[self.pos + 2 : self.pos + 6]
                if not re.fullmatch(r"[0-9a-fA-F]{4}", digits):
                    raise self.fail("four hex digits after \\u")
                out.append(chr(int(digits, 16)))
                self.pos += 6
            elif escape in _ESCAPES:
                out.append(_ESCAPES[escape])
                self.pos += 2
            else:
                raise self.fail("a supported escape")


def js_const(script: str, name: str) -> Any:
    """The value of ``const <name> = <literal>`` in the prototype's script."""
    found = list(re.finditer(rf"\bconst\s+{re.escape(name)}\s*=\s*", script))
    if len(found) != 1:
        raise FixtureError(f"expected one `const {name} = ...`, found {len(found)}")
    value, _ = parse_js_literal(script, found[0].end())
    return value


# --------------------------------------------------------------------------
# Markup
# --------------------------------------------------------------------------


def _has_class(name: str) -> str:
    return f"contains(concat(' ', normalize-space(@class), ' '), ' {name} ')"


def _one(element: Any, xpath: str) -> Any:
    found = element.xpath(xpath)
    if len(found) != 1:
        raise FixtureError(f"expected one match for {xpath!r}, found {len(found)}")
    return found[0]


def _text(element: Any, xpath: str = ".") -> str:
    return " ".join(_one(element, xpath).text_content().split())


def _require_keys(record: Mapping[str, Any], keys: frozenset[str], what: str) -> None:
    if set(record) != keys:
        raise FixtureError(
            f"{what} has fields {sorted(record)}, expected {sorted(keys)}. "
            "Update the API schema and this script together."
        )


def _nullify(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: None if value in PLACEHOLDERS.get(key, ()) else value
        for key, value in record.items()
    }


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _label(mapping: Mapping[str, str], label: str, what: str) -> str:
    if label not in mapping:
        raise FixtureError(f"unrecognised {what} {label!r}; known: {sorted(mapping)}")
    return mapping[label]


def prototype_date(doc: Any) -> date:
    """The date the prototype's dashboard is set on."""
    shown = _text(doc, f"//div[{_has_class('today')}]")
    return datetime.strptime(shown, "%A, %d %B %Y").date()


def _last_checked(label: str, today: date) -> str | None:
    if label == "Not yet":
        return None
    if label == "Today":
        days = 0
    elif match := re.fullmatch(r"(\d+) days? ago", label):
        days = int(match.group(1))
    else:
        raise FixtureError(f"unrecognised last-checked label {label!r}")
    when = datetime.combine(today - timedelta(days=days), SCAN_TIME, tzinfo=AEST)
    return when.isoformat()


def extract_sources(doc: Any, today: date) -> list[dict[str, Any]]:
    table = _one(doc, '//table[@id="srcTable"]')
    columns = tuple(" ".join(th.text_content().split()) for th in table.xpath(".//th"))
    if columns != REGISTRY_COLUMNS:
        raise FixtureError(f"registry columns changed: {columns}")

    sources = []
    for row in table.xpath("./tbody/tr"):
        cells = row.xpath("./td")
        name = _text(cells[1], ".//b")
        topic = _text(cells[3])
        if topic != row.get("data-topic"):
            raise FixtureError(f"{name}: topic cell and data-topic disagree")
        sources.append(
            {
                "id": _slug(name),
                "name": name,
                "url": "https://" + _text(cells[1], ".//small"),
                "owner": _text(cells[2]),
                "topic": topic,
                "format": _text(cells[4]),
                "update_frequency": _text(cells[5]),
                "status": _label(SOURCE_STATUS, _text(cells[6]), "source status"),
                "last_checked": _last_checked(_text(cells[7]), today),
            }
        )
    return sources


def extract_summary(doc: Any) -> list[dict[str, Any]]:
    stats = doc.xpath(
        '//div[@id="view-dashboard"]//section[@aria-label="Summary"]'
        f"/div[{_has_class('stat')}]"
    )
    cards = []
    for stat in stats:
        shown = _text(stat, f".//div[{_has_class('ring')}]/b")
        if not (match := re.fullmatch(r"(\d+)%", shown)):
            raise FixtureError(f"summary card value {shown!r} is not a percentage")
        cards.append(
            {
                "label": _text(stat, f".//div[{_has_class('t')}]"),
                "percent": int(match.group(1)),
                "detail": _text(stat, f".//div[{_has_class('d')}]"),
            }
        )
    return cards


def extract_activity(doc: Any) -> list[dict[str, Any]]:
    card = _one(
        doc,
        '//div[@id="view-dashboard"]'
        '//section[.//h3[normalize-space()="Recent activity"]]',
    )
    items = []
    for item in card.xpath(".//ul/li"):
        icon = _one(item, f"./span[{_has_class('ico')}]").get("class").split()
        items.append(
            {
                "ico": next(c for c in icon if c != "ico"),
                "title": _text(item, f"./span[{_has_class('txt')}]/b"),
                "detail": _text(item, f"./span[{_has_class('txt')}]/small"),
                "when": _text(item, f"./span[{_has_class('when')}]"),
            }
        )
    return items


def _settings_rows(doc: Any, section: str) -> dict[str, Any]:
    rows = {}
    for row in doc.xpath(f'//section[@id="{section}"]/div[@class="row"]'):
        labels = row.xpath('./div[@class="l"]/b')
        if labels:
            # Direct text only: a policy row's <b> also holds a "Policy" badge.
            rows[(labels[0].text or "").strip()] = row
    return rows


def _select(row: Any, mapping: Mapping[str, Any] | None = None) -> Any:
    """The selected option (the first, as a browser would, if none is marked)."""
    options = row.xpath(".//select/option")
    labels = [" ".join(o.text_content().split()) for o in options]
    marked = [
        lbl for o, lbl in zip(options, labels, strict=True) if "selected" in o.attrib
    ]
    chosen = marked[0] if marked else labels[0]
    if mapping is None:
        return chosen
    for label in labels:
        _label(mapping, label, "settings option")
    return mapping[chosen]


def _switch(row: Any, *, locked: bool = False) -> bool:
    switch = _one(row, './/button[@role="switch"]')
    if locked and "disabled" not in switch.attrib:
        raise FixtureError("a policy switch is no longer locked in the prototype")
    return bool(switch.get("aria-checked") == "true")


def _pressed(row: Any) -> list[str]:
    chips = row.xpath(f'.//button[{_has_class("chip-f")}][@aria-pressed="true"]')
    return [" ".join(chip.text_content().split()) for chip in chips]


def _flag_below(label: str) -> int | None:
    if label == "Don't flag":
        return None
    if match := re.fullmatch(r"(\d+)% confidence", label):
        return int(match.group(1))
    raise FixtureError(f"unrecognised confidence threshold {label!r}")


def extract_settings(doc: Any) -> dict[str, Any]:
    scan = _settings_rows(doc, "s-scan")
    agent = _settings_rows(doc, "s-ai")
    thresholds = {
        label: _flag_below(label)
        for label in (
            " ".join(o.text_content().split())
            for o in agent["Flag findings below"].xpath(".//select/option")
        )
    }
    return {
        "scan": {
            "frequency": _select(scan["Frequency"], SCAN_FREQUENCY),
            "run_on": _select(scan["Run on"]),
            "topics": _pressed(scan["Topics included"]),
            "retry_blocked": _switch(scan["Retry blocked sources"]),
            "skip_unchanged": _switch(scan["Skip unchanged sources"]),
            "paused": _switch(scan["Pause scheduled scans"]),
        },
        "thresholds": {
            "flag_below": _select(agent["Flag findings below"], thresholds),
            "change_sensitivity": _select(
                agent["Change sensitivity"], CHANGE_SENSITIVITY
            ),
        },
        "policies": {
            "only_cite_approved": _switch(
                agent["Only cite approved sources"], locked=True
            ),
            "staff_sign_off": _switch(
                agent["Staff sign-off before publishing"], locked=True
            ),
        },
    }


def extract_meta(doc: Any, script: str, today: date) -> dict[str, Any]:
    """Values the mock needs that the prototype states once: who, when, what."""
    profile = _settings_rows(doc, "s-profile")
    titles = re.findall(r"<h4>([^<]+)</h4>", script)
    if len(titles) != 1:
        raise FixtureError(f"expected one report title in the script, found {titles}")
    return {
        "today": today.isoformat(),
        "reviewer": _one(profile["Display name"], ".//input").get("value"),
        "report_title": titles[0],
    }


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def build_fixtures(html_text: str) -> dict[str, Any]:
    """Every fixture file's content, keyed by file name."""
    doc = lxml_html.fromstring(html_text)
    script = "\n".join(s.text or "" for s in doc.xpath("//script[not(@src)]"))
    today = prototype_date(doc)

    claims = js_const(script, "claims")
    for claim in claims:
        _require_keys(claim, CLAIM_KEYS, f"claim {claim.get('id')}")

    draft = []
    for claim_id, text in js_const(script, "draftText").items():
        _require_keys(text, DRAFT_KEYS, f"draftText[{claim_id}]")
        draft.append({"claim_id": int(claim_id), **_nullify(text)})

    outcomes = js_const(script, "outcomes")
    for outcome in outcomes:
        _require_keys(outcome, OUTCOME_KEYS, "scan outcome")

    notifications = js_const(script, "notifs")
    for notification in notifications:
        _require_keys(notification, NOTIFICATION_KEYS, "notification")

    return {
        "sources.json": extract_sources(doc, today),
        "claims.json": [_nullify(claim) for claim in claims],
        "draft.json": sorted(draft, key=lambda item: item["claim_id"]),
        "outcomes.json": [
            {
                "state": _label(OUTCOME_STATE, o["st"], "scan outcome"),
                "detail": o["d"],
            }
            for o in outcomes
        ],
        "notifications.json": notifications,
        "summary.json": extract_summary(doc),
        "activity.json": extract_activity(doc),
        "settings.json": extract_settings(doc),
        "meta.json": extract_meta(doc, script, today),
    }


def render(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def stale_files(fixtures: Mapping[str, Any], out: Path) -> list[str]:
    """Fixture files that differ from what the prototype now produces."""
    stale = [
        name
        for name, value in fixtures.items()
        if not (out / name).exists()
        or (out / name).read_text(encoding="utf-8") != render(value)
    ]
    orphans: Iterable[str] = (
        p.name for p in sorted(out.glob("*.json")) if p.name not in fixtures
    )
    return stale + [f"{name} (no longer produced)" for name in orphans]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--html", type=Path, default=DEFAULT_HTML)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if the committed fixtures differ; write nothing",
    )
    args = parser.parse_args(argv)

    fixtures = build_fixtures(args.html.read_text(encoding="utf-8"))
    if args.check:
        stale = stale_files(fixtures, args.out)
        for name in stale:
            print(f"stale: {name}")
        if stale:
            print("Regenerate: uv run python scripts/extract_prototype_fixtures.py")
            return 1
        print(f"{len(fixtures)} fixture file(s) match the prototype")
        return 0

    args.out.mkdir(parents=True, exist_ok=True)
    for name, value in fixtures.items():
        (args.out / name).write_text(render(value), encoding="utf-8", newline="\n")
        print(f"wrote {args.out / name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
