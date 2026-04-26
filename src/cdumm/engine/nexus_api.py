"""NexusMods API integration for CDUMM.

Checks if installed mods have newer versions on NexusMods.
Uses the NexusMods v1 REST API with personal API key for testing.

Rules (from NexusMods Acceptable Use Policy):
- Personal API key: testing/personal use ONLY, not for public release
- Required headers: Application-Name, Application-Version
- Rate limits: 500 requests/hour, 20,000/day
- No mass scraping, no rehosting data
- Open source preferred

API docs: https://app.swaggerhub.com/apis-docs/NexusMods/nexus-mods_public_api_params_in_form_data/1.0
AUP: https://help.nexusmods.com/article/114-api-acceptable-use-policy
"""

import json
import logging
import re
import urllib.request
from dataclasses import dataclass

from cdumm import __version__

logger = logging.getLogger(__name__)

BASE_URL = "https://api.nexusmods.com/v1"
GAME_DOMAIN = "crimsondesert"


@dataclass
class NexusModInfo:
    mod_id: int
    name: str
    version: str
    author: str
    updated_timestamp: int
    url: str


@dataclass
class NexusFileInfo:
    file_id: int
    name: str
    version: str
    uploaded_timestamp: int
    file_name: str
    # 1=MAIN, 2=PATCH, 3=OPTIONAL, 4=OLD_VERSION, 6=DELETED, 7=ARCHIVED
    # Used to skip OLD_VERSION/DELETED files when picking update candidates.
    category_id: int = 0


@dataclass
class NexusFileUpdate:
    """Author-declared 'this file is a newer version of X' link.

    Per Nexus's IModFiles contract, ``file_updates`` chains old file
    IDs to their successors. Walking this chain is the canonical way
    to detect updates because it survives file renames, variant
    splits, and reorganisations of a mod page.
    """
    old_file_id: int
    new_file_id: int
    old_file_name: str = ""
    new_file_name: str = ""
    uploaded_timestamp: int = 0


@dataclass
class ModUpdateStatus:
    mod_id: int
    local_name: str
    local_version: str
    latest_version: str
    has_update: bool
    mod_url: str
    # The file_id for the highest-version file we found. Used by the
    # premium-direct-download flow so the GUI can call download_link
    # without round-tripping through the browser.
    latest_file_id: int = 0
    # True when the user's stored nexus_real_file_id is no longer
    # present in the Nexus file list (author deleted it, or mod was
    # taken down). Distinct from "outdated" — there's no successor
    # to upgrade to. UI can render a different badge.
    file_deleted_on_nexus: bool = False


class NexusPremiumRequired(Exception):
    """Raised when the API rejects a download_link call with HTTP 403
    "not permitted without visiting nexusmods.com — this is for premium
    users only." CDUMM's GUI catches this and falls back to opening the
    mod's file-tab page in the user's browser so the user can click
    "Mod Manager Download" and route the file back to CDUMM via the
    registered ``nxm://`` handler.
    """


class NexusAuthError(Exception):
    """Raised when Nexus rejects our API key (HTTP 401 / invalid).
    Distinct from generic transport errors so the GUI can surface
    "re-enter your API key" instead of a bland "update check failed"
    warning that users tend to ignore.
    """


class NexusRateLimited(Exception):
    """Raised when Nexus returns HTTP 429. Carries the epoch at
    which the hourly quota resets so the caller can back off
    intelligently instead of retrying every 30 minutes.
    """

    def __init__(self, message: str = "", reset_at: int = 0) -> None:
        super().__init__(message or "Nexus rate limit exceeded")
        self.reset_at = int(reset_at)


# Module-level rate-limit snapshot — updated on every API response so
# the application can surface "you're about to hit the daily cap"
# warnings, and so the log carries a paper trail for the AUP review.
# See https://help.nexusmods.com/article/105 for header definitions.
_last_rate_limit: dict[str, str] = {}


def _log_rate_limits(headers) -> None:
    """Capture Nexus rate-limit headers into the module-level snapshot.

    Nexus returns four headers on every response:

    - ``X-RL-Daily-Remaining``
    - ``X-RL-Daily-Reset``
    - ``X-RL-Hourly-Remaining``
    - ``X-RL-Hourly-Reset``

    We log them at DEBUG level and retain the latest values in
    ``_last_rate_limit`` so GUI surfaces can read them without making
    an extra request. ``headers`` is a ``Message`` / ``HTTPMessage`` /
    ``dict`` — anything with ``.get()`` works; ``None`` is a no-op.
    """
    if headers is None:
        return
    snapshot = {}
    for key in ("X-RL-Daily-Remaining", "X-RL-Daily-Reset",
                "X-RL-Hourly-Remaining", "X-RL-Hourly-Reset"):
        try:
            val = headers.get(key) or headers.get(key.lower())
        except Exception:
            val = None
        if val is not None:
            snapshot[key] = str(val)
    if snapshot:
        _last_rate_limit.update(snapshot)
        logger.debug("nexus rate limits: %s", snapshot)


