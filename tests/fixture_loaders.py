"""Shared loaders for committed binary fixtures.

Audit finding C7 (2026-06-10): the byte-writer proof tests were gated
on machine-local paths (Temp extracts, gitignored issue_repro), so
the suite was green in CI while the round-trip proofs for the archive
writers never executed anywhere. The CD 1.10 vanilla extracts now
live zlib-compressed in tests/fixtures/vanilla110/ (10:1 on these
tables); the gitignored issue_repro copy is the fallback for files
not yet committed.
"""
from __future__ import annotations

import zlib
from functools import lru_cache
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TESTS_DIR.parent


@lru_cache(maxsize=8)
def load_vanilla110(name: str) -> bytes:
    """Load a CD 1.10 vanilla extract (e.g. "iteminfo.pabgb").

    Raises FileNotFoundError when neither the committed compressed
    fixture nor the issue_repro fallback has the file.
    """
    packed = _TESTS_DIR / "fixtures" / "vanilla110" / (name + ".zlib")
    if packed.exists():
        return zlib.decompress(packed.read_bytes())
    loose = _REPO_ROOT / "issue_repro" / "182" / "vanilla110" / name
    if loose.exists():
        return loose.read_bytes()
    raise FileNotFoundError(
        f"vanilla110 fixture {name!r} absent from tests/fixtures and "
        f"issue_repro")


def has_vanilla110(name: str) -> bool:
    try:
        load_vanilla110(name)
        return True
    except FileNotFoundError:
        return False


@lru_cache(maxsize=8)
def load_vanilla113(name: str) -> bytes:
    """Load a CD 1.13 vanilla extract (e.g. "iteminfo.pabgb").

    Same idea as :func:`load_vanilla110`, for the version the game
    actually ships. Needed because 1.13 restructured iteminfo's
    equipment records (SubItem tag 17 + _enchantDataList), so the 1.10
    fixture cannot exercise that code at all.

    This is finding C7 all over again, which is why it now has a
    committed fixture rather than an env var. The 1.13 tests were
    reading ``CDUMM_VANILLA_ITEMINFO_DIR`` or a ``tests/fixtures/iteminfo/``
    directory that has never existed in this repo -- so they skipped
    silently, in CI and on a fresh clone, and the strongest assertions in
    the suite (0 opaque records, the enchant-tier oracle, byte-exact
    re-serialization) guarded nothing. A test that can only run on one
    developer's machine is a test that does not exist.

    Compresses ~10:1 like the 1.10 tables (5.8 MB -> 549 KB).
    """
    packed = _TESTS_DIR / "fixtures" / "vanilla113" / (name + ".zlib")
    if packed.exists():
        return zlib.decompress(packed.read_bytes())
    raise FileNotFoundError(
        f"vanilla113 fixture {name!r} absent from tests/fixtures")


def has_vanilla113(name: str) -> bool:
    try:
        load_vanilla113(name)
        return True
    except FileNotFoundError:
        return False


class _FixtureHandle110:
    """Same handle, over the CD 1.10 fixtures.

    Which era a test wants is not cosmetic. The iteminfo writer/parser
    tests were pinned against Faisal's extract from the 2026-04-29 patch
    (records grew 10 bytes) and assert against *that* layout, so they
    belong on vanilla110. Pointing them at the 1.13 table makes the
    parser desync -- it doesn't just fail, it spins -- which is a fair
    demonstration of why "just make it not skip" isn't the same as
    "make it test the right thing".
    """

    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        self._name = name

    def exists(self) -> bool:
        return has_vanilla110(self._name)

    def read_bytes(self) -> bytes:
        return load_vanilla110(self._name)

    def with_suffix(self, suffix: str) -> "_FixtureHandle110":
        stem = self._name.rsplit(".", 1)[0]
        return _FixtureHandle110(stem + suffix)

    @property
    def name(self) -> str:
        return self._name

    def __fspath__(self) -> str:
        return self._name

    def __repr__(self) -> str:
        return f"<vanilla110 fixture {self._name!r}>"


def vanilla110_file(name: str) -> _FixtureHandle110:
    """A Path-like handle onto a committed CD 1.10 fixture."""
    return _FixtureHandle110(name)


class _FixtureHandle:
    """A committed fixture that quacks like the ``Path`` it replaces.

    Finding C7 was fixed for *some* tests but not all: 16 test modules
    still pointed at ``C:\\Users\\faisa\\AppData\\Local\\Temp\\...`` and
    ``C:\\Users\\faisa\\Downloads\\...`` -- one maintainer's machine. Those
    tests could not run in CI, could not run on a fresh clone, and could
    not run for any contributor. They were not "tests that need a game
    install"; they were tests that had quietly stopped existing.

    They all consumed the path the same way (``.exists()``,
    ``.read_bytes()``, occasionally ``.with_suffix()``), so exposing that
    tiny surface over the committed fixture turns each of those modules
    into a one-line change instead of a rewrite -- and, more importantly,
    makes the skip conditions evaluate to "present" so the assertions
    finally execute.
    """

    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        self._name = name

    def exists(self) -> bool:
        return has_vanilla113(self._name)

    def read_bytes(self) -> bytes:
        return load_vanilla113(self._name)

    def with_suffix(self, suffix: str) -> "_FixtureHandle":
        stem = self._name.rsplit(".", 1)[0]
        return _FixtureHandle(stem + suffix)

    @property
    def name(self) -> str:
        return self._name

    def __fspath__(self) -> str:            # so os.fspath()/str() stay sane
        return self._name

    def __repr__(self) -> str:
        return f"<vanilla113 fixture {self._name!r}>"


def vanilla113_file(name: str) -> _FixtureHandle:
    """A Path-like handle onto a committed CD 1.13 fixture.

    Use this instead of hardcoding an absolute path to a game extract.
    """
    return _FixtureHandle(name)


def real_mod_fixture(relpath: str) -> Path:
    """Locate a real third-party mod file used as a test fixture.

    These are other people's mods from Nexus, so they are deliberately
    NOT committed -- that would be redistributing someone else's work.
    But they must not be pinned to one maintainer's Downloads folder
    either: four tests hardcoded ``C:/Users/faisa/Downloads/Compressed/``
    and therefore skipped for literally every other human and every CI
    run, while looking like they were covering the real-mod import path.

    Resolution order:
      1. ``$CDUMM_MOD_FIXTURES/<relpath>``
      2. ``tests/fixtures/mods/<relpath>``  (gitignored; drop files here)

    Returns the resolved Path (which may not exist -- callers skip on
    ``.exists()``, and the skip reason should say how to enable it).
    """
    import os
    env = os.environ.get("CDUMM_MOD_FIXTURES")
    if env:
        p = Path(env) / relpath
        if p.exists():
            return p
    return _TESTS_DIR / "fixtures" / "mods" / relpath


MOD_FIXTURE_HOWTO = (
    "real third-party mod fixture not present -- drop it in "
    "tests/fixtures/mods/ or set $CDUMM_MOD_FIXTURES (these mods are not "
    "committed: they're other authors' work)"
)
