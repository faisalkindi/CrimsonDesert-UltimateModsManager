"""Headless tests for the game-data index engine (cdumm.engine.game_index).

Exercises the schema, per-archive insert, query helpers, and the full
build_index path with an injected fake parser + fake install dir — no real
Crimson Desert install or archive bytes required.
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from cdumm.engine import game_index as gi


def _entry(path, paz_file="0008/0.paz", offset=0, comp=10, orig=10, enc=False):
    return SimpleNamespace(
        path=path, paz_file=paz_file, offset=offset,
        comp_size=comp, orig_size=orig,
        compressed=(comp != orig), encrypted=enc)


def _db(entries_by_archive):
    con = sqlite3.connect(":memory:")
    gi.create_schema(con)
    for arch, ents in entries_by_archive.items():
        gi.insert_archive(con, arch, ents)
    gi.finalize(con)
    gi.write_stats(con)
    return con


def test_category_and_ext_derivation():
    assert gi.category_of("gamedata/iteminfo.pabgb") == "gamedata"
    assert gi.category_of("sequencer/x.paseq") == "sequencer"
    assert gi.category_of("root.txt") == "(root)"
    assert gi.ext_of("a/b.PASEQ") == ".paseq"       # lower-cased
    assert gi.ext_of("gamedata/iteminfo.pabgb") == ".pabgb"
    assert gi.ext_of("noext") == "(none)"


def test_schema_counts_and_flags():
    con = _db({
        "0008": [_entry("gamedata/iteminfo.pabgb", comp=100, orig=200),  # compressed
                 _entry("gamedata/iteminfo.pabgh"),
                 _entry("sequencer/x.paseq")],
        "0014": [_entry("ui/y.xml", enc=True)],
    })
    st = gi.get_stats(con)
    assert st["assets_total"] == "4"
    assert st["archives"] == "2"
    # flags persisted as 0/1
    comp = con.execute(
        "SELECT compressed FROM assets WHERE path='gamedata/iteminfo.pabgb'"
    ).fetchone()[0]
    enc = con.execute(
        "SELECT encrypted FROM assets WHERE path='ui/y.xml'").fetchone()[0]
    assert comp == 1 and enc == 1


def test_search_by_substring_ext_category_archive():
    con = _db({"0008": [
        _entry("gamedata/iteminfo.pabgb"),
        _entry("sequencer/loading.paseq"),
        _entry("character/hero.paa"),
    ], "0014": [_entry("sequencer/other.paseq")]})

    r = gi.search_assets(con, query="iteminfo")
    assert [x["path"] for x in r] == ["gamedata/iteminfo.pabgb"]

    r = gi.search_assets(con, ext=".paseq")
    assert {x["path"] for x in r} == {
        "sequencer/loading.paseq", "sequencer/other.paseq"}

    r = gi.search_assets(con, category="character")
    assert [x["path"] for x in r] == ["character/hero.paa"]

    r = gi.search_assets(con, ext=".paseq", archive="0014")
    assert [x["path"] for x in r] == ["sequencer/other.paseq"]


def test_search_limit_and_like_wildcards_are_literal():
    con = _db({"0008": [_entry(f"gamedata/item{i}.pabgb") for i in range(10)]})
    assert len(gi.search_assets(con, query="item", limit=3)) == 3
    # a query containing % must match literally, not as a wildcard
    con.execute("INSERT INTO assets VALUES('gamedata/50%off.txt','0008',"
                "'gamedata','.txt','0008/0.paz',0,1,1,0,0)")
    r = gi.search_assets(con, query="50%off")
    assert [x["path"] for x in r] == ["gamedata/50%off.txt"]


def test_data_tables_catalog_dedup_and_order():
    con = _db({"0008": [
        _entry("gamedata/iteminfo.pabgb", orig=555),
        _entry("gamedata/stringinfo.pabgb", orig=999),
        _entry("gamedata/iteminfo.pabgb", orig=555),   # dup name
        _entry("character/hero.paa"),                  # not a table
    ]})
    tables = gi.list_data_tables(con)
    assert [t["name"] for t in tables] == [
        "stringinfo.pabgb", "iteminfo.pabgb"]          # largest first, deduped


def test_category_counts():
    con = _db({"0008": [
        _entry("character/a.paa"), _entry("character/b.paa"),
        _entry("sound/c.wem")]})
    counts = {c["category"]: c["n"] for c in gi.category_counts(con)}
    assert counts == {"character": 2, "sound": 1}


def test_build_index_end_to_end(tmp_path):
    """Full build_index path with a fake install + injected parser."""
    game = tmp_path / "game"
    for d in ("0008", "0014"):
        (game / d).mkdir(parents=True)
        (game / d / "0.pamt").write_bytes(b"")   # presence is all archive_dirs needs

    fake = {
        "0008": [_entry("gamedata/iteminfo.pabgb", orig=500),
                 _entry("gamedata/iteminfo.pabgh", orig=20),
                 _entry("character/hero.paa")],
        "0014": [_entry("sequencer/loading.paseq")],
    }

    def fake_parse(pamt_path, paz_dir=None):
        arch = pamt_path.replace("\\", "/").split("/")[-2]
        return fake[arch]

    seen = []
    out = tmp_path / "idx.sqlite"
    stats = gi.build_index(str(game), str(out),
                           parse_pamt=fake_parse,
                           progress=lambda a, n: seen.append((a, n)))

    assert stats["assets_total"] == 4
    assert stats["archives"] == 2
    assert stats["data_table_distinct"] == 2      # iteminfo.pabgb + .pabgh
    assert sorted(seen) == [("0008", 3), ("0014", 1)]

    con = sqlite3.connect(str(out))
    assert gi.search_assets(con, query="iteminfo")[0]["archive"] == "0008"
    con.close()


def test_build_index_no_archives_raises(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        gi.build_index(str(tmp_path), str(tmp_path / "x.sqlite"),
                       parse_pamt=lambda *a, **k: [])


# ── on-demand extraction + preview helpers ───────────────────────────

def _con_with_asset(path, paz_file, offset, comp, orig, enc=0):
    """In-memory index carrying exactly one hand-built asset row."""
    con = sqlite3.connect(":memory:")
    gi.create_schema(con)
    con.execute(
        "INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?,?)",
        (path, "0008", gi.category_of(path), gi.ext_of(path),
         paz_file, offset, comp, orig, int(comp != orig), enc))
    gi.finalize(con)
    return con


def _write_paz(tmp_path, payload, prefix=b"PAZ0hdr!"):
    """A fake .paz: prefix bytes then the stored payload; returns (path, off)."""
    p = tmp_path / "0008" / "8.paz"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(prefix + payload)
    return str(p), len(prefix)


def test_get_asset_returns_extract_fields():
    con = _con_with_asset("ui/x.xml", "/g/0008/8.paz", 0, 5, 9, enc=1)
    row = gi.get_asset(con, "ui/x.xml")
    assert row["comp_size"] == 5 and row["orig_size"] == 9
    assert row["compressed"] == 1 and row["encrypted"] == 1
    assert gi.get_asset(con, "nope") is None


def test_extract_uncompressed(tmp_path):
    data = b"hello world, plain stored bytes"
    paz, off = _write_paz(tmp_path, data)
    con = _con_with_asset("gamedata/x.txt", paz, off, len(data), len(data))
    assert gi.extract_asset(con, "gamedata/x.txt", str(tmp_path)) == data


def test_extract_compressed_roundtrip(tmp_path):
    import pytest
    pytest.importorskip("lz4")
    from cdumm.archive import paz_crypto
    plain = b"<root>" + b"AB" * 500 + b"</root>"     # compressible
    payload = paz_crypto.lz4_compress(plain)
    assert len(payload) != len(plain)                # actually compressed
    paz, off = _write_paz(tmp_path, payload)
    con = _con_with_asset("gamedata/x.bin", paz, off, len(payload), len(plain))
    assert gi.extract_asset(con, "gamedata/x.bin", str(tmp_path)) == plain


def test_extract_encrypted_roundtrip(tmp_path):
    import pytest
    from cdumm.archive import paz_crypto
    try:
        stored = paz_crypto.encrypt(b"<xml>secret</xml>", "x.xml")
    except Exception:                                # no crypto backend
        pytest.skip("no ChaCha20 backend available")
    paz, off = _write_paz(tmp_path, stored)
    con = _con_with_asset("ui/x.xml", paz, off, len(stored), len(stored), enc=1)
    assert gi.extract_asset(
        con, "ui/x.xml", str(tmp_path)) == b"<xml>secret</xml>"


def test_extract_reresolves_paz_under_game_dir(tmp_path):
    data = b"relocated install bytes"
    paz, off = _write_paz(tmp_path, data)            # real file at tmp/0008/8.paz
    # Stored path is stale (old machine); extract must re-resolve via game_dir.
    con = _con_with_asset("gamedata/x.txt",
                          r"E:\old\0008\8.paz", off, len(data), len(data))
    assert gi.extract_asset(con, "gamedata/x.txt", str(tmp_path)) == data


def test_extract_missing_asset_and_missing_paz(tmp_path):
    import pytest
    con = _con_with_asset("gamedata/x.txt", str(tmp_path / "gone.paz"),
                          0, 4, 4)
    with pytest.raises(KeyError):
        gi.extract_asset(con, "not/indexed", str(tmp_path))
    with pytest.raises(FileNotFoundError):
        gi.extract_asset(con, "gamedata/x.txt", str(tmp_path))


def test_decode_text_accepts_text_rejects_binary():
    assert gi.decode_text(b"<root>hi</root>\n") == "<root>hi</root>\n"
    assert gi.decode_text("héllo".encode("utf-16-le")) == "héllo"
    assert gi.decode_text(b"") == ""
    assert gi.decode_text(bytes(range(256)) * 8) is None   # mostly non-print


def test_hexdump_shape_and_truncation():
    d = gi.hexdump(b"ABC\x00\xff", limit=64)
    assert d.startswith("00000000  41 42 43 00 FF")
    assert "ABC.." in d
    big = gi.hexdump(b"\x00" * 100, limit=16)
    assert "showing first 16" in big and "100 bytes total" in big


def test_decode_image_png_roundtrips_and_downscales():
    import pytest
    PILImage = pytest.importorskip("PIL.Image")
    import io
    buf = io.BytesIO()
    PILImage.new("RGB", (2000, 1000), (10, 20, 30)).save(buf, format="PNG")
    r = gi.decode_image(buf.getvalue(), "x.png", max_dim=512)
    assert r is not None
    assert r["orig_w"] == 2000 and r["orig_h"] == 1000
    assert max(r["width"], r["height"]) == 512            # downscaled
    assert r["png"][:8] == b"\x89PNG\r\n\x1a\n"             # valid PNG out


def test_decode_image_rejects_non_images():
    # Not an image and not an image extension → no decode attempt / None.
    assert gi.decode_image(b"not an image at all", "x.bin") is None
    assert gi.decode_image(bytes(64), "gamedata/iteminfo.pabgb") is None


def test_dds_split_decompress_roundtrips():
    import pytest
    pytest.importorskip("lz4")
    from cdumm.archive import paz_crypto
    header = b"DDS " + bytes(124)            # 128-byte plaintext DDS header
    body = b"PIXELDATA" * 400                # compressible pixel body
    stored = header + paz_crypto.lz4_compress(body)   # header + LZ4 body
    out = gi._dds_split_decompress(stored, len(header) + len(body))
    assert out == header + body


def test_dds_split_decompress_rejects_non_dds():
    assert gi._dds_split_decompress(b"\x00" * 200, 500) is None       # no magic
    assert gi._dds_split_decompress(b"DDS " + bytes(60), 500) is None  # too short


def test_dds_top_mip_recovers_from_lz4_stream():
    import pytest
    pytest.importorskip("lz4")
    from cdumm.archive import paz_crypto
    mip0 = bytes(range(256)) * 4          # 1024 bytes of top-mip "pixels"
    tail = b"\xAB" * 4000                 # stands in for the lower mip chain
    body = paz_crypto.lz4_compress(mip0 + tail)   # continuous LZ4 body
    hdr = bytearray(b"DDS " + bytes(124))
    hdr[20:24] = len(mip0).to_bytes(4, "little")  # dwPitchOrLinearSize = mip0
    hdr[84:88] = b"DXT1"
    out = gi._dds_top_mip_dds(bytes(hdr) + body)
    assert out is not None
    assert out[:4] == b"DDS "
    assert out[28:32] == (1).to_bytes(4, "little")     # mipcount forced to 1
    assert out[128:128 + len(mip0)] == mip0            # top mip recovered


def test_lz4_stream_decode_partial():
    import pytest
    pytest.importorskip("lz4")
    from cdumm.archive import paz_crypto
    full = b"HELLO_WORLD_" * 500
    comp = paz_crypto.lz4_compress(full)
    assert gi._lz4_stream_decode(comp, 0, 100) == full[:100]   # stop early


def test_extract_strings_pulls_field_names():
    blob = (b"\x00\x01Sequence\x00\x00_isAccessLock\x00\x04bool"
            b"\x00\xffSequence\x00")
    s = gi.extract_strings(blob, min_len=4)
    assert "Sequence" in s and "_isAccessLock" in s and "bool" in s
    assert s.count("Sequence") == 1                      # de-duplicated
    assert gi.extract_strings(b"\x00\x01ab\x00", min_len=4) == []  # too short