def get_rate_limit_snapshot() -> dict[str, str]:
    """Return a copy of the most recent rate-limit header values."""
    return dict(_last_rate_limit)


def _api_request(endpoint: str, api_key: str) -> dict | list:
    """Make an authenticated request to the NexusMods API.

    Verified working headers (tested against live API):
    - apikey: user's personal key (lowercase per NexusMods convention)
    - Application-Name / Application-Version: required per AUP
    - SSL context required for PyInstaller builds

    Raises :class:`NexusPremiumRequired` on HTTP 403 from endpoints that
    gate behind a premium membership (currently only download_link).
    """
    import ssl
    url = f"{BASE_URL}{endpoint}"
    headers = {
        "apikey": api_key,
        "User-Agent": f"CDUMM/{__version__}",
        "Application-Name": "CDUMM",
        "Application-Version": __version__,
    }
    req = urllib.request.Request(url, headers=headers)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            # Log Nexus's rate-limit headers so we have visibility for
            # the AUP review — Nexus returns daily/hourly remaining
            # quota on every response (see help.nexusmods.com/article/105).
            _log_rate_limits(resp.headers)
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        _log_rate_limits(getattr(e, "headers", None))
        if e.code == 401:
            # Invalid / revoked / expired API key. Surface a dedicated
            # exception so the GUI can prompt the user to re-enter it
            # instead of treating this as a transient failure to retry
            # silently forever.
            try:
                body = json.loads(e.read().decode("utf-8", errors="replace"))
                msg = body.get("message", "")
            except Exception:
                msg = ""
            logger.warning("nexus auth rejected (401): %s", msg or "no body")
            raise NexusAuthError(msg or "Nexus rejected the API key (401)")
        if e.code == 429:
            # Rate limited. Read the Hourly-Reset header so the caller
            # can back off until the window rolls over (typically a
            # few minutes) instead of retrying every 30 min and
            # burning further quota. Bug #23.
            reset_at = 0
            try:
                headers = getattr(e, "headers", None)
                if headers is not None:
                    raw = (headers.get("X-RL-Hourly-Reset")
                           or headers.get("x-rl-hourly-reset"))
                    if raw:
                        reset_at = int(raw)
            except Exception:
                reset_at = 0
            logger.warning("nexus rate limit hit (429), reset_at=%d",
                           reset_at)
            raise NexusRateLimited(
                "Nexus rate limit exceeded", reset_at=reset_at)
        if e.code in (403, 404, 410) and "download_link" in endpoint:
            # 403 → premium required (free user without website handover)
            # 404 → file_id no longer valid (taken down, or stale cache)
            # 410 → resource gone (file removed by author)
            # All three should fall back to opening the mod's Files
            # tab so the user can pick a still-available file. Codex
            # adversarial review M4: 404/410 used to bubble up as a
            # raw HTTP error toast with no recovery path.
            try:
                body = json.loads(e.read().decode("utf-8", errors="replace"))
                msg = body.get("message", "")
            except Exception:
                msg = ""
            logger.info(
                "download_link %d (browser fallback): %s", e.code, msg)
            raise NexusPremiumRequired(
                msg or f"Download not available (HTTP {e.code})")
        raise


def get_download_link(mod_id: int, file_id: int, api_key: str,
                      nxm_key: str | None = None,
                      nxm_expires: int | None = None) -> str | None:
    """Fetch a signed CDN URL for a specific mod file.

    Two paths:

    * **Premium user** — call with only ``mod_id``, ``file_id``,
      ``api_key``. Nexus responds with the list of CDN mirrors and
      this returns the first URI.
    * **Free user via nxm:// handover** — the user clicked "Mod Manager
      Download" on the website, Windows fired an ``nxm://`` URL at
      CDUMM, and that URL carried ``key`` + ``expires`` query params.
      Pass those through and Nexus honors the request even for free
      accounts because the user went through the website's gate.

    Raises :class:`NexusPremiumRequired` when the user is free and no
    ``nxm_key`` was supplied — caller should open the browser fallback.
    """
    endpoint = f"/games/{GAME_DOMAIN}/mods/{mod_id}/files/{file_id}/download_link.json"
    if nxm_key is not None and nxm_expires is not None:
        endpoint += f"?key={urllib.parse.quote(nxm_key)}&expires={nxm_expires}"
    data = _api_request(endpoint, api_key)
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            return first.get("URI")
    logger.warning("download_link: unexpected response shape: %r",
                   type(data).__name__)
    return None


def mod_page_files_url(mod_id: int) -> str:
    """Browser fallback URL: the mod's Files tab where the user can
    click "Mod Manager Download" to trigger the nxm:// flow."""
    return f"https://www.nexusmods.com/{GAME_DOMAIN}/mods/{mod_id}?tab=files"


