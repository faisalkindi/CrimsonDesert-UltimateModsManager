"""Open CDUMM's GitHub issue forms pre-filled, with the report on the clipboard.

A link can open one of the repository's issue forms and fill in its title and
text fields: ``template`` picks the form, ``title`` fills the title, and a text
field takes the value of the query parameter named after its ``id`` in
``.github/ISSUE_TEMPLATE``. The report itself can't go in the link: URL-encoded
it runs to 11-22 K characters, well past what GitHub accepts (see
``MAX_URL_LENGTH``). So the link carries only the form, a title and the
reporter's own description. The caller puts the scrubbed report on the
clipboard, and the form's required report box stays empty for the reporter to
paste it into. Nothing is posted: the reporter reviews the form and submits it.

Dropdowns are not filled in. GitHub ignored ``problem-type``, ``install-source``
and ``when`` given either as option text or as an index (tested 2026-11-07),
and its docs only promise text fields. ``labels`` is never passed: it needs
triage rights, and without them the link is a 404. The forms add their own
labels.

Pure Python, no Qt, so the tests need neither a display nor the game.
"""
from __future__ import annotations

import re
from urllib.parse import quote, urlencode

REPO_URL = "https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager"
NEW_ISSUE_URL = f"{REPO_URL}/issues/new"
CHOOSER_URL = f"{NEW_ISSUE_URL}/choose"

BUG_TEMPLATE = "bug_report.yml"
CRASH_TEMPLATE = "crash_report.yml"

# Order of the severity combo on the Bug Report page and in BugReportDialog.
# Both build their combo from this tuple, so an index means the same severity
# in both places and here.
SEVERITY_KEYS = ("bug.crash", "bug.wrong", "bug.visual", "bug.other")
SEVERITY_CRASH = SEVERITY_KEYS.index("bug.crash")

# The form field, by its ``id:``, that gets the reporter's description...
DESCRIPTION_FIELD = {BUG_TEMPLATE: "what-happened", CRASH_TEMPLATE: "context"}
# ...and the required report box, which the link leaves empty on purpose so
# the reporter pastes the report from the clipboard. Never used to build the
# link; the tests check both maps against the form files.
REPORT_FIELD = {BUG_TEMPLATE: "bug-report", CRASH_TEMPLATE: "report"}

# Longest link this module builds. Measured on 2026-11-07 against this repo's
# issues/new page:
#   - signed in: 200 up to 8,187 characters, 414 from 8,194;
#   - signed out, GitHub redirects to /login?return_to=<the link, encoded
#     again>. It answers 500 instead from 6,862 characters, and it drops
#     return_to, so the form is lost after signing in, once that Location
#     passes about 7,710 characters (kept at 7,710, dropped at 7,725).
# Encoding again leaves letters, digits and "-._~" alone and turns each "%XX"
# into "%25XX". Spaces are written as "%20", not "+", so no character grows by
# more than that 5/3 ("+" would become "%2B", 3x). A 4,000-character link
# therefore redirects with a Location of at most about 6,650, under all three
# limits whatever the text. At the cap the description keeps about 2,600
# characters of English, 600 of Cyrillic or 340 of Chinese.
MAX_URL_LENGTH = 4000

TITLE_MAX_CHARS = 80

TRUNCATION_NOTE = (
    "\n\n(Cut to fit the link. The full text is under USER DESCRIPTION "
    "in the report.)")

_USER_PATH_RE = re.compile(
    r"([A-Za-z]:[\\/]+Users[\\/]+)[^\\/\r\n\"'<>|:*?]+",
    re.IGNORECASE,
)


def scrub_windows_paths(text: str) -> str:
    """Replace the name in `C:\\Users\\<name>` with USER to protect privacy.

    Preserves the slash style that was used (backslash, forward-slash, or the
    doubled backslashes Python error text quotes a path with), the drive letter
    and the rest of the path. Case-insensitive on the Users/drive portion.

    The name ends at the next slash, or at any other character a Windows name
    can't hold. A path that stops at the name, like `USERPROFILE=C:\\Users\\me`,
    has no closing slash, so text after it on that line can go with the name.
    Losing that beats keeping the name.
    """
    if not text:
        return text
    return _USER_PATH_RE.sub(lambda m: f"{m.group(1)}USER", text)


def template_for_severity(severity_index: int) -> str:
    """The issue form for a severity combo index: a crash gets the crash form."""
    return CRASH_TEMPLATE if severity_index == SEVERITY_CRASH else BUG_TEMPLATE


def issue_title(description: str, limit: int = TITLE_MAX_CHARS) -> str:
    """A title from the description's first non-blank line, or "" if it has none.

    No placeholder title: GitHub won't submit an issue without a title, so an
    empty one makes the reporter write a real one.
    """
    line = next(
        (" ".join(ln.split()) for ln in description.splitlines() if ln.strip()), "")
    if len(line) <= limit:
        return line
    cut = line[: limit - 1]
    space = cut.rfind(" ")
    if space >= limit // 2:
        cut = cut[:space]
    return cut.rstrip(" .,;:-") + "\u2026"


def build_issue_url(severity_index: int, description: str,
                    max_length: int = MAX_URL_LENGTH) -> str:
    """The link that opens the right form with its title and description filled.

    The description is scrubbed like the report. When the whole link would
    pass ``max_length``, the description is cut and says so; the full text is
    still in the report on the clipboard. The report box is never filled.
    """
    template = template_for_severity(severity_index)
    field = DESCRIPTION_FIELD[template]
    text = scrub_windows_paths(
        description.replace("\r\n", "\n").replace("\r", "\n").strip())
    title = issue_title(text)

    def link(body: str, with_title: bool = True) -> str:
        params = [("template", template)]
        if title and with_title:
            params.append(("title", title))
        if body:
            params.append((field, body))
        return f"{NEW_ISSUE_URL}?{urlencode(params, quote_via=quote)}"

    url = link(text)
    if len(url) <= max_length:
        return url

    # The longest start of the description that still fits with the note. A
    # longer start never encodes shorter, so a binary search finds it.
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(link(text[:mid].rstrip() + TRUNCATION_NOTE)) <= max_length:
            lo = mid
        else:
            hi = mid - 1
    if lo:
        return link(text[:lo].rstrip() + TRUNCATION_NOTE)
    # Not even a few characters fit, which only a tiny max_length allows: fall
    # back to the title alone, then to the bare form.
    without_body = link("")
    if len(without_body) <= max_length:
        return without_body
    return link("", with_title=False)
