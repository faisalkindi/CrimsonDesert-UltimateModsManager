"""_open_zip_utf8 must not crash on Python 3.10.

zipfile's ``metadata_encoding=`` parameter only exists on Python 3.11+.
The helper used to pass it unconditionally and guard only against
UnicodeDecodeError, so on 3.10 -- a supported version per pyproject's
``requires-python = ">=3.10"`` -- every .zip import raised an uncaught
TypeError at ZipFile construction, before any name was decoded. This
opens a real archive (with a non-ASCII member name, the case
metadata_encoding targets) and asserts the helper returns a usable
ZipFile on the running interpreter.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

from cdumm.engine.import_handler import _open_zip_utf8


def test_open_zip_utf8_opens_archive_on_any_supported_python(tmp_path: Path) -> None:
    # Katakana folder/file name built from code points, so this source
    # file stays pure ASCII while the archived name is genuinely
    # non-ASCII (the case metadata_encoding is meant to handle).
    folder = "".join(map(chr, (0x30D5, 0x30A9, 0x30EB, 0x30C0)))
    stem = "".join(map(chr, (0x30C6, 0x30AF, 0x30B9, 0x30C1, 0x30E3)))
    member = f"{folder}/{stem}.dds"

    zp = tmp_path / "mod.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr(member, b"payload")

    with _open_zip_utf8(zp) as zf:
        names = zf.namelist()

    assert any(n.endswith(".dds") for n in names)