def validate_api_key(api_key: str) -> dict | None:
    """Validate a NexusMods API key. Returns user info or None."""
    try:
        return _api_request("/users/validate.json", api_key)
    except Exception as e:
        logger.warning("API key validation failed: %s", e)
        return None


def get_mod_info(mod_id: int, api_key: str) -> NexusModInfo | None:
    """Get info about a specific mod."""
    try:
        data = _api_request(
            f"/games/{GAME_DOMAIN}/mods/{mod_id}.json", api_key)
        return NexusModInfo(
            mod_id=data.get("mod_id", mod_id),
            name=data.get("name", mod_id),
            version=data.get("version", ""),
            author=data.get("author", ""),
            updated_timestamp=data.get("updated_timestamp", 0),
            url=f"https://www.nexusmods.com/{GAME_DOMAIN}/mods/{mod_id}",
        )
    except (NexusAuthError, NexusRateLimited):
        # Let the caller react (prompt re-entry / back off the
        # schedule); don't bury these alongside generic transport
        # failures.
        raise
    except Exception as e:
        logger.warning("Failed to get mod info for %d: %s", mod_id, e)
        return None


def get_mod_files(mod_id: int, api_key: str
                  ) -> tuple[list[NexusFileInfo], list[NexusFileUpdate]] | None:
    """Get all files + the file_updates chain for a mod.

    Returns ``(files, file_updates)``:

    - ``files`` — every file Nexus has on the mod page, sorted newest
      first by upload timestamp.
    - ``file_updates`` — author-declared ``old_file_id`` →
      ``new_file_id`` chain. Walking this is the canonical way to
      detect updates because it survives file renames and variant
      reorganisations (per Nexus Mods IModFiles contract).

    Returns ``None`` on any API/transport failure so callers can
    distinguish "no files uploaded" (``([], [])``) from "request
    failed" and retry later.
    """
    try:
        data = _api_request(
            f"/games/{GAME_DOMAIN}/mods/{mod_id}/files.json", api_key)
        if isinstance(data, dict):
            raw_files = data.get("files", []) or []
            raw_updates = data.get("file_updates", []) or []
        else:
            # Fallback for non-dict responses (older API shape).
            raw_files = data or []
            raw_updates = []
        files = [
            NexusFileInfo(
                file_id=f.get("file_id", 0),
                name=f.get("name", ""),
                version=f.get("version", ""),
                uploaded_timestamp=f.get("uploaded_timestamp", 0),
                file_name=f.get("file_name", ""),
                category_id=f.get("category_id", 0),
            ) for f in raw_files
        ]
        files.sort(key=lambda x: x.uploaded_timestamp, reverse=True)
        updates = [
            NexusFileUpdate(
                old_file_id=u.get("old_file_id", 0),
                new_file_id=u.get("new_file_id", 0),
                old_file_name=u.get("old_file_name", ""),
                new_file_name=u.get("new_file_name", ""),
                uploaded_timestamp=u.get("uploaded_timestamp", 0),
            ) for u in raw_updates
            if u.get("old_file_id") and u.get("new_file_id")
        ]
        return files, updates
    except (NexusAuthError, NexusRateLimited):
        raise
    except Exception as e:
        logger.warning("Failed to get files for mod %d: %s", mod_id, e)
        return None


def _resolve_latest_file(user_file_id: int,
                          files: list[NexusFileInfo],
                          file_updates: list[NexusFileUpdate]
                          ) -> NexusFileInfo | None:
    """Walk the file_updates chain to find the file that supersedes
    ``user_file_id``.

    Algorithm:

    1. If ``user_file_id`` itself isn't in ``files``, return None.
    2. Look for ``{old_file_id: user_file_id, new_file_id: X}``. If
       found, recurse with X. If not, the user is on the chain head.
    3. Return the chain-head file's :class:`NexusFileInfo`.

    Cycle protection caps the walk at 64 hops (no real mod has that
    many successive uploads in one chain).

    Returns ``None`` when the user's file_id isn't in this mod's
    file list (file may have been deleted, or backfill bound the
    wrong nexus_mod_id).
    """
    if not user_file_id:
        return None
    by_id = {f.file_id: f for f in files}
    if user_file_id not in by_id:
        return None
    chain = {u.old_file_id: u.new_file_id for u in file_updates}
    current = user_file_id
    seen = {current}
    for _ in range(64):
        nxt = chain.get(current)
        if nxt is None or nxt in seen:
            break
        seen.add(nxt)
        current = nxt
    return by_id.get(current)


