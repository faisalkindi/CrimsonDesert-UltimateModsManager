from pathlib import Path

import pytest

from cdumm.storage.database import Database


@pytest.fixture
def db(tmp_path: Path) -> Database:
    """Provide an initialized in-memory-like database for tests."""
    database = Database(tmp_path / "test.db")
    database.initialize()
    yield database
    database.close()


# ── derive_table_layout tests: resolve readers by FIELD NAME, not address ──
#
# The game moved from build 25116796 to 25381195 on 2026-09-20 and every
# function address in the exe changed. Tests that pinned a reader by its
# address all failed at once, although every property they checked still
# held. A reader is identified by the field whose error string it guards,
# and field_reads() already maps names to callees, so that is the handle
# tests use. The historical addresses stay in the docstrings as the
# build-25116796 record. GitHub #409.

def _crimson_desert_dir() -> Path | None:
    import os
    env = os.environ.get("CDUMM_GAME_DIR")
    if env and (Path(env) / "bin64").is_dir():
        return Path(env)
    for root in ("C:", "D:", "E:", "F:"):
        for lib in ("SteamLibrary", "Steam"):
            p = Path(f"{root}/{lib}/steamapps/common/Crimson Desert")
            if (p / "bin64").is_dir():
                return p
    return None


@pytest.fixture(scope="session")
def deriver():
    """A Deriver over the installed game, or skip."""
    import sys
    pytest.importorskip("capstone", reason="analysis-only dependency")
    pytest.importorskip("pefile", reason="analysis-only dependency")
    game = _crimson_desert_dir()
    if game is None:
        pytest.skip("no Crimson Desert install found (set CDUMM_GAME_DIR)")
    tools = Path(__file__).resolve().parents[1] / "tools"
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))
    from derive_table_layout import Deriver
    return Deriver(game)


@pytest.fixture(scope="session")
def reader_of(deriver):
    """``reader_of(table, field)``: the callee that reads that field."""
    def _lookup(table: str, field: str) -> int:
        for name, kind, val in deriver.field_reads(table):
            if name == field:
                assert kind == "call", (
                    f"{table}.{field} is read inline ({kind} {val})")
                return val
        raise AssertionError(f"{table}.{field} not found in field_reads")
    return _lookup
