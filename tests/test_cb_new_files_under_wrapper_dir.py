"""Character Creator layout: wrapper folder above NNNN/ and new non-DDS
files with no vanilla PAMT entry (prefab / pac / pac_xml)."""
from types import SimpleNamespace

from cdumm.engine.crimson_browser_handler import (
    _numbered_dir_index,
    _vanilla_ext_convention,
)


def test_numbered_dir_index_strips_wrapper() -> None:
    assert _numbered_dir_index(("0009", "character", "a.prefab")) == 0
    assert _numbered_dir_index(
        ("Character Creator Enhanced", "0012", "ui", "x.dds")) == 1
    # no numbered dir / nothing after it -> not a game file path
    assert _numbered_dir_index(("README.txt",)) is None
    assert _numbered_dir_index(("docs", "THIRD_PARTY.txt")) is None
    assert _numbered_dir_index(("wrap", "0009")) is None
    # a 3-digit part below a wrapper is not a PAZ dir
    assert _numbered_dir_index(("wrap", "123", "a.xml")) is None


def _e(path: str, hi: int):
    return SimpleNamespace(path=path, flags=hi << 16)


def test_vanilla_ext_convention_from_flags() -> None:
    entries = [
        _e("character/a.pac", 0x01), _e("character/b.pac", 0x01),
        _e("character/c.pac", 0x00),
        _e("character/a.pac_xml", 0x32), _e("character/b.pac_xml", 0x30),
        _e("character/a.prefab", 0x00), _e("character/b.prefab", 0x02),
    ]
    conv = _vanilla_ext_convention(entries)
    # .pac is type 1 ('PAR' partial) in vanilla: new ones are stored raw
    assert conv[".pac"] == (0, False)
    assert conv[".pac_xml"] == (2, True)
    assert conv[".prefab"] == (2, False)