def get_recently_updated(
    api_key: str, period: str = "1w"
) -> dict[int, int] | None:
    """Get mods updated in the given period.

    Uses a single API call to check all recently updated mods.
    This is the efficient way — avoids per-mod API calls.

    Args:
        period: "1d", "1w", or "1m"

    Returns:
        ``{mod_id: latest_file_update_timestamp}`` on success (possibly
        empty when genuinely no mods updated in the window), or
        ``None`` on transport / API failure. The caller can then
        distinguish "feed trustworthy, no updates" from "feed
        unavailable, don't trust the silence" and fall through to
        per-mod checks on the latter.
    """
    try:
        data = _api_request(
            f"/games/{GAME_DOMAIN}/mods/updated.json?period={period}",
            api_key)
        return {
            entry["mod_id"]: entry.get("latest_file_update", 0)
            for entry in data
        }
    except (NexusAuthError, NexusRateLimited):
        # Let both propagate — the GUI surfaces distinct banners for
        # auth failures and rate-limit backoff; lumping either into
        # "feed unavailable" would waste follow-up per-mod calls that
        # are guaranteed to fail the same way (Bug #33).
        raise
    except Exception as e:
        logger.warning("Failed to get recent updates: %s", e)
        return None


WEEK_SECONDS = 7 * 24 * 3600


