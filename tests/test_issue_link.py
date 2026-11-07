"""The link behind Report on GitHub: right form, right fields, short enough.

cdumm.gui.issue_link is pure Python, so none of this needs Qt, a display or
the game. The URL limits it is held to were measured against github.com on
2026-11-07 (see MAX_URL_LENGTH in the module):

- signed in, issues/new answers 200 up to 8,187 characters and 414 above;
- signed out, it answers 500 from 6,862 characters, and below that redirects
  to /login?return_to=<link>, dropping return_to once that Location passes
  7,710 characters. The Location length is exactly
  len(SIGN_IN_PREFIX) + len(quote(link, safe="")) in all six cases measured.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

import pytest

from cdumm.gui.issue_link import (
    BUG_TEMPLATE,
    CRASH_TEMPLATE,
    DESCRIPTION_FIELD,
    MAX_URL_LENGTH,
    NEW_ISSUE_URL,
    REPORT_FIELD,
    SEVERITY_CRASH,
    SEVERITY_KEYS,
    TITLE_MAX_CHARS,
    TRUNCATION_NOTE,
    build_issue_url,
    issue_title,
    scrub_windows_paths,
    template_for_severity,
)

REPO = Path(__file__).resolve().parents[1]
FORMS = REPO / ".github" / "ISSUE_TEMPLATE"

SIGN_IN_PREFIX = "https://github.com/login?return_to="
SIGNED_IN_LIMIT = 8187       # longest link that loaded signed in
SIGNED_OUT_LIMIT = 6856      # longest that still redirected signed out (500 at 6,862)
RETURN_TO_LIMIT = 7710       # longest Location that kept return_to

SEVERITY_BUG = SEVERITY_KEYS.index("bug.wrong")

# Dropdown ids in the two forms. GitHub ignored them in a link, as option text
# and as an index, so the link must not carry them.
DROPDOWN_IDS = {"problem-type", "install-source", "when"}


def _params(url: str) -> dict[str, str]:
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == NEW_ISSUE_URL
    pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
    keys = [k for k, _ in pairs]
    assert len(keys) == len(set(keys)), f"repeated parameter in {keys}"
    return dict(pairs)


def _sign_in_location_length(url: str) -> int:
    return len(SIGN_IN_PREFIX) + len(quote(url, safe=""))


# ── Which form ───────────────────────────────────────────────────────────


def test_crash_severity_opens_the_crash_form():
    assert SEVERITY_KEYS[SEVERITY_CRASH] == "bug.crash"
    assert template_for_severity(SEVERITY_CRASH) == CRASH_TEMPLATE
    assert _params(build_issue_url(SEVERITY_CRASH, "it froze"))["template"] == CRASH_TEMPLATE


@pytest.mark.parametrize("key", ["bug.wrong", "bug.visual", "bug.other"])
def test_every_other_severity_opens_the_bug_form(key):
    index = SEVERITY_KEYS.index(key)
    assert template_for_severity(index) == BUG_TEMPLATE
    assert _params(build_issue_url(index, "x"))["template"] == BUG_TEMPLATE


def test_no_selection_opens_the_bug_form():
    # A Qt combo reports -1 when nothing is selected.
    assert template_for_severity(-1) == BUG_TEMPLATE


def test_severity_keys_are_the_ones_the_combo_translates():
    import json
    en = json.loads((REPO / "src" / "cdumm" / "translations" / "en.json")
                    .read_text(encoding="utf-8"))
    assert all(k in en for k in SEVERITY_KEYS)
    assert len(set(SEVERITY_KEYS)) == 4


# ── What the link carries ────────────────────────────────────────────────


def test_bug_link_fills_title_and_what_happened_only():
    p = _params(build_issue_url(SEVERITY_BUG, "Apply fails\nSecond line"))
    assert p == {"template": BUG_TEMPLATE, "title": "Apply fails",
                 "what-happened": "Apply fails\nSecond line"}


def test_crash_link_fills_title_and_context_only():
    p = _params(build_issue_url(SEVERITY_CRASH, "Clicked Apply, it froze"))
    assert p == {"template": CRASH_TEMPLATE, "title": "Clicked Apply, it froze",
                 "context": "Clicked Apply, it froze"}


@pytest.mark.parametrize("severity", range(len(SEVERITY_KEYS)))
@pytest.mark.parametrize(
    "description", ["", "short", "x" * 20000, "日本語 " * 2000],
    # Short ids: pytest puts the id in an environment variable, and Windows
    # caps those at 32,767 characters.
    ids=["empty", "short", "long-ascii", "long-cjk"])
def test_never_labels_dropdowns_or_the_report(severity, description):
    p = _params(build_issue_url(severity, description))
    assert "labels" not in p, "labels= 404s for anyone without triage rights"
    assert not DROPDOWN_IDS & p.keys()
    assert not set(REPORT_FIELD.values()) & p.keys(), (
        "the report box must stay empty so the reporter pastes the report")
    template = p["template"]
    assert set(p) <= {"template", "title", DESCRIPTION_FIELD[template]}


def test_empty_description_gives_the_bare_form():
    for text in ("", "   \n\t \r\n "):
        assert _params(build_issue_url(SEVERITY_BUG, text)) == {"template": BUG_TEMPLATE}


def test_encoding_round_trips_every_character():
    text = ("All ASCII: " + "".join(chr(c) for c in range(32, 127)) + "\n"
            "50% done #3 a+b=c&d?e/f \\ tab\tend\n"
            "Unicode: — → é ü 日本語 Ру́сский 🙂\u00a0nbsp")
    url = build_issue_url(SEVERITY_BUG, text)
    assert url.isascii()
    assert " " not in url and "+" not in url, "spaces go as %20 (see MAX_URL_LENGTH)"
    assert _params(url)["what-happened"] == text


def test_line_endings_become_newlines():
    p = _params(build_issue_url(SEVERITY_BUG, "one\r\ntwo\rthree\nfour"))
    assert p["what-happened"] == "one\ntwo\nthree\nfour"


# ── Title ────────────────────────────────────────────────────────────────


def test_title_is_the_first_non_blank_line_with_spaces_collapsed():
    assert issue_title("\n\n   Mods   don't\tapply \nmore detail") == "Mods don't apply"


def test_long_title_is_cut_at_a_word_with_an_ellipsis():
    line = "word " * 40
    title = issue_title(line)
    assert len(title) <= TITLE_MAX_CHARS
    assert title.endswith("\u2026")
    assert line.startswith(title[:-1])
    assert not title[:-1].endswith(" ")


def test_long_title_without_spaces_is_still_capped():
    title = issue_title("x" * 500)
    assert len(title) == TITLE_MAX_CHARS and title.endswith("\u2026")


def test_no_description_no_title():
    assert issue_title("") == ""
    assert issue_title(" \n\t\n") == ""


# ── Length ───────────────────────────────────────────────────────────────

# Inputs chosen to make the encoded link, and the sign-in redirect's second
# encoding, as long as possible: every kind of character the link can hold.
LONG_INPUTS = {
    "ascii": "The game crashes when I apply this mod. " * 400,
    # One run of spaces: the cut strips it, keeping just "a".
    "space-run": "a" + " " * 12000 + "b",
    # Spaces between words survive the cut. Written as "+", each would grow
    # to "%2B" in the sign-in redirect (3x) and push it past return_to's limit.
    "spaced-words": ("x" + " " * 50) * 300,
    "aligned-log": "    at cdumm.engine.apply   (line   42)   \n" * 400,
    "newline-run": "a" + "\n" * 12000 + "b",
    "blank-lines": ("x" + "\n" * 50) * 300,
    "percent-and-plus": "%+&=#?/" * 3000,
    "cyrillic": "Игра вылетает при применении мода. " * 400,
    "cjk": "应用模组后游戏崩溃。" * 1500,
    "emoji": "🙂💥" * 4000,
    "mixed-with-path": ("C:\\Users\\Someone\\AppData\\Local\\cdumm\\cdumm.log "
                        "→ ошибка 日本 🙂 50% a+b\n") * 300,
}


@pytest.mark.parametrize("severity", [SEVERITY_BUG, SEVERITY_CRASH])
@pytest.mark.parametrize("name", sorted(LONG_INPUTS))
def test_long_descriptions_are_cut_to_fit_every_measured_limit(severity, name):
    text = LONG_INPUTS[name]
    url = build_issue_url(severity, text)
    assert len(url) <= MAX_URL_LENGTH
    assert len(url) <= SIGNED_OUT_LIMIT <= SIGNED_IN_LIMIT
    assert _sign_in_location_length(url) <= RETURN_TO_LIMIT, (
        "signed-out reporters would lose the form at the sign-in redirect")

    p = _params(url)
    body = p[DESCRIPTION_FIELD[p["template"]]]
    assert body.endswith(TRUNCATION_NOTE)
    kept = body[: -len(TRUNCATION_NOTE)]
    expected = scrub_windows_paths(text.strip())
    assert kept and expected.startswith(kept), "the cut keeps the start, intact"


@pytest.mark.parametrize("name", sorted(LONG_INPUTS))
def test_the_cut_keeps_as_much_as_fits(name):
    """The next longer start of the description would not have fitted."""
    text = scrub_windows_paths(LONG_INPUTS[name].strip())
    p = _params(build_issue_url(SEVERITY_BUG, LONG_INPUTS[name]))
    kept = p["what-happened"][: -len(TRUNCATION_NOTE)]
    # The next start that differs once trailing whitespace is stripped, as
    # the cut strips it.
    m = len(kept) + 1
    while m <= len(text) and text[:m].rstrip() == kept:
        m += 1
    assert m <= len(text), "nothing was left out, so nothing should be cut"
    params = [("template", BUG_TEMPLATE), ("title", p["title"]),
              ("what-happened", text[:m].rstrip() + TRUNCATION_NOTE)]
    rejected = f"{NEW_ISSUE_URL}?{urlencode(params, quote_via=quote)}"
    assert len(rejected) > MAX_URL_LENGTH


def test_a_description_that_fits_is_left_whole():
    text = "Mod X won't apply. " * 50
    url = build_issue_url(SEVERITY_BUG, text)
    assert len(url) <= MAX_URL_LENGTH
    assert _params(url)["what-happened"] == text.strip()
    assert TRUNCATION_NOTE.strip() not in url


def test_a_tiny_cap_still_names_the_form():
    url = build_issue_url(SEVERITY_CRASH, "word " * 100, max_length=150)
    assert _params(url)["template"] == CRASH_TEMPLATE
    assert len(url) <= 150


def test_full_reports_would_not_fit_which_is_why_they_go_on_the_clipboard():
    """A report-sized description (11-22 K encoded in practice) gets cut."""
    report_sized = "  #  12 [ON ] Some Mod Name v1.2  nexus=123  deltas=4 (1.2 MB)\n" * 250
    url = build_issue_url(SEVERITY_BUG, report_sized)
    assert len(url) <= MAX_URL_LENGTH
    assert _params(url)["what-happened"].endswith(TRUNCATION_NOTE)


# ── Privacy ──────────────────────────────────────────────────────────────


def test_windows_user_name_is_scrubbed_from_title_and_description():
    text = r"Crash reading C:\Users\Finlay Smith\AppData\Local\cdumm\x.log and D:/users/bob/x"
    p = _params(build_issue_url(SEVERITY_BUG, text))
    for value in (p["title"], p["what-happened"]):
        assert "Finlay" not in value and "bob" not in value
        assert r"C:\Users\USER\AppData" in value
        assert "D:/users/USER/x" in value


def test_scrub_keeps_slash_style_drive_and_rest_of_path():
    assert scrub_windows_paths(r"E:\Users\me\Games\x") == r"E:\Users\USER\Games\x"
    assert scrub_windows_paths("c:/USERS/me/x") == "c:/USERS/USER/x"
    assert scrub_windows_paths(r"C:\Program Files\x") == r"C:\Program Files\x"
    assert scrub_windows_paths("") == ""


def test_scrub_is_still_importable_from_bug_report():
    """bug_report_page imports it from there; it moved to stay Qt-free."""
    pytest.importorskip("qfluentwidgets")
    from cdumm.gui import bug_report, issue_link
    assert bug_report.scrub_windows_paths is issue_link.scrub_windows_paths


# ── The forms the link points at ─────────────────────────────────────────


def _form_fields(path: Path) -> dict[str, tuple[str, bool]]:
    """{id: (type, required)} for an issue form's body elements.

    A small reader for the forms' own layout (two-space list items under
    `body:`), so the test needs no YAML library, which isn't a dependency.
    """
    text = path.read_text(encoding="utf-8")
    body = text.split("\nbody:", 1)[1]
    fields: dict[str, tuple[str, bool]] = {}
    for chunk in re.split(r"\n  - type: ", body)[1:]:
        kind = chunk.split(None, 1)[0]
        m = re.search(r"^    id: ([\w-]+)\s*$", chunk, re.MULTILINE)
        if not m:
            continue
        required = re.search(
            r"^    validations:\s*\n      required: true\s*$", chunk, re.MULTILINE)
        fields[m.group(1)] = (kind, bool(required))
    return fields


@pytest.mark.parametrize("template", [BUG_TEMPLATE, CRASH_TEMPLATE])
def test_link_fields_exist_in_the_issue_forms(template):
    fields = _form_fields(FORMS / template)
    assert fields, f"read no fields from {template}"
    desc = DESCRIPTION_FIELD[template]
    report = REPORT_FIELD[template]
    assert fields.get(desc, ("missing",))[0] == "textarea", (
        f"{template} has no textarea id {desc!r}; the description would be lost")
    assert fields.get(report) == ("textarea", True), (
        f"{template}'s report box {report!r} must be a required textarea, so "
        "the reporter can't submit without pasting the report")


def test_dropdown_ids_are_real_dropdowns():
    """Keeps the no-dropdowns check above from being vacuous."""
    found = {fid for t in (BUG_TEMPLATE, CRASH_TEMPLATE)
             for fid, (kind, _) in _form_fields(FORMS / t).items() if kind == "dropdown"}
    assert found >= DROPDOWN_IDS