def check_mod_updates(
    mods: list[dict],
    api_key: str,
    db=None,  # Kept for backwards compatibility; ignored — see below.
) -> tuple[list[ModUpdateStatus], list[int], int, dict[int, int]]:
    """Check for updates on all mods that have a NexusMods ID.

    Strategy:

    - Pull the 1-week recently-updated feed once (cheap, batched).
    - For each mod: if it's in the feed, always fetch its file list.
      If it's NOT in the feed, consult ``mod["nexus_last_checked_at"]``
      (set to ``0`` / ``None`` when never checked). When the last check
      was less than a week ago, we trust the feed's "not updated" and
      skip the per-mod endpoint. When the last check was longer ago —
      or the column is missing — fall through and hit the per-mod
      endpoint anyway.

    Returns ``(updates, checked_row_ids, now_timestamp, backfill_file_ids)``:

    - ``updates`` — ``ModUpdateStatus`` for every mod we both fetched
      AND matched a file for. ``has_update=True`` means the Nexus file
      supersedes the local copy; ``has_update=False`` means "confirmed
      current" (name match succeeded but no newer file). Callers that
      only want outdated entries should pipe the list through
      :func:`filter_outdated`.
    - ``checked_row_ids`` — ``mods.id`` values whose file list we
      successfully fetched. Only rows that returned a valid file list
      are included — transient failures are NOT persisted, so the next
      run retries. Caller persists the timestamps on its own thread to
      avoid the SQLite cross-thread constraint.
    - ``now_timestamp`` — unix time at which the check ran, so the
      caller can apply the same value to every row.
    - ``backfill_file_ids`` — ``{mod_row_id: nexus_file_id}`` for rows
      that had no ``nexus_real_file_id`` but were resolved via the
      name-match fallback this cycle. Caller persists these so the
      next check can walk the ``file_updates`` chain instead of
      guessing by name.

    ``db`` is accepted for backwards compatibility but no longer used.
    Previous revisions wrote ``nexus_last_checked_at`` from inside this
    function, which raised ``sqlite3.ProgrammingError`` when called from
    a worker thread and silently dropped the optimisation.
    """
    import time

    nexus_mods = [(m, m["nexus_mod_id"]) for m in mods if m.get("nexus_mod_id")]
    if not nexus_mods:
        return [], [], int(time.time()), {}

    # Optimization call — cheap, even when 0 mods are outdated. Returns
    # None when the feed call itself failed (network, 5xx). In that case
    # we can't trust "not in feed" as negative evidence, so we skip the
    # feed-skip optimization for this cycle and fall through to the
    # per-mod endpoint for every mod.
    updated_ids = get_recently_updated(api_key, period="1w")
    feed_trustworthy = updated_ids is not None

    now = int(time.time())
    results: list[ModUpdateStatus] = []
    checked_mod_row_ids: list[int] = []
    # Backfill map: mod row id -> Nexus file_id. Populated when the
    # name-match path successfully resolves a file for a row that has
    # no nexus_real_file_id yet. The caller writes these back so the
    # NEXT update check can use the reliable file_updates chain walk
    # instead of fragile name matching.
    backfill_file_ids: dict[int, int] = {}
    for mod, nexus_id in nexus_mods:
        last_checked = int(mod.get("nexus_last_checked_at") or 0)
        in_feed = feed_trustworthy and nexus_id in updated_ids
        if (feed_trustworthy
                and not in_feed
                and (now - last_checked) < WEEK_SECONDS):
            # Feed skipped this mod AND we confirmed it was current
            # within the past week — safe to trust "no update".
            continue
        # When the feed call itself failed we can't trust "not in
        # feed = not updated", but the per-mod TTL is still valid:
        # if we successfully checked this mod within the past week,
        # don't re-hammer the per-mod endpoint just because the feed
        # blip means we'd otherwise check ALL mods this cycle (rate
        # limit risk on users with 50+ mods). Bug from Faisal
        # 2026-04-26 issue #3.
        if (not feed_trustworthy
                and last_checked > 0
                and (now - last_checked) < WEEK_SECONDS):
            continue

        result = get_mod_files(nexus_id, api_key)
        if result is None:
            # Transport/API failure — don't mark as checked so the
            # next run retries. Silently-dropping this was Codex P1.
            continue
        files, file_updates = result
        # files is a list (possibly empty). Empty = no uploads, valid.
        if mod.get("id") is not None and isinstance(mod["id"], int):
            checked_mod_row_ids.append(mod["id"])
        if not files:
            continue

        # A Nexus mod page often hosts MULTIPLE distinct files — main
        # mod plus optional addons, variants, hotfix builds. Mod 774's
        # page hosts six different mods under one mod_id. The user
        # installed ONE of them; we must only check for updates to
        # that specific file.
        #
        # Two strategies, in order:
        #
        # 1. **file_updates chain** (canonical, used by Vortex). When
        #    we know the user's nexus_file_id, walk the author's
        #    declared "this file is a newer version of X" chain
        #    (``file_updates``) to find the file that supersedes
        #    theirs. This survives renames + variant splits.
        # 2. **Name match fallback** for legacy rows where we don't
        #    have nexus_file_id yet (imported before v3.1.7a).
        local_ver = (mod.get("version") or "").strip()
        local_tuple = _version_to_tuple(local_ver)
        local_name = (mod.get("name") or "").strip()
        # nexus_real_file_id is the actual numeric Nexus file_id.
        # The older nexus_file_id column contains a VERSION string
        # by historical mistake — don't use it for chain walking.
        try:
            local_file_id = int(mod.get("nexus_real_file_id") or 0)
        except (TypeError, ValueError):
            local_file_id = 0

        # Track whether the user's stored file_id is gone from Nexus
        # entirely (deleted by author / mod taken down). The result
        # carries this so the GUI can render a 'source removed' badge
        # rather than a generic 'unknown' state.
        # Bug from Faisal 2026-04-26 issue #4.
        file_deleted_on_nexus = False
        # When self-correction triggers (chain says we're current but
        # versions disagree), the version mismatch IS the signal that
        # we're outdated — even when the version strings don't parse
        # to comparable tuples. Force has_update=True downstream so
        # the user sees the red pill. Bug from Faisal 2026-04-26
        # issue #2.
        forced_outdated_by_self_correction = False
        latest = None
        if local_file_id:
            latest = _resolve_latest_file(local_file_id, files, file_updates)
            if latest is None:
                # The user's file_id is no longer in the Nexus files
                # list — author deleted it, or the page got
                # restructured. Promote from debug to info so it's
                # visible in logs without being noisy.
                logger.info(
                    "update check: file_id=%d not in files for "
                    "nexus_mod_id=%d — file appears deleted on Nexus; "
                    "falling back to name match",
                    local_file_id, nexus_id)
                if {f.file_id for f in files} and local_file_id not in {f.file_id for f in files}:
                    file_deleted_on_nexus = True
            else:
                # Self-correction: if the chain walk says we're on the
                # latest (latest.file_id == local_file_id) but the
                # versions disagree, an earlier name-match backfill
                # latched onto the wrong file. Force re-resolve via the
                # name-match path below so the user gets the red
                # 'click to update' pill instead of stuck 'current'.
                # Bug from Faisal 2026-04-26 — Fat Stacks 1536.
                if latest.file_id == local_file_id:
                    latest_ver = (latest.version or "").strip()
                    latest_ver_tuple = _version_to_tuple(latest_ver)
                    versions_disagree = False
                    if (local_tuple is not None
                            and latest_ver_tuple is not None
                            and latest_ver_tuple != local_tuple):
                        # Tuple compare — preferred path.
                        versions_disagree = True
                    elif (local_ver and latest_ver
                          and (local_tuple is None
                               or latest_ver_tuple is None)
                          and local_ver.lower() != latest_ver.lower()):
                        # Bug from Faisal 2026-04-26 issue #2: at
                        # least one side doesn't parse to a tuple but
                        # both have non-empty strings that don't match.
                        # Treat that as a mismatch too — covers mods
                        # with weird version strings (alpha/beta/etc).
                        versions_disagree = True
                    if versions_disagree:
                        logger.info(
                            "update check: nexus_real_file_id=%d for "
                            "%r looks wrong (local_ver=%r, file_ver=%r)"
                            " — re-doing name match",
                            local_file_id, local_name, local_ver,
                            latest.version)
                        latest = None
                        # Force backfill recompute despite stored id
                        local_file_id = 0
                        forced_outdated_by_self_correction = True

        if latest is None:
            # Either no local_file_id stored, or it's not in the
            # current files list. Fall back to the name-match path.
            candidates = _filter_files_by_name(files, local_name)
            if not candidates:
                logger.debug(
                    "update check: no Nexus file matches local mod %r "
                    "(nexus_mod_id=%d, %d files on page)",
                    local_name, nexus_id, len(files))
                continue
            latest_tuple_inner = None
            for f in candidates:
                f_ver = (f.version or "").strip()
                if not f_ver:
                    continue
                f_tuple = _version_to_tuple(f_ver)
                if f_tuple is None:
                    continue
                if latest_tuple_inner is None or f_tuple > latest_tuple_inner:
                    latest_tuple_inner = f_tuple
                    latest = f
            if latest is None:
                # No version-parseable candidate. Fall back to upload
                # order — get_mod_files sorts files newest-first, so
                # candidates[0] is the most recently uploaded file
                # whose name matches. Better than skipping the mod
                # entirely (which leaves the user with no signal).
                # Bug from Faisal 2026-04-26 issue #2 — alpha/beta
                # version strings don't parse, but we still want to
                # detect the chain forward.
                latest = candidates[0]
            # Backfill: this row had no nexus_real_file_id going in
            # but we just resolved one via the name-match path. Record
            # it so the caller can persist it and the next check
            # uses the reliable chain walk.
            #
            # When the page hosts MULTIPLE files at different versions
            # (Fat Stacks 1536: two version-1 files + one version-2),
            # `latest` here is the highest-version pick — correct for
            # the has_update determination but WRONG for backfill: if
            # the user is actually on the older file, latching the
            # backfill to the latest makes future cycles think the
            # user is current. Prefer to backfill the candidate whose
            # version matches local_ver. Bug from Faisal 2026-04-26.
            if not local_file_id:
                row_id = mod.get("id")
                backfill_target = latest
                if local_tuple is not None:
                    matching = [
                        f for f in candidates
                        if _version_to_tuple(
                            (f.version or "").strip()) == local_tuple
                    ]
                    if len(matching) == 1:
                        backfill_target = matching[0]
                    elif len(matching) > 1:
                        # Multiple version-matching candidates — prefer
                        # the one that has a file_updates chain entry
                        # pointing forward (we know there's a successor,
                        # so the next chain walk will return it). Falls
                        # back to lowest file_id when no successor info.
                        chain_starts = {
                            u.old_file_id for u in file_updates
                        }
                        with_chain = [
                            f for f in matching
                            if f.file_id in chain_starts
                        ]
                        if with_chain:
                            backfill_target = min(
                                with_chain, key=lambda f: f.file_id)
                        else:
                            backfill_target = min(
                                matching, key=lambda f: f.file_id)
                matched_file_id = int(getattr(
                    backfill_target, "file_id", 0) or 0)
                # Don't queue a backfill that would just rewrite the
                # same value already stored. Bug from Faisal 2026-04-26
                # issue #1: when self-correction triggered but the only
                # available backfill target is the same wrong value
                # already in nexus_real_file_id (the v1 file is
                # archived and excluded by name-match), the DB was
                # getting the same wrong value rewritten on every cycle.
                existing_id = 0
                try:
                    existing_id = int(mod.get("nexus_real_file_id") or 0)
                except (TypeError, ValueError):
                    existing_id = 0
                if (isinstance(row_id, int) and row_id > 0
                        and matched_file_id > 0
                        and matched_file_id != existing_id):
                    backfill_file_ids[row_id] = matched_file_id

        latest_tuple = _version_to_tuple((latest.version or "").strip())
        remote_ver = (latest.version or "").strip()

        # Outdated iff the chain walk landed on a DIFFERENT file_id
        # (the author explicitly declared a successor), OR we couldn't
        # do a chain walk (legacy row, local_file_id=0) and a name-
        # match candidate has a strictly greater version string.
        #
        # Critical: when local_file_id is set AND latest.file_id ==
        # local_file_id, the user IS on the file Nexus is serving as
        # the latest. Version-string drift between CDUMM's filename-
        # extracted local_ver and Nexus's API "version" field is NOT
        # an update — that was the false-positive 'mod has only 1 file
        # but pill is red' bug (e.g. Better Radial Menus 1.5 vs 1.5.2,
        # both on file_id 5733).
        has_update = False
        if forced_outdated_by_self_correction:
            # Self-correction caught a version mismatch on a stored
            # file_id that earlier mistakenly latched onto the latest.
            # The disagreement IS the signal. Bug from Faisal
            # 2026-04-26 issue #2.
            has_update = True
        elif local_file_id and latest.file_id != local_file_id:
            # Author declared this as a successor — definitively
            # outdated regardless of version string parsing.
            has_update = True
        elif local_file_id and latest.file_id == local_file_id:
            # Same file. User is current. Version drift is metadata,
            # not a real update.
            has_update = False
        elif local_tuple is None:
            # No local_file_id AND local version doesn't parse — bail
            # out as 'unknown' rather than guessing.
            has_update = False
        elif latest_tuple is not None and latest_tuple > local_tuple:
            # Legacy row (no nexus_real_file_id) AND name-match
            # candidate has a strictly greater version. Real update.
            has_update = True

        # Diagnostic: when we flag a mod outdated, log the comparison
        # so we can audit false-positives like 'mod has only 1 file
        # but pill is red'. The triggers are an explicit author-
        # declared successor (file_id mismatch) OR a version-string
        # comparison; both are recorded.
        if has_update:
            trigger = ("file_id_mismatch"
                       if local_file_id and latest.file_id != local_file_id
                       else "version_greater")
            logger.info(
                "update check: outdated %r (nexus_mod_id=%d) — "
                "trigger=%s, local_ver=%r local_file_id=%d, "
                "latest_ver=%r latest_file_id=%d",
                mod.get("name"), nexus_id, trigger,
                local_ver, local_file_id,
                remote_ver, int(getattr(latest, "file_id", 0) or 0))

        # Bug #1 fix: emit results for BOTH outdated and confirmed-current
        # mods so the UI can paint green vs grey correctly. The page
        # layer treats "has_update=False" entries as "confirmed current"
        # (green) and the ABSENCE of any entry as "unknown" (grey).
        # Before this, only outdated mods were appended, so every other
        # matched mod was indistinguishable from a name-match failure and
        # got painted green anyway by the elif-nexus_id branch.
        results.append(ModUpdateStatus(
            mod_id=nexus_id,
            local_name=mod["name"],
            local_version=local_ver,
            latest_version=remote_ver,
            has_update=has_update,
            mod_url=f"https://www.nexusmods.com/{GAME_DOMAIN}/mods/{nexus_id}",
            latest_file_id=int(getattr(latest, "file_id", 0) or 0),
            file_deleted_on_nexus=file_deleted_on_nexus,
        ))

    return results, checked_mod_row_ids, now, backfill_file_ids


# Nexus file category_id values that should NEVER be considered as an
# update target, even when they match the local mod's name. 4 =
# OLD_VERSION (explicitly archived by the author), 6 = DELETED, 7 =
# ARCHIVED (page-level archive). MAIN (1), PATCH (2), OPTIONAL (3)
# are the valid update candidates.
_EXCLUDED_CATEGORY_IDS = {4, 6, 7}


def clear_outdated_after_update(
    updates: dict[int, "ModUpdateStatus"],
    nexus_mod_id: int,
    new_version: str,
) -> dict[int, "ModUpdateStatus"]:
    """Return a NEW updates dict where ``nexus_mod_id``'s entry (if any)
    has been replaced with a ``has_update=False`` entry reflecting the
    just-downloaded version.

    Used by the nxm:// post-import hook: after a successful update we
    know the user is now on the latest file we knew about, so the pill
    should paint GREEN. Popping the entry outright would reset the pill
    to GREY (unknown) under the three-state semantics added for Bug #3.

    The caller's input dict is not mutated.
    """
    out = dict(updates)
    existing = out.get(nexus_mod_id)
    if existing is None:
        return out
    ver = (new_version or existing.latest_version or "").strip()
    out[nexus_mod_id] = ModUpdateStatus(
        mod_id=existing.mod_id,
        local_name=existing.local_name,
        local_version=ver,
        latest_version=ver,
        has_update=False,
        mod_url=existing.mod_url,
        latest_file_id=existing.latest_file_id,
    )
    return out


def filter_outdated(updates):
    """Keep only ``ModUpdateStatus`` entries where ``has_update=True``.

    ``check_mod_updates`` emits entries for BOTH outdated (red pill)
    and confirmed-current (green pill) mods so the pill renderer can
    distinguish them from unknown-state mods. UI paths that only want
    a "what's actually new" summary (Settings → Check for Mod Updates
    dialog, log lines, toast counts) should pipe the result through
    this so they don't show up-to-date mods as if they had updates.

    Accepts any iterable of items with a ``has_update`` attribute.
    """
    return [u for u in updates if getattr(u, "has_update", False)]


def _filter_files_by_name(files, local_name: str):
    """Return Nexus files whose name matches the local mod's name.

    A Nexus mod page can host multiple distinct files — main + addons,
    variants, hotfix builds. We only want to check for updates to the
    file the user actually has, not "any file on the page."

    Two-pass match:

    1. **Exact prettified equality** — the strict path. ``Faster
       Vanilla Style`` matches ``faster vanilla style`` and
       ``Faster_Vanilla_Style`` after prettification.
    2. **High token overlap fallback** — when no strict match wins,
       accept files with Jaccard ≥ 0.6. Handles minor rewordings
       between when the user imported and now.

    Returns ``[]`` when nothing matches. Caller should leave the mod
    un-flagged in that case rather than guess.
    """
    if not local_name or not files:
        return []
    # Lazy import — mod_matching pulls in the import_handler chain
    # which is heavyweight for a hot path.
    from cdumm.engine.mod_matching import is_same_mod, token_overlap_ratio

    # Exclude archival categories (OLD_VERSION, DELETED, ARCHIVED) up
    # front so no downstream pass can accidentally pick one as the
    # latest. The ``category_id`` attribute defaults to 0 when the
    # input object doesn't carry it — treated as "unknown but not
    # explicitly archived", i.e. kept.
    files = [
        f for f in files
        if getattr(f, "category_id", 0) not in _EXCLUDED_CATEGORY_IDS
    ]
    if not files:
        return []

    exact = []
    for f in files:
        f_name = (getattr(f, "name", "") or "").strip()
        if f_name and is_same_mod(local_name, f_name):
            exact.append(f)
    if exact:
        return exact

    # Fallback: pick the highest-overlap file IF its overlap clears
    # the 0.6 threshold. Single best match, not all candidates above
    # 0.6, so we don't accidentally pick a sibling variant that
    # happens to share many words with the user's mod.
    best = None
    best_score = 0.0
    for f in files:
        f_name = (getattr(f, "name", "") or "").strip()
        if not f_name:
            continue
        score = token_overlap_ratio(local_name, f_name)
        if score >= 0.6 and score > best_score:
            best = f
            best_score = score
    if best is not None:
        return [best]

    # Bug #4 fix: when a Nexus page has exactly one file, accept it
    # unconditionally. A 2026-04-22 real-user log showed Berserk The
    # Dragon Slayer (1455) failed matching with 1 file on its page,
    # because the local stored name was "Berserk The Dragon Slayer"
    # while the author uploaded it under a different timestamped
    # filename. With only one candidate there's no variant ambiguity
    # to protect against — unconditional acceptance is safe.
    if len(files) == 1:
        only = files[0]
        f_name = (getattr(only, "name", "") or "").strip()
        if f_name:
            logger.debug(
                "update check: single-file auto-match %r -> %r "
                "(name overlap failed but no other candidates)",
                local_name, f_name)
            return [only]
    return []


def _version_to_tuple(ver: str):
    """Parse a version string into a comparable tuple per semver 2.0.0.

    Returns a 2-tuple ``(core_tuple, marker_tuple)``:

    - ``core_tuple`` — the MAJOR.MINOR.PATCH numeric parts (with
      trailing zeros stripped so 1.0 == 1.0.0 == 1).
    - ``marker_tuple`` — ``(1,)`` for a normal release, or
      ``(0, *pre_release_ids)`` for a pre-release. The leading 0 vs 1
      ensures pre-releases always compare LESS than the same base
      version (semver §11). Each pre-release identifier is wrapped as
      ``(is_alpha, value)`` so numeric < alpha within pre-release
      comparisons (also semver §11) and Python doesn't choke on
      mixed-type tuple comparison.

    Returns ``None`` when the string isn't parseable so callers can
    detect "unknown version" explicitly.

    Examples (precedence verified against semver.org examples):

    - ``1.2`` → ``((1, 2), (1,))`` (normal release)
    - ``1.2-rc1`` → ``((1, 2), (0, (True, 'rc1')))`` (pre-release)
    - ``1.2`` > ``1.2-rc1`` → True (normal release wins)
    - ``1.0.0-alpha`` < ``1.0.0`` → True (semver §11 example)
    - ``1.0.0-alpha`` < ``1.0.0-alpha.1`` → True (semver §11)
    - ``1.0.0-alpha.1`` < ``1.0.0-beta`` → True
    - ``v.2`` == ``2`` → True (Vaxis-style prefix)
    - ``1.0`` == ``1.0.0`` → True (trailing-zero normalization)
    - Build metadata (``1.0+abc``) ignored per semver §10.
    """
    if not ver:
        return None
    s = ver.strip().lower()
    # Strip leading "v" or "v." prefix.
    if s.startswith("v."):
        s = s[2:]
    elif s.startswith("v") and len(s) > 1 and s[1].isdigit():
        s = s[1:]
    # Strip build metadata — per semver §10, build metadata MUST be
    # ignored when determining precedence.
    s = s.split("+", 1)[0]
    # Split off the pre-release tag (if any). First "-" wins.
    if "-" in s:
        core, pre = s.split("-", 1)
    else:
        core, pre = s, ""
    # Parse the numeric core (MAJOR.MINOR.PATCH...).
    parts = []
    for seg in core.split("."):
        seg = seg.strip()
        if not seg:
            continue
        try:
            parts.append(int(seg))
        except ValueError:
            # Non-numeric tail like "1.0a" or "1.2 (final)" — keep the
            # leading numeric run and stop.
            num = ""
            for ch in seg:
                if ch.isdigit():
                    num += ch
                else:
                    break
            if num:
                parts.append(int(num))
            break
    if not parts:
        return None
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    core_tuple = tuple(parts)
    # Pre-release identifiers (semver §11): split on dots, classify
    # numeric vs alpha. Numeric IDs always lower than alpha (so
    # ``1.0-1 < 1.0-alpha``); the (False, n) vs (True, "alpha") wrapper
    # keeps Python tuple comparison type-safe.
    if pre:
        pre_ids = []
        for ident in pre.split("."):
            ident = ident.strip()
            if not ident:
                continue
            try:
                pre_ids.append((False, int(ident)))
            except ValueError:
                pre_ids.append((True, ident))
        if pre_ids:
            return (core_tuple, (0, *pre_ids))
    return (core_tuple, (1,))
