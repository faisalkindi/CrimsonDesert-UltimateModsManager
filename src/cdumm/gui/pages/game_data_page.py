"""Game Data page — build + browse a searchable catalog of the installed game.

A tool page over ``cdumm.engine.game_index``: one button builds (or refreshes)
a SQLite index of every archive asset + the keyed game-data tables, on a
background thread. You can choose where the index file is saved, open its
folder, and search the indexed assets in a table right in the app.

Strings are literal (not tr() keys) for this first version, so it adds no new
localization keys; localization is a follow-up.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import subprocess
import tempfile

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import (QColor, QFont, QImage, QPixmap, QSyntaxHighlighter,
                           QTextCharFormat)
from PySide6.QtWidgets import (QDialog, QFileDialog, QHBoxLayout, QHeaderView,
                               QLabel, QScrollArea, QSizePolicy, QSplitter,
                               QTableWidgetItem, QVBoxLayout, QWidget)
from qfluentwidgets import TableItemDelegate, isDarkTheme

from cdumm.engine import game_index
from cdumm.gui.pages.tool_page import ToolPageBase
from cdumm.platform import IS_MACOS, IS_WINDOWS

logger = logging.getLogger(__name__)


class _GameIndexWorker(QObject):
    """Runs game_index.build_index off the UI thread."""

    progress = Signal(int, str)
    done = Signal(dict)
    error = Signal(str)

    def __init__(self, game_dir: str, out_path: str) -> None:
        super().__init__()
        self._game_dir = game_dir
        self._out = out_path

    def run(self) -> None:
        try:
            total = max(1, len(game_index.archive_dirs(self._game_dir)))
            state = {"i": 0}

            def cb(archive: str, n: int) -> None:
                state["i"] += 1
                pct = min(99, int(state["i"] * 100 / total))
                self.progress.emit(pct, f"Indexed {archive}  ({n:,} entries)")

            stats = game_index.build_index(
                self._game_dir, self._out, progress=cb)
            self.done.emit(stats)
        except Exception as ex:  # noqa: BLE001 — surface any failure to the UI
            self.error.emit(str(ex))


_GRID_ROW_CAP = 500          # max records rendered in the table-preview grid


def _cell(v) -> str:
    """Stringify one field value for a grid cell (bounded)."""
    if v is None:
        return ""
    s = str(v)
    return s if len(s) <= 200 else s[:200] + "…"


_FILE_TYPE_GUIDE = """A quick guide to Crimson Desert's file types — what you're looking at, and which ones you actually edit to make a mod. Many are Pearl Abyss's own undocumented formats: where a meaning is inferred it's marked "~", and the genuinely opaque ones are named honestly at the bottom rather than guessed at.

━━ THE ONES MODDERS EDIT MOST ━━
.pabgb / .pabgh Game-data TABLES + their key index — items, NPCs (character), quests, skills, drops, gimmicks, spawns, stages. Stats and values live here; most gameplay mods edit these. Opens as a grid of records (the _key and _name columns are always reliable).
.paz The ARCHIVE everything is packed into (like a .zip). The mod manager reads/writes these for you — you rarely touch them by hand.

━━ VISUALS ━━
.dds / .png TEXTURES / images — armour, faces, UI, the world map. .dds opens as an image (with a 3D view too).
.padxil Compiled SHADERS — "DXIL" is DirectX shader bytecode that draws surfaces. ~
.material / .technique / .mi / .pamt Material + shading setup: which shader, and its textures and parameters. ~
.pami / .pam / .pamlod / .pampg Model / MESH data + level-of-detail / streaming variants. ~
.meshinfo Mesh + collision / physics info for an object.
.impostor A flat "billboard" stand-in for a mesh seen from far away. ~
.prefab / .prefabdata_xml A placed "scene object" with its components and transform (and its data as XML).
.spline / .spline2d Curves / paths used to lay out geometry and routes. ~
.ies Light profiles (IES photometry) — how a lamp casts light. ~
.ttf A font file (TrueType).

━━ ANIMATION & COMBAT ━━
.paa ANIMATIONS (Pearl Abyss "PAR" clips) — character/creature motion.
.paa_metabin Metadata that rides alongside an animation.
.paac / .paatt ACTION CHARTS + their attribute blocks — the combat/animation logic (which move plays and its properties). ~
.motionblending How one animation blends into the next. ~
.hkx HAVOK data — ragdoll / physics / some animation (third-party format; shown as a structure outline).

━━ AUDIO ━━
.wem Wwise SOUND streams — SFX, voice, music. Windows can't play them raw; the previewer decodes them with vgmstream so you can hear + export them.
.bnk Wwise SOUNDBANK — a container of sounds + event data.
.pasound A sound definition / reference. ~

━━ VIDEO ━━
.mp4 VIDEO clips — the in-game "advice" tutorials for gear, skills and items. Play them inline (Pause + seek) or extract to a file.

━━ EFFECTS · CUTSCENES · WORLD ━━
.pae Particle / EFFECT data — fire, sparks, auras.
.paseq / .paseqc / .pastage SEQUENCER / quest-STAGE timelines — cutscene + quest logic. .pastage opens as an editable field/type schema. ~
.paproj / .paprojdesc Projectile definitions — arrows, bombs, spells. ~
.palevel / .levelinfo Level / world data. ~
.road / .roadsector / .roadidx Roads + their sector / index data for AI navigation. ~
.paschedule / .paschedulepath NPC daily SCHEDULES + the paths they walk. ~
.binarygimmick Interactive "gimmicks" — levers, doors, traps, physics props. ~
.linkedsceneobject / .questgaugecount Scene-object links and quest counters. ~
.pat / .pbd / .uianiminit Other packed object / UI binaries. No names inside — shown as a typed word table. ~

━━ TEXT & CONFIG ━━
.xml / .pac / .pac_xml / .app_xml / .html / .css / .thtml Human-readable text, config, and UI (.pac is the packed twin of .pac_xml).
.txt / .dat / .binarystring / .paloc Plain text, loose data blobs, packed strings, and localisation. ~

━━ OTHER PEARL ABYSS FORMATS (opaque — not editable by hand) ━━
Value-only packed binaries with no field names inside. The previewer shows them as a typed word table (the raw bytes as unsigned / signed / float) so you can still patch by offset, but their layout isn't documented:
.paccd .imp .parg .seqmt .paem .pabc .save .pab .pabv .pcg .pasg .pashv .papr .ani .pai .pas .pma .paacdesc .paasmt .pamhc .pappt .pathc .paschedulectx .paseqh .binarygimmickcacheddata .binarygimmickframeevent

━━ HOW THE PREVIEW DECIDES WHAT TO SHOW ━━
• Text formats → shown as text.
• Textures → shown as an image (+ 3D).
• Data tables (.pabgb) → a record grid.
• Reflection formats (.pae / .paseq / .pastage / .prefab / .meshinfo …) → a Field → Type schema table, using the engine's OWN names.
• Audio (.wem / .bnk) → metadata + Play / Export-to-WAV.
• Video (.mp4) → plays inline with Pause + seek.
• Everything else (packed formats like .paatt / .paa / .pabgh / .pat / .pbd / .uianiminit) → a typed word table: the exact bytes shown as unsigned / signed / float. These formats carry no field names inside the file, so the values are shown accurately as raw numbers you can still patch by offset.

Tip: a name ending in "info" is almost always a game-data table you can edit."""


_MOD_HOWTO_GUIDE = """How to turn game data into a mod — no hex editor needed.

1. Build the index (top of this tab), then click a keyed data table
   (.pabgb) in the results — e.g. iteminfo for items/gear, storeinfo
   for shops. It opens as a grid of records.

2. Edit a value. Cells CDUMM has verified byte-exact are editable —
   double-click one (for example an item's price) and type a new
   number. Un-verified fields stay locked so you can't corrupt them.

3. Click "Make mod from edits…" to build the mod, or
   "Export .field.json…" to save a shareable copy.

4. Enable the new mod in the Mods tab, then Apply.

Every edit is a same-width, byte-exact write: the file stays valid,
the game still loads it, and you can disable the mod to revert."""


class _ExtHighlighter(QSyntaxHighlighter):
    """Colour the file-type tokens (.dds, .pabgb, .pac_xml, ...) in the
    "New to modding" guides so each entry reads as a scannable line rather
    than one wall of grey text. The colour tracks the theme accent and
    updates live when the accent picker changes (same bus as the buttons)."""

    _RX = re.compile(r"\.[A-Za-z][A-Za-z0-9_]+")

    def __init__(self, document) -> None:
        super().__init__(document)
        self._fmt = QTextCharFormat()
        self._fmt.setFontWeight(QFont.Weight.Bold)
        self._apply_accent()
        try:   # the accent module ships in a separate PR; degrade if absent
            from cdumm.gui import accent
            accent.bus().changed.connect(self._on_accent)
        except Exception:
            pass

    def _apply_accent(self) -> None:
        try:
            from cdumm.gui import accent
            self._fmt.setForeground(accent.current_accent())
        except Exception:   # no accent module in this build -> brand blue
            self._fmt.setForeground(QColor("#2878D0"))

    def _on_accent(self) -> None:
        self._apply_accent()
        self.rehighlight()

    def highlightBlock(self, text: str) -> None:  # noqa: N802
        for m in self._RX.finditer(text):
            self.setFormat(m.start(), m.end() - m.start(), self._fmt)


def _human_size(n) -> str:
    """Bytes -> a short human string (e.g. ``512 B``, ``763 KB``, ``4.8 MB``)."""
    size = float(int(n))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.0f} {unit}" if size >= 100 else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{int(n)} B"


# Broad file-type category -> colour, so the Type column reads at a glance
# (audio vs texture vs table ...). Extensions not listed stay the default.
_CAT_COLOUR = {
    "table": "#5AD19A", "visual": "#4FA3FF", "anim": "#E0A85E",
    "audio": "#B98BE0", "video": "#E06A8B", "world": "#4FC9C9", "text": "#9AA0A6",
}
_EXT_CAT = {
    ".pabgb": "table", ".pabgh": "table",
    ".wem": "audio", ".bnk": "audio", ".pasound": "audio",
    ".mp4": "video",
    ".dds": "visual", ".png": "visual", ".padxil": "visual", ".material": "visual",
    ".technique": "visual", ".mi": "visual", ".pamt": "visual", ".pami": "visual",
    ".pam": "visual", ".pamlod": "visual", ".pampg": "visual", ".meshinfo": "visual",
    ".prefab": "visual", ".prefabdata_xml": "visual", ".spline": "visual",
    ".spline2d": "visual", ".ies": "visual", ".ttf": "visual", ".impostor": "visual",
    ".paa": "anim", ".paa_metabin": "anim", ".paac": "anim", ".paatt": "anim",
    ".hkx": "anim", ".motionblending": "anim",
    ".pae": "world", ".paseq": "world", ".paseqc": "world", ".pastage": "world",
    ".paproj": "world", ".palevel": "world", ".levelinfo": "world", ".road": "world",
    ".roadsector": "world", ".roadidx": "world", ".paschedule": "world",
    ".paschedulepath": "world", ".binarygimmick": "world", ".pat": "world",
    ".pbd": "world", ".uianiminit": "world",
    ".xml": "text", ".pac": "text", ".pac_xml": "text", ".app_xml": "text",
    ".html": "text", ".css": "text", ".thtml": "text", ".txt": "text",
    ".dat": "text", ".binarystring": "text", ".paloc": "text",
}


def _type_colour(ext: str) -> "str | None":
    """Hex colour for a file-type's category, or None if uncategorised."""
    return _CAT_COLOUR.get(_EXT_CAT.get((ext or "").lower()))


def _split_path(path: str) -> "tuple[str, str]":
    """Return ``(folder, name)`` from a game asset path (handles / and \\)."""
    sep = max(path.rfind("/"), path.rfind("\\"))
    return (path[:sep], path[sep + 1:]) if sep >= 0 else ("", path)


class _NumericItem(QTableWidgetItem):
    """Table cell that SORTS by a numeric key while DISPLAYING formatted text,
    so the Size column sorts by real byte count (not by its "763 KB" text)."""

    def __init__(self, text: str, value: float) -> None:
        super().__init__(text)
        self._value = value

    def __lt__(self, other) -> bool:
        if isinstance(other, _NumericItem):
            return self._value < other._value
        return super().__lt__(other)


class _ZebraDelegate(TableItemDelegate):
    """Stronger alternating-row shading. qfluentwidgets' stock alternate band
    is alpha 5/255 (near-invisible); pre-fill odd, non-selected rows with a
    firmer translucent band, then let the base delegate paint hover / selection
    / content on top. Keyed on the VISUAL row so it survives sorting, and it
    skips selected rows so the selection highlight still shows."""

    def paint(self, painter, option, index):  # noqa: N802
        if (index.row() % 2) and index.row() not in self.selectedRows:
            painter.save()
            _c = 255 if isDarkTheme() else 0
            painter.fillRect(option.rect, QColor(_c, _c, _c, 16))
            painter.restore()
        super().paint(painter, option, index)


def _build_row_items(r) -> list:
    """Build the five cells for one search result: Name (full path stashed in
    UserRole for selection) - Folder (dimmed) - Archive - Type (category-
    coloured) - Size (human text, numeric sort key, right-aligned, exact bytes
    in the tooltip)."""
    path = str(r["path"])
    folder, name = _split_path(path)
    ext = str(r["ext"])
    size = int(r["orig_size"])

    name_item = QTableWidgetItem(name)
    name_item.setData(Qt.ItemDataRole.UserRole, path)
    name_item.setToolTip(path)

    folder_item = QTableWidgetItem(folder)
    folder_item.setForeground(QColor("#9AA0A6"))
    folder_item.setToolTip(path)

    archive_item = QTableWidgetItem(str(r["archive"]))

    type_item = QTableWidgetItem(ext)
    type_item.setToolTip(ext)
    _tc = _type_colour(ext)
    if _tc:
        # Colour only — do NOT override the item font. A standalone item's
        # font() is the default (not the table's 15px), so bolding it here
        # rendered Type larger/heavier than every other column.
        type_item.setForeground(QColor(_tc))

    size_item = _NumericItem(_human_size(size), size)
    size_item.setTextAlignment(
        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    size_item.setToolTip(f"{size:,} bytes")

    return [name_item, folder_item, archive_item, type_item, size_item]


def _shape_records(records: dict, schema, positions: dict | None = None
                   ) -> tuple[list, list, int, float]:
    """Turn parse_records output into (columns, rows, total, health).

    Columns are ``_key`` + ``_name`` + the schema's field names in order;
    rows are stringified and capped to the first ``_GRID_ROW_CAP`` by key.
    ``health`` is the fraction of schema-field columns whose sampled values
    look unusable — constant, all-zero/None, or simply mirroring the key —
    i.e. a signal that CDUMM's patch-oriented parser mis-read this table's
    fields (its job is diffing, not a clean human dump).
    """
    field_names = [f.name for f in schema.fields] if schema else []
    # _key and _name are shown as their own leading columns; several tables
    # (e.g. sequencerspawninfo) also list _key/_name among their schema
    # fields, which would render a redundant duplicate column — drop those.
    field_names = [f for f in field_names if f not in ("_key", "_name")]
    # Verified-only gate: for a hand-curated table, only fields the author
    # validated against real record data are shown decoded; the rest render
    # `(unverified)` so a guessed byte never masquerades as fact.
    verified = getattr(schema, "verified_fields", None) if schema else None

    # Mask fields the decoder can't trust. A field is an "unknown-width
    # placeholder" when it has no struct format, no walker type descriptor, and
    # isn't a CString — i.e. an upstream `direct_15B` / `reader_*` / complex
    # field whose real size is unknown. The decoder reads it left-to-right, so
    # the FIRST such field (wrong width) misaligns every field after it too.
    # Show fields up to that point; render it and everything after
    # `(unverified)` rather than present misleading bytes. (Tables with a
    # hand-built override have real type descriptors, so nothing trips this.)
    masked = set()
    if schema:
        hit = False
        for f in schema.fields:
            if f.name in ("_key", "_name"):
                continue          # metadata from the index/header, always shown
            if not hit and hasattr(f, "field_type") and (
                    not getattr(f, "struct_fmt", None)
                    and not getattr(f, "type_descriptor", None)
                    and f.field_type != "CString"):
                hit = True
            if hit:
                masked.add(f.name)

    def _fieldval(k, c):
        if (verified is not None and c not in verified) or c in masked:
            return "(unverified)"
        return _cell(records[k].get(c))

    pos_col = "world pos (X, Y, Z)"
    cols = ["_key", "_name"] + ([pos_col] if positions else []) + field_names
    keys = sorted(records)[:_GRID_ROW_CAP]

    def _posval(k):
        p = positions.get(k) if positions else None
        return f"{p[0]:.1f}, {p[1]:.1f}, {p[2]:.1f}" if p else ""

    rows = []
    for k in keys:
        row = [_cell(records[k].get("_key")), _cell(records[k].get("_name"))]
        if positions:
            row.append(_posval(k))
        row += [_fieldval(k, c) for c in field_names]
        rows.append(row)

    suspect = 0
    # Unverified fields are intentionally not decoded — exclude them from the
    # health score so a curated table isn't penalised for hiding guesses.
    scored_fields = [f for f in field_names
                     if (verified is None or f in verified) and f not in masked]
    sample = keys[:60]
    for fn in scored_fields:
        vals = [records[k].get(fn) for k in sample]
        sv = [str(v) for v in vals]
        const = len(set(sv)) == 1
        zero = all(s in ("", "None") or set(s) <= set("0") for s in sv)
        seq = False
        try:
            iv = [int(v) for v in vals]
            seq = len(iv) > 2 and all(
                (iv[i] - iv[0]) == (sample[i] - sample[0])
                for i in range(len(iv)))
        except (TypeError, ValueError):
            pass
        if const or zero or seq:
            suspect += 1
    health = (suspect / len(scored_fields)) if scored_fields else 0.0
    return cols, rows, len(records), health


def _locate_gear_stats(table: str, body: bytes, header: bytes) -> dict:
    """``{item_key: [GearStat, ...]}`` for iteminfo, ``{}`` for anything else.

    Gear stats (armour defence, weapon damage, enhancement values) live in
    nested blocks that the display decoder flattens away, so they get no
    grid columns and have to be edited through their own dialog. They come
    from the native 1.13 decode — every stat at its exact path, nothing
    scanned or inferred.

    Best-effort: a table view must never fail because the stat locator did.
    """
    if table != "iteminfo":
        return {}
    try:
        from cdumm.engine.gear_stat_view import locate_all_gear_stats
        from cdumm.engine.iteminfo_native_parser import (
            detect_iteminfo_layout, parse_iteminfo_from_bytes)
        n = int.from_bytes(header[:2], "little")
        starts = sorted(
            int.from_bytes(header[2 + i * 8 + 4:2 + i * 8 + 8], "little")
            for i in range(n))
        items = parse_iteminfo_from_bytes(
            body, starts, fields=detect_iteminfo_layout(body, starts))
        return locate_all_gear_stats(
            {it["key"]: it for it in items if "key" in it})
    except Exception:  # noqa: BLE001 — never break the table view
        logger.exception("gear-stat locator failed on %s", table)
        return {}


class _PreviewWorker(QObject):
    """Reads + decodes one asset off the UI thread so a large table or
    texture can never freeze the app. Emits exactly one ``ready`` dict
    carrying its generation id (stale results are ignored by the page)."""

    ready = Signal(dict)

    def __init__(self, index_path, game_dir, path, gen,
                 byte_limit, table_limit, image_limit, text_cap, hex_cap):
        super().__init__()
        self._index_path = index_path
        self._game_dir = game_dir
        self._path = path
        self._gen = gen
        self._byte_limit = byte_limit
        self._table_limit = table_limit
        self._image_limit = image_limit
        self._text_cap = text_cap
        self._hex_cap = hex_cap
        self._video_limit = 256 * 1024 * 1024  # mp4 clips are small; cap anyway

    def run(self) -> None:
        res = {"gen": self._gen, "path": self._path}
        try:
            con = sqlite3.connect(self._index_path)
            try:
                self._work(con, res)
            finally:
                con.close()
        except Exception as ex:  # noqa: BLE001 — surface to the pane
            res.update(kind="error", error=str(ex))
        self.ready.emit(res)

    def _work(self, con, res: dict) -> None:
        row = game_index.get_asset(con, self._path)
        if row is None:
            res.update(kind="error", error="asset not in index")
            return
        res["row"] = {k: row[k] for k in
                      ("ext", "orig_size", "archive", "compressed", "encrypted")}
        orig = int(row["orig_size"])
        gd = self._game_dir

        # 1) keyed game-data table → parsed grid (CDUMM's semantic schemas)
        if gd and self._path.endswith(".pabgb"):
            try:
                from cdumm.semantic import parser as sem
                table = sem.identify_table_from_path(self._path)
            except Exception:  # noqa: BLE001
                table = None
            if table:
                if orig > self._table_limit:
                    res.update(kind="toobig")
                    return
                try:
                    body = game_index.extract_asset(con, self._path, gd)
                    header = game_index.extract_asset(
                        con, self._path[:-6] + ".pabgh", gd)
                    # Display-only decoder: honors override flags + walks
                    # variable-length fields so richly-schema'd tables
                    # (iteminfo, regioninfo, ...) show their real columns.
                    recs = sem.parse_records_display(table, body, header)
                except Exception:  # noqa: BLE001 — fall back to a raw view
                    recs = {}
                if recs:
                    positions = game_index.decode_table_positions(
                        table, body, header)
                    cols, rows, total, health = _shape_records(
                        recs, sem.get_schema(table), positions)
                    res.update(kind="table", table=table, cols=cols,
                               rows=rows, total=total, health=health,
                               has_pos=bool(positions),
                               gear_stats=_locate_gear_stats(
                                   table, body, header))
                    return

        # 2) no game folder → metadata only
        if not gd:
            res.update(kind="meta")
            return

        # 3) image asset (chiefly DDS textures) → rendered PNG for the pane
        if self._path.lower().endswith(game_index._IMAGE_EXTS):
            if orig > self._image_limit:
                res.update(kind="toobig")
                return
            data = game_index.extract_asset(con, self._path, gd)
            img = game_index.decode_image(data, self._path)
            if img:
                res.update(kind="image", img=img)
            else:  # unsupported codec (e.g. BC7) → show the header bytes
                res.update(kind="hex",
                           text=game_index.hexdump(data, limit=self._hex_cap))
            return

        # 3.5) Wwise audio (.wem streams / .bnk soundbanks) → metadata + play
        if self._path.lower().endswith(game_index.WWISE_EXTS):
            if orig > self._image_limit:
                res.update(kind="toobig")
                return
            data = game_index.extract_asset(con, self._path, gd)
            audio = game_index.decode_audio(data, self._path)
            if audio:
                res.update(kind="audio", audio=audio,
                           vgmstream=bool(game_index.find_vgmstream()))
                return

        # 3.6) Video clips (.mp4) → play inline via Qt Multimedia.
        if self._path.lower().endswith(game_index.VIDEO_EXTS):
            if orig > self._video_limit:
                res.update(kind="toobig")
                return
            data = game_index.extract_asset(con, self._path, gd)
            res.update(kind="video", data=data)
            return

        # 4) too big for an inline byte preview
        if orig > self._byte_limit:
            res.update(kind="toobig")
            return
        # 5) text, structure outline, or hex
        data = game_index.extract_asset(con, self._path, gd)
        text = game_index.decode_text(data, limit=self._text_cap)
        if text is not None:
            res.update(kind="text", text=text[:self._text_cap],
                       truncated=len(text) >= self._text_cap)
            return
        # Verbose reflection binaries (.pae/.paseq/.prefab/.meshinfo/…) embed a
        # full field→type schema; surface it as a named table.
        refl = game_index.decode_reflection(data)
        if refl:
            res.update(kind="schema", **refl)
            return
        # The flat name outline is only meaningful for those same verbose
        # reflection formats. Packed value-only files (.pabgh/.paatt/…) and
        # third-party binaries (.hkx, .roadsector, …) embed no name schema, so
        # mining them yields misleading noise ("navigraphX" repeats, Havok type
        # tags) — route everything else to the struct/hex view instead.
        if game_index.ext_of(self._path) in game_index.REFLECTION_EXTS:
            strings = game_index.extract_strings(data)
            if len(strings) >= 6:
                res.update(kind="outline", strings=strings, nstr=len(strings))
                return
        # No embedded names, but a word-aligned struct (.paatt attribute
        # blocks, .pabgh key indexes) reads far better as a typed word
        # table than a raw hex wall.
        st = game_index.decode_struct(data)
        if st:
            res.update(kind="struct", **st)
        else:
            res.update(kind="hex",
                       text=game_index.hexdump(data, limit=self._hex_cap))


class _VgmDownloadWorker(QObject):
    """Downloads + installs the right vgmstream build off the UI thread so the
    one-click 'Enable audio playback' can't freeze the app."""

    done = Signal(bool, str)

    def __init__(self, dest_dir: str):
        super().__init__()
        self._dest = dest_dir

    def run(self) -> None:
        try:
            ok, msg = game_index.download_vgmstream(self._dest)
        except Exception as ex:  # noqa: BLE001
            ok, msg = False, str(ex)
        self.done.emit(ok, msg)


class _AudioDecodeWorker(QObject):
    """Decode a Wwise .wem to a temp WAV via vgmstream off the UI thread.

    vgmstream runs as a subprocess; doing that inline froze the window
    ('not responding') until it finished. This runs it on a worker thread and
    hands back the WAV path + real duration so the pane can play it and show a
    live timer.
    """

    done = Signal(dict)   # {token, wav, dur, name} on success; {token, error}

    def __init__(self, index_path, game_dir, path, data, name, token):
        super().__init__()
        self._index_path = index_path
        self._game_dir = game_dir
        self._path = path
        self._data = data
        self._name = name
        self._token = token

    def run(self) -> None:
        import contextlib
        import tempfile
        import wave
        res = {"token": self._token, "name": self._name}
        data = self._data
        try:
            if data is None:                       # large asset — read on demand
                con = sqlite3.connect(self._index_path)
                try:
                    data = game_index.extract_asset(
                        con, self._path, self._game_dir)
                finally:
                    con.close()
            fd, wav = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            if not game_index.convert_to_wav(data, wav):
                with contextlib.suppress(OSError):
                    os.unlink(wav)
                res["error"] = ("Could not decode audio — is vgmstream "
                                "installed?")
                self.done.emit(res)
                return
            dur = 0.0
            with contextlib.suppress(Exception):
                with contextlib.closing(wave.open(wav, "rb")) as w:
                    if w.getframerate():
                        dur = w.getnframes() / float(w.getframerate())
            res["wav"] = wav
            res["dur"] = dur
        except Exception as ex:  # noqa: BLE001
            res["error"] = f"Read/decode failed: {ex}"
        self.done.emit(res)


class _Texture3DView(QDialog):
    """Pop-up 3D material preview: the selected texture on a flat plane,
    sphere, or cube. Built with Qt3D, constructed lazily (Qt3D is only
    imported when the user opens a 3D preview), and the caller wraps
    construction in try/except so a GPU/driver problem can't crash the app.

    A flat plane is how billboard / decal / UI textures appear in game;
    solid game meshes aren't attainable (the mesh formats are proprietary),
    so the sphere/cube are for inspecting how a texture wraps and tiles."""

    def __init__(self, qimage, title="Texture — 3D preview", parent=None):
        super().__init__(parent)
        # PySide6 6.10 nests the Qt3D classes under a same-named namespace.
        from PySide6.Qt3DExtras import Qt3DExtras as _E
        from PySide6.Qt3DCore import Qt3DCore as _C
        from PySide6.Qt3DRender import Qt3DRender as _R
        from PySide6.QtGui import QVector3D, QColor
        from PySide6.QtCore import QUrl
        from qfluentwidgets import CaptionLabel, PushButton

        self.setWindowTitle(title)
        self.resize(760, 700)
        # A QDialog shows only a close button; make it a normal OS window with
        # minimize / maximize / close like any other program.
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint)

        # Most portable way to get a QImage into a Qt3D texture across PySide6
        # builds: a temp PNG loaded by QTextureLoader (a painted-texture
        # subclass rendered nothing on this build).
        fd, self._tmp_png = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        qimage.save(self._tmp_png, "PNG")

        self._window = _E.Qt3DWindow()
        self._window.defaultFrameGraph().setClearColor(QColor("#20222E"))
        self._window.installEventFilter(self)     # middle-mouse orbit
        container = QWidget.createWindowContainer(self._window, self)
        container.setMinimumSize(420, 420)
        container.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(8, 8, 8, 8)
        root_layout.setSpacing(8)
        bar = QHBoxLayout()
        for label, shape in (("Plane", "plane"), ("Sphere", "sphere"),
                             ("Cube", "cube")):
            btn = PushButton(label, self)
            btn.clicked.connect(lambda _=False, s=shape: self._set_shape(s))
            bar.addWidget(btn)
        bar.addStretch(1)
        bar.addWidget(CaptionLabel(
            "Left- or middle-drag to orbit · scroll to zoom", self))
        root_layout.addLayout(bar)
        root_layout.addWidget(container, 1)

        self._root = _C.QEntity()
        self._cam = self._window.camera()
        self._cam.lens().setPerspectiveProjection(45.0, 1.2, 0.05, 1000.0)
        # Unreal-style orbit: the camera revolves around a FIXED pivot at the
        # origin (where the shape sits) at a controllable distance, so the asset
        # stays centred and can never translate off-screen. Drag = orbit,
        # wheel = dolly. Driven by hand — QOrbitCameraController was removed
        # because it also pans the view centre, which let the asset fly out of
        # frame and vanish at the window edges (the reported bug).
        self._azimuth = 0.0        # degrees, around the vertical axis
        self._elevation = 0.0      # degrees, up/down (kept off the poles)
        self._distance = 3.2       # camera distance from the pivot
        self._orbit_last = None
        self._update_camera()

        # Unlit texture material — shows the texture at full brightness with
        # no lighting dependency, so the shape is always visible.
        tex = _R.QTextureLoader(self._root)
        tex.setSource(QUrl.fromLocalFile(self._tmp_png))
        mat = _E.QTextureMaterial(self._root)
        mat.setTexture(tex)

        # Keep Python references to every Qt3D node. PySide6 garbage-collects
        # unparented meshes / transforms / materials otherwise, destroying the
        # component and leaving the scene empty — the bug that made earlier
        # attempts render nothing. Parenting each mesh to its entity + holding
        # them in a list guarantees they survive.
        self._keep = [tex, mat]
        self._shapes = {}
        # Plane — a flat card (thin cuboid, visible from both sides).
        pe = _C.QEntity(self._root)
        pmesh = _E.QCuboidMesh(pe)
        ptx = _C.QTransform(pe)
        ptx.setScale3D(QVector3D(2.0, 2.0, 0.03))
        pe.addComponent(pmesh)
        pe.addComponent(mat)
        pe.addComponent(ptx)
        self._shapes["plane"] = pe
        self._keep += [pmesh, ptx]
        # Sphere
        se = _C.QEntity(self._root)
        smesh = _E.QSphereMesh(se)
        smesh.setRadius(1.2)
        smesh.setRings(60)
        smesh.setSlices(60)
        se.addComponent(smesh)
        se.addComponent(mat)
        self._shapes["sphere"] = se
        self._keep.append(smesh)
        # Cube
        ce = _C.QEntity(self._root)
        cmesh = _E.QCuboidMesh(ce)
        ce.addComponent(cmesh)
        ce.addComponent(mat)
        self._shapes["cube"] = ce
        self._keep.append(cmesh)

        self._window.setRootEntity(self._root)
        self._set_shape("plane")

    def _update_camera(self):
        """Position the camera on a sphere around the origin from the current
        azimuth / elevation / distance. The view centre is pinned to the
        origin, so the asset is always framed no matter how you spin it."""
        import math
        from PySide6.QtGui import QVector3D
        self._elevation = max(-89.0, min(89.0, self._elevation))
        a = math.radians(self._azimuth)
        e = math.radians(self._elevation)
        cos_e = math.cos(e)
        pos = QVector3D(self._distance * cos_e * math.sin(a),
                        self._distance * math.sin(e),
                        self._distance * cos_e * math.cos(a))
        self._cam.setViewCenter(QVector3D(0, 0, 0))
        self._cam.setPosition(pos)
        self._cam.setUpVector(QVector3D(0, 1, 0))

    def eventFilter(self, obj, event):  # noqa: N802
        """Drag (left or middle button) orbits the camera around the fixed
        pivot; the wheel dollies in and out. Same feel as an Unreal viewport —
        the asset stays put and you move the camera around it."""
        from PySide6.QtCore import QEvent
        t = event.type()
        _orbit_btns = (Qt.MouseButton.LeftButton, Qt.MouseButton.MiddleButton)
        if (t == QEvent.Type.MouseButtonPress
                and event.button() in _orbit_btns):
            self._orbit_last = event.position()
            return True
        if t == QEvent.Type.MouseMove and self._orbit_last is not None:
            p = event.position()
            self._azimuth -= (p.x() - self._orbit_last.x()) * 0.3
            self._elevation -= (p.y() - self._orbit_last.y()) * 0.3
            self._orbit_last = p
            self._update_camera()
            return True
        if (t == QEvent.Type.MouseButtonRelease
                and event.button() in _orbit_btns):
            self._orbit_last = None
            return True
        if t == QEvent.Type.Wheel:
            # positive delta = wheel up = zoom in (smaller distance)
            step = 0.88 if event.angleDelta().y() > 0 else 1.0 / 0.88
            self._distance = max(1.5, min(40.0, self._distance * step))
            self._update_camera()
            return True
        return super().eventFilter(obj, event)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        try:
            w = max(1, self._window.width())
            h = max(1, self._window.height())
            self._cam.lens().setPerspectiveProjection(45.0, w / h, 0.05, 1000.0)
        except Exception:  # noqa: BLE001
            pass

    def _set_shape(self, which: str) -> None:
        for name, ent in self._shapes.items():
            ent.setEnabled(name == which)

    def closeEvent(self, event):  # noqa: N802
        try:
            if getattr(self, "_tmp_png", None) and os.path.exists(self._tmp_png):
                os.remove(self._tmp_png)
        except Exception:  # noqa: BLE001
            pass
        super().closeEvent(event)


class GameDataPage(ToolPageBase):
    """Build, locate, and search an index of the installed game's data."""

    def __init__(self, parent=None) -> None:
        super().__init__(
            object_name="GameDataPage",
            title="Game Data",
            description=(
                "Build a searchable catalog of this Crimson Desert install — "
                "every asset the game ships plus the keyed game-data tables "
                "(items, NPCs, quests, skills, drops, ...). Click any result "
                "to preview it on the right — text formats show as text, "
                "everything else as hex + metadata — or extract it to disk."),
            run_label="Build / refresh game-data index",
            parent=parent,
        )
        self._app_data_dir = None
        self._index_path = os.path.join(
            tempfile.gettempdir(), "cdumm_game_index.sqlite")
        self._thread: QThread | None = None
        self._worker: _GameIndexWorker | None = None

        self._stat_assets = self._add_stat_card("--", "Assets", "#2878D0")
        self._stat_archives = self._add_stat_card("--", "Archives", "#8B5CF6")
        self._stat_tables = self._add_stat_card("--", "Data tables", "#0EA5E9")

        self._build_controls()

    # ── extra controls (persistent — not wiped by _clear_results) ────
    def _build_controls(self) -> None:
        from qfluentwidgets import (BodyLabel, CaptionLabel, ComboBox, LineEdit,
                                     PlainTextEdit, PushButton,
                                     StrongBodyLabel, TableWidget)
        root = self._container.layout()

        # Save-location row: path label + Change + Open folder
        loc_row = QHBoxLayout()
        loc_row.setSpacing(8)
        self._path_label = CaptionLabel(
            f"Save location:  {self._index_path}", self._container)
        self._path_label.setWordWrap(True)
        loc_row.addWidget(self._path_label)
        loc_row.addSpacing(12)
        self._change_btn = PushButton("Change…", self._container)
        self._change_btn.clicked.connect(self._choose_location)
        loc_row.addWidget(self._change_btn)
        self._open_btn = PushButton("Open folder", self._container)
        self._open_btn.clicked.connect(self._open_location)
        loc_row.addWidget(self._open_btn)
        loc_row.addStretch(1)   # keep the buttons grouped by the path, not far right
        root.insertLayout(root.count() - 1, loc_row)
        root.insertSpacing(root.count() - 1, 16)

        # Search box — capped width + left-aligned (it doesn't need to span
        # the whole page), so it also stops crowding the buttons above it.
        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        self._search = LineEdit(self._container)
        self._search.setPlaceholderText(
            "Search assets by path (build the index first)…")
        self._search.setClearButtonEnabled(True)
        self._search.setFixedHeight(38)
        self._search.setMaximumWidth(480)
        _sf = self._search.font()
        _sf.setPixelSize(15)
        self._search.setFont(_sf)
        self._search.textChanged.connect(self._on_search)
        search_row.addWidget(self._search)
        # File-type filter — narrow results to one extension (e.g. .dds). The
        # list is curated until the index is built, then repopulated from the
        # actual extensions present (with counts) in _populate_type_filter().
        _type_lbl = CaptionLabel("Type", self._container)
        _type_lbl.setContentsMargins(14, 0, 6, 0)
        search_row.addWidget(_type_lbl)
        self._type_combo = ComboBox(self._container)
        self._type_combo.setFixedHeight(38)
        self._type_combo.setMinimumWidth(150)
        self._type_exts: list[str | None] = []
        self._set_type_items(self._DEFAULT_TYPE_FILTERS)
        self._type_combo.currentIndexChanged.connect(
            lambda _i: self._on_search(self._search.text()))
        search_row.addWidget(self._type_combo)
        _show_lbl = CaptionLabel("Show", self._container)
        _show_lbl.setContentsMargins(14, 0, 6, 0)
        search_row.addWidget(_show_lbl)
        self._limit_combo = ComboBox(self._container)
        self._limit_combo.addItems([f"{n:,}" for n in self._LIMIT_OPTIONS])
        self._limit_combo.setCurrentIndex(1)          # default 300 (unchanged)
        self._limit_combo.setFixedHeight(38)
        self._limit_combo.setMinimumWidth(104)
        self._limit_combo.currentIndexChanged.connect(
            lambda _i: self._on_search(self._search.text()))
        search_row.addWidget(self._limit_combo)
        search_row.addStretch(1)
        root.insertLayout(root.count() - 1, search_row)

        self._hits = BodyLabel("", self._container)
        self._hits.setContentsMargins(2, 4, 0, 0)
        _hitf = self._hits.font()
        _hitf.setPixelSize(15)
        self._hits.setFont(_hitf)
        root.insertWidget(root.count() - 1, self._hits)
        root.insertSpacing(root.count() - 1, 8)

        # ── Modding info row — three columns side by side ────────────────
        # The "Largest keyed game-data tables" summary (filled after a build)
        # sits next to two collapsible guides: what the file types mean, and
        # how to turn game data into a mod. Grouping them here keeps the
        # newcomer help beside the thing it explains.
        info_row = QHBoxLayout()
        info_row.setContentsMargins(0, 0, 0, 0)
        info_row.setSpacing(12)

        # Column 1 — the largest-tables card lands here in _on_done().
        self._largest_col = QVBoxLayout()
        self._largest_col.setContentsMargins(0, 0, 0, 0)
        self._largest_col.setSpacing(0)
        info_row.addLayout(self._largest_col, 3)

        # Column 2 — "New to modding? What these file types mean".
        types_col = QVBoxLayout()
        types_col.setContentsMargins(0, 0, 0, 0)
        types_col.setSpacing(6)
        self._guide_btn = PushButton(
            "📖  New to modding?  What these file types mean", self._container)
        self._guide_btn.setCheckable(True)
        self._guide_btn.clicked.connect(
            lambda: self._guide_box.setVisible(self._guide_btn.isChecked()))
        types_col.addWidget(self._guide_btn)
        self._guide_box = PlainTextEdit(self._container)
        self._guide_box.setReadOnly(True)
        self._guide_box.setPlainText(_FILE_TYPE_GUIDE)
        self._guide_hl = _ExtHighlighter(self._guide_box.document())
        _gbf = self._guide_box.font()
        _gbf.setPixelSize(13)
        self._guide_box.setFont(_gbf)
        self._guide_box.setFixedHeight(300)
        self._guide_box.setVisible(False)
        types_col.addWidget(self._guide_box)
        types_col.addStretch(1)
        info_row.addLayout(types_col, 4)

        # Column 3 — "How to make a mod from game data" (the same collapsible
        # pattern, so the two guides read as a pair).
        howto_col = QVBoxLayout()
        howto_col.setContentsMargins(0, 0, 0, 0)
        howto_col.setSpacing(6)
        self._howto_btn = PushButton(
            "🛠  How to make a mod from game data", self._container)
        self._howto_btn.setCheckable(True)
        self._howto_btn.clicked.connect(
            lambda: self._howto_box.setVisible(self._howto_btn.isChecked()))
        howto_col.addWidget(self._howto_btn)
        self._howto_box = PlainTextEdit(self._container)
        self._howto_box.setReadOnly(True)
        self._howto_box.setPlainText(_MOD_HOWTO_GUIDE)
        self._howto_hl = _ExtHighlighter(self._howto_box.document())
        _hbf = self._howto_box.font()
        _hbf.setPixelSize(13)
        self._howto_box.setFont(_hbf)
        self._howto_box.setFixedHeight(300)
        self._howto_box.setVisible(False)
        howto_col.addWidget(self._howto_box)
        howto_col.addStretch(1)
        info_row.addLayout(howto_col, 4)

        root.insertLayout(root.count() - 1, info_row)
        root.insertSpacing(root.count() - 1, 8)

        # Results table (left) + live preview pane (right), in a draggable
        # splitter. It's given stretch=1 (below) + an Expanding policy so it
        # fills the page down to the bottom instead of sitting short with a
        # big dead zone beneath it.
        split = QSplitter(Qt.Horizontal, self._container)
        split.setChildrenCollapsible(False)
        split.setMinimumHeight(460)
        split.setSizePolicy(QSizePolicy.Policy.Expanding,
                            QSizePolicy.Policy.Expanding)

        self._table = TableWidget(split)
        self._table.setColumnCount(5)
        self._table.setHorizontalHeaderLabels(
            ["Name", "Folder", "Archive", "Type", "Size"])
        self._table.verticalHeader().hide()
        self._table.setEditTriggers(self._table.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(
            self._table.SelectionBehavior.SelectRows)
        self._table.setSortingEnabled(True)          # click a header to sort
        self._table.setAlternatingRowColors(True)    # zebra (via _ZebraDelegate)
        self._table.setItemDelegate(_ZebraDelegate(self._table))
        self._table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_table_menu)
        # Reserve a gutter for the vertical scrollbar so it sits beside the
        # rows instead of overlaying the last column (which caused mis-clicks
        # landing on a row instead of the scrollbar).
        self._table.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        # Larger, more readable text + roomier rows.
        _tf = self._table.font()
        _tf.setPixelSize(15)
        self._table.setFont(_tf)
        _hdr = self._table.horizontalHeader()
        _hf = _hdr.font()
        _hf.setPixelSize(15)
        _hdr.setFont(_hf)
        # Folder stretches to fill the slack; the rest get fixed widths, so the
        # columns still span the full width (scrollbar hugs the last column)
        # WITHOUT ResizeToContents, which re-measures every row and can stall.
        _hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for _c in (0, 2, 3, 4):
            _hdr.setSectionResizeMode(_c, QHeaderView.ResizeMode.Interactive)
        self._table.setColumnWidth(0, 260)   # Name
        self._table.setColumnWidth(2, 80)    # Archive
        self._table.setColumnWidth(3, 120)   # Type
        self._table.setColumnWidth(4, 110)   # Size
        self._table.verticalHeader().setDefaultSectionSize(36)
        self._table.itemSelectionChanged.connect(self._on_asset_selected)
        split.addWidget(self._table)

        # Preview pane: title + metadata + monospace text/hex view + extract.
        pane = QWidget(split)
        pv = QVBoxLayout(pane)
        pv.setContentsMargins(10, 0, 0, 0)
        pv.setSpacing(6)
        self._pv_title = StrongBodyLabel("Select an asset to preview", pane)
        _ptf = self._pv_title.font()
        _ptf.setPixelSize(16)
        self._pv_title.setFont(_ptf)
        pv.addWidget(self._pv_title)
        self._pv_meta = CaptionLabel("", pane)
        self._pv_meta.setWordWrap(True)
        pv.addWidget(self._pv_meta)
        self._pv_text = PlainTextEdit(pane)
        self._pv_text.setReadOnly(True)
        self._pv_text.setLineWrapMode(PlainTextEdit.LineWrapMode.NoWrap)
        _mf = self._pv_text.font()
        _mf.setFamily("Consolas")
        _mf.setStyleHint(_mf.StyleHint.Monospace)
        _mf.setPixelSize(13)
        self._pv_text.setFont(_mf)
        self._pv_text.setPlaceholderText(
            "Click any row on the left to view that asset.\n\nKeyed data "
            "tables (items, NPCs, quests, skills, drops …) open as a grid of "
            "records, DDS textures render as images, and text formats "
            "(XML, JSON, JS, CSS) show as text; everything else shows a "
            "hex + metadata view.")
        pv.addWidget(self._pv_text, 1)

        # Grid view for keyed game-data tables (.pabgb), parsed via CDUMM's
        # semantic schemas. Hidden until a table is selected.
        self._pv_grid = TableWidget(pane)
        self._pv_grid.verticalHeader().hide()
        self._pv_grid.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self._pv_grid.setEditTriggers(self._pv_grid.EditTrigger.NoEditTriggers)
        _gf = self._pv_grid.font()
        _gf.setPixelSize(13)
        self._pv_grid.setFont(_gf)
        self._pv_grid.verticalHeader().setDefaultSectionSize(30)
        self._pv_grid.setVisible(False)
        # Mod maker: capture edits to verified cells (staged into a Format 3
        # mod). Always connected; the handler no-ops unless the current
        # preview is an editable table (self._pv_table is set).
        self._pv_grid.itemChanged.connect(self._on_grid_cell_edited)
        # Gear-stat editor: the button only appears when the selected row is
        # an item that actually carries stats.
        self._pv_grid.itemSelectionChanged.connect(self._update_gear_button)
        pv.addWidget(self._pv_grid, 1)

        # Image view for textures (DDS decoded to PNG). Hidden until selected.
        self._pv_image = QLabel(pane)
        self._pv_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._pv_img_scroll = QScrollArea(pane)
        self._pv_img_scroll.setWidget(self._pv_image)
        self._pv_img_scroll.setWidgetResizable(True)
        self._pv_img_scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._pv_img_scroll.setVisible(False)
        pv.addWidget(self._pv_img_scroll, 1)

        # Video view for .mp4 clips — plays inline via Qt Multimedia (FFmpeg
        # backend). Guarded so the Game Data page still loads if a build ships
        # without multimedia. Hidden until a video is selected.
        self._video_ok = False
        self._video_err = ""
        self._pv_vtemp = None
        try:
            from PySide6.QtMultimediaWidgets import QVideoWidget
            from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
            from PySide6.QtWidgets import QSlider
            self._pv_video = QVideoWidget(pane)
            self._pv_video.setMinimumHeight(240)
            self._pv_video.setVisible(False)
            pv.addWidget(self._pv_video, 1)
            self._pv_player = QMediaPlayer(self)
            self._pv_audio_out = QAudioOutput(self)
            self._pv_player.setAudioOutput(self._pv_audio_out)
            self._pv_player.setVideoOutput(self._pv_video)
            self._pv_vrow = QWidget(pane)
            _vr = QHBoxLayout(self._pv_vrow)
            _vr.setContentsMargins(0, 0, 0, 0)
            self._pv_vplay = PushButton("⏸  Pause", self._pv_vrow)
            self._pv_vplay.clicked.connect(self._on_video_playpause)
            _vr.addWidget(self._pv_vplay)
            self._pv_vslider = QSlider(Qt.Orientation.Horizontal, self._pv_vrow)
            self._pv_vslider.sliderMoved.connect(self._pv_player.setPosition)
            _vr.addWidget(self._pv_vslider, 1)
            self._pv_vrow.setVisible(False)
            pv.addWidget(self._pv_vrow)
            self._pv_player.durationChanged.connect(
                lambda d: self._pv_vslider.setRange(0, max(0, d)))
            self._pv_player.positionChanged.connect(self._on_video_pos)
            self._video_ok = True
        except Exception as _ve:  # noqa: BLE001
            _cause = getattr(_ve, "__cause__", None) or getattr(_ve, "__context__", None)
            self._video_err = str(_ve) + (f" | {_cause!r}" if _cause else "")
            import logging as _lg
            _lg.getLogger(__name__).warning(
                "video preview unavailable: %s", self._video_err)

        # "View in 3D" — only shown for image previews; opens a pop-up with
        # the texture on a rotatable sphere/cube (Qt3D).
        self._pv_3d_btn = PushButton("View in 3D  ⬤", pane)
        self._pv_3d_btn.setVisible(False)
        self._pv_3d_btn.clicked.connect(self._on_view_3d)
        pv.addWidget(self._pv_3d_btn)

        # Save the decoded texture as a normal image — PNG keeps the
        # transparent background, JPEG flattens it. Only shown for images.
        self._pv_saveimg_btn = PushButton("Save as PNG / JPEG…", pane)
        self._pv_saveimg_btn.setVisible(False)
        self._pv_saveimg_btn.clicked.connect(self._on_save_image)
        pv.addWidget(self._pv_saveimg_btn)

        # Wwise audio controls — only shown for .wem/.bnk previews. Play/Export
        # need the bundled vgmstream; they stay disabled (with a note) if it
        # isn't present, but raw extract always works.
        self._pv_play_btn = PushButton("▶  Play", pane)
        self._pv_play_btn.setVisible(False)
        self._pv_play_btn.clicked.connect(self._on_play_audio)
        pv.addWidget(self._pv_play_btn)
        self._pv_export_btn = PushButton("Export as WAV…", pane)
        self._pv_export_btn.setVisible(False)
        self._pv_export_btn.clicked.connect(self._on_export_wav)
        pv.addWidget(self._pv_export_btn)

        # One-click "get the right vgmstream for my OS" — only shown for audio
        # when vgmstream isn't found yet.
        self._pv_getvgm_btn = PushButton("⬇  Enable audio playback", pane)
        self._pv_getvgm_btn.setVisible(False)
        self._pv_getvgm_btn.clicked.connect(self._on_get_vgmstream)
        pv.addWidget(self._pv_getvgm_btn)

        self._pv_extract = PushButton("Extract raw file…", pane)
        self._pv_extract.setEnabled(False)
        self._pv_extract.clicked.connect(self._extract_raw)
        pv.addWidget(self._pv_extract)

        # ── Mod maker (Format 3) — shown only for a verified, editable table.
        # Double-clicking a verified value stages an edit; these turn the
        # staged edits into a Format 3 (.field.json) mod (import or export).
        self._mm_status = CaptionLabel("", pane)
        self._mm_status.setWordWrap(True)
        self._mm_status.setVisible(False)
        pv.addWidget(self._mm_status)
        self._mm_make_btn = PushButton("Make mod from edits…", pane)
        self._mm_make_btn.setVisible(False)
        self._mm_make_btn.setEnabled(False)
        self._mm_make_btn.clicked.connect(self._on_make_mod)
        pv.addWidget(self._mm_make_btn)
        self._mm_export_btn = PushButton("Export .field.json…", pane)
        self._mm_export_btn.setVisible(False)
        self._mm_export_btn.setEnabled(False)
        self._mm_export_btn.clicked.connect(self._on_export_field_json)
        pv.addWidget(self._mm_export_btn)
        # Gear stats sit in nested blocks that the grid flattens away, so
        # they get their own dialog. Shown only for rows that have them.
        self._mm_gear_btn = PushButton("Edit gear stats…", pane)
        self._mm_gear_btn.setVisible(False)
        self._mm_gear_btn.clicked.connect(self._on_edit_gear_stats)
        pv.addWidget(self._mm_gear_btn)
        split.addWidget(pane)

        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        self._preview_bytes: bytes | None = None
        self._preview_name: str | None = None
        self._pv_gen = 0                 # bumped per selection; ignore stale
        self._play_token = 0             # bumped per Play; ignore stale finish
        self._pv_jobs: list = []         # live (thread, worker) — keep refs
        self._pv_qimage: QImage | None = None   # current texture, for 3D
        self._pv_3d_dlg = None                   # open 3D dialog (keep a ref)
        # Mod-maker state (Format 3): the currently editable table, its
        # columns, its schema, and the edits staged from the grid so far.
        self._pv_table: str | None = None
        self._pv_cols: list = []
        self._pv_schema = None
        self._mm_editable_cols: dict = {}        # grid col index -> FieldSpec
        self._pending_edits: dict = {}           # (key, field) -> FieldEdit
        # Gear stats for the current table: {item_key: [GearStat, ...]}.
        # Populated for iteminfo only; empty for every other table.
        self._gear_stats: dict = {}
        # Stat id -> name, read from the game's own statusinfo table. Loaded
        # once, lazily, the first time the editor is opened.
        self._stat_names: dict | None = None
        # stretch=1 makes this row absorb the page's spare vertical space
        # (the base layout's trailing addStretch() has factor 0, so it yields).
        root.insertWidget(root.count() - 1, split, 1)

    # ── engine wiring ────────────────────────────────────────────────
    def set_managers(self, **kwargs) -> None:
        super().set_managers(**kwargs)
        # Preserve across re-wire calls that don't pass app_data_dir.
        self._app_data_dir = kwargs.get("app_data_dir") or self._app_data_dir
        saved = self._load_pref()
        if saved:
            self._index_path = saved
        elif self._app_data_dir:
            self._index_path = os.path.join(
                str(self._app_data_dir), "game_index.sqlite")
        if hasattr(self, "_path_label"):
            self._path_label.setText(f"Save location:  {self._index_path}")
        if hasattr(self, "_open_btn"):
            self._open_btn.setEnabled(os.path.exists(self._index_path))

    def _load_pref(self):
        try:
            if self._db:
                from cdumm.storage.config import Config
                return Config(self._db).get("game_index_path") or None
        except Exception:  # noqa: BLE001
            pass
        return None

    def _save_pref(self, path: str) -> None:
        try:
            if self._db:
                from cdumm.storage.config import Config
                Config(self._db).set("game_index_path", path)
        except Exception:  # noqa: BLE001
            pass

    # ── location actions ─────────────────────────────────────────────
    def _choose_location(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Choose where to save the game-data index",
            self._index_path, "SQLite database (*.sqlite)")
        if path:
            self._index_path = path
            self._save_pref(path)
            self._path_label.setText(f"Save location:  {path}")
            self._open_btn.setEnabled(os.path.exists(path))

    def _open_location(self) -> None:
        p = self._index_path
        folder = os.path.dirname(p) or "."
        try:
            if IS_WINDOWS:
                if os.path.exists(p):
                    subprocess.Popen(["explorer", "/select,",
                                      os.path.normpath(p)])
                else:
                    os.startfile(folder)  # noqa: S606
            elif IS_MACOS:
                args = ["open", "-R", p] if os.path.exists(p) else ["open",
                                                                     folder]
                subprocess.Popen(args)
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as ex:  # noqa: BLE001
            self._set_status(f"Could not open folder: {ex}", "#BF616A")

    # ── run ─────────────────────────────────────────────────────────
    def _on_run_clicked(self) -> None:
        if not self._can_run():
            return
        if not self._game_dir:
            self._set_status("No game folder configured.", "#BF616A")
            return

        self._clear_results()
        self._set_running(True)

        self._thread = QThread(self)
        self._worker = _GameIndexWorker(str(self._game_dir), self._index_path)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.done.connect(self._on_done)
        self._worker.error.connect(self._on_error)
        self._worker.done.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)
        self._worker.done.connect(self._worker.deleteLater)
        self._worker.error.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(lambda: setattr(self, "_thread", None))
        self._thread.start()

    # ── worker callbacks (main thread, via queued signals) ──────────
    def _on_progress(self, pct: int, msg: str) -> None:
        self._set_progress(pct, msg)

    def _on_done(self, stats: dict) -> None:
        self._set_running(False)
        try:
            self._stat_assets.set_value(
                f"{int(stats.get('assets_total', 0)):,}")
            self._stat_archives.set_value(str(stats.get("archives", "--")))
            self._stat_tables.set_value(
                str(stats.get("data_table_distinct", "--")))
        except Exception:  # noqa: BLE001
            pass
        self._set_status("Index built.", "#2E7D32")
        self._open_btn.setEnabled(os.path.exists(self._index_path))

        # Record the build in the Activity Log, the same way the other pages
        # log their actions (shows up as a "snapshot" entry).
        self._log_activity(
            "snapshot",
            f"Game data index built: "
            f"{int(stats.get('assets_total', 0)):,} assets indexed",
            f"{stats.get('archives', '?')} archives · "
            f"{stats.get('data_table_distinct', '?')} data tables")

        detail = ""
        try:
            con = sqlite3.connect(self._index_path)
            try:
                self._populate_type_filter(con)   # real extensions + counts
                tables = game_index.list_data_tables(con)[:12]
            finally:
                con.close()
            detail = "\n".join(
                f"• {t['name']}  ({t['orig_size']:,} bytes)"
                for t in tables)
        except Exception as ex:  # noqa: BLE001
            detail = f"(could not read tables: {ex})"
        # Drop the fresh card into the info row's left column (next to the
        # help boxes). Clear any prior card first so a rebuild replaces it.
        from cdumm.gui.pages.tool_page import _ResultCard
        while self._largest_col.count():
            old = self._largest_col.takeAt(0)
            w = old.widget()
            if w is not None:
                w.deleteLater()
        card = _ResultCard(
            "Largest keyed game-data tables", detail, "", self._container)
        card.setMaximumWidth(600)   # hug the content instead of spanning the page
        self._largest_col.addWidget(card)
        self._largest_col.addStretch(1)
        # refresh the viewer if a search is active
        self._on_search(self._search.text())

    def _on_error(self, msg: str) -> None:
        self._set_running(False)
        self._set_status("Index failed.", "#BF616A")
        self._add_result_card("Error", msg, "#BF616A")

    # ── search / viewer ──────────────────────────────────────────────
    # Result-count choices for the "Show" selector. Capped at 20,000: a broad
    # 2-char substring can match >1,000,000 paths and the results table renders
    # on the UI thread, so an unbounded "All" would freeze the app — 20,000
    # already covers every realistic per-type query (e.g. .pae is 6,638).
    _LIMIT_OPTIONS = (100, 300, 1_000, 5_000, 20_000)

    def _result_limit(self) -> int:
        combo = getattr(self, "_limit_combo", None)
        idx = combo.currentIndex() if combo is not None else 1
        if 0 <= idx < len(self._LIMIT_OPTIONS):
            return self._LIMIT_OPTIONS[idx]
        return 300

    # Curated file-type filter shown before the index is built (and as a
    # fallback). Once built, _populate_type_filter() replaces this with the
    # extensions actually present, each with its count.
    _DEFAULT_TYPE_FILTERS = (
        ("All types", None), (".pabgb", ".pabgb"), (".dds", ".dds"),
        (".wem", ".wem"), (".bnk", ".bnk"), (".paa", ".paa"),
        (".pae", ".pae"), (".paseq", ".paseq"), (".prefab", ".prefab"),
        (".meshinfo", ".meshinfo"), (".hkx", ".hkx"), (".xml", ".xml"),
    )

    def _set_type_items(self, items) -> None:
        """Rebuild the Type dropdown from ``items`` (list of (label, ext)),
        keeping the current selection by extension when it still exists."""
        prev = self._type_filter() if getattr(self, "_type_exts", None) else None
        self._type_combo.blockSignals(True)
        self._type_combo.clear()
        self._type_exts = [ext for _lbl, ext in items]
        self._type_combo.addItems([lbl for lbl, _ext in items])
        if prev in self._type_exts:
            self._type_combo.setCurrentIndex(self._type_exts.index(prev))
        self._type_combo.blockSignals(False)

    def _type_filter(self) -> str | None:
        """The currently selected extension filter, or None for 'All types'."""
        exts = getattr(self, "_type_exts", None)
        if not exts:
            return None
        idx = self._type_combo.currentIndex()
        return exts[idx] if 0 <= idx < len(exts) else None

    def _populate_type_filter(self, con) -> None:
        """Fill the Type dropdown from EVERY extension actually indexed, most
        common first, each labelled with its file count.

        No cap: rare but modding-relevant formats (e.g. .pabgb keyed tables,
        ~130 files) must not be dropped just because bulk assets
        (.wem / .paa / .dds, hundreds of thousands each) dwarf them in count.
        """
        try:
            rows = con.execute(
                "SELECT ext, COUNT(*) c FROM assets GROUP BY ext "
                "ORDER BY c DESC").fetchall()
        except Exception:  # noqa: BLE001
            return
        items = [("All types", None)]
        for ext, c in rows:
            if ext and ext != "(none)":
                items.append((f"{ext}  ({c:,})", ext))
        if len(items) > 1:
            self._set_type_items(items)

    def _on_search(self, text: str) -> None:
        text = (text or "").strip()
        ext = self._type_filter()
        if not os.path.exists(self._index_path):
            self._hits.setText("Build the index first to search.")
            self._table.setRowCount(0)
            return
        # A type filter lets you browse every file of that type with no search
        # text; without one, require 2+ chars so a bare query can't scan all
        # 1.6M paths.
        if ext is None and len(text) < 2:
            self._hits.setText("Type at least 2 characters to search.")
            self._table.setRowCount(0)
            return
        limit = self._result_limit()
        try:
            con = sqlite3.connect(self._index_path)
            try:
                rows = game_index.search_assets(
                    con, query=text, ext=ext, limit=limit)
            finally:
                con.close()
        except Exception as ex:  # noqa: BLE001
            self._hits.setText(f"Search failed: {ex}")
            return
        capped = len(rows) >= limit
        self._hits.setText(
            f"{len(rows):,} match(es)"
            + (f" (showing first {limit:,} — narrow the search for more)"
               if capped else ""))
        self._table.setSortingEnabled(False)   # bulk-fill, then re-enable
        self._table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            for c, cell in enumerate(_build_row_items(r)):
                self._table.setItem(i, c, cell)
        self._table.setSortingEnabled(True)

    # ── preview pane ─────────────────────────────────────────────────
    _PREVIEW_TEXT_CAP = 200_000            # chars shown for text assets
    _PREVIEW_HEX_CAP = 4096               # bytes shown for the hex view
    _PREVIEW_SIZE_LIMIT = 4 * 1024 * 1024   # inline byte-preview ceiling
    _TABLE_SIZE_LIMIT = 8 * 1024 * 1024     # parse .pabgb data tables up to here
    _IMAGE_SIZE_LIMIT = 64 * 1024 * 1024    # decode DDS textures up to here
    #   Reading / decoding / parsing all run on a background worker, so these
    #   caps just bound memory + latency — they no longer gate the UI thread.

    def _selected_path(self) -> str | None:
        items = self._table.selectedItems()
        if not items:
            return None
        cell = self._table.item(items[0].row(), 0)   # Name cell carries the
        if cell is None:                              # full path in UserRole
            return None
        full = cell.data(Qt.ItemDataRole.UserRole)
        return str(full) if full else cell.text()

    def _set_type_filter(self, ext: str) -> None:
        """Point the Type dropdown at ``ext`` (re-runs the search)."""
        exts = getattr(self, "_type_exts", [])
        if ext in exts:
            self._type_combo.setCurrentIndex(exts.index(ext))

    def _on_table_menu(self, pos) -> None:
        """Right-click a result row: copy its path / filename, or filter the
        list to that file type."""
        item = self._table.itemAt(pos)
        if item is None:
            return
        name_cell = self._table.item(item.row(), 0)
        full = name_cell.data(Qt.ItemDataRole.UserRole) if name_cell else None
        if not full:
            return
        full = str(full)
        _folder, name = _split_path(full)
        type_cell = self._table.item(item.row(), 3)
        ext = type_cell.text() if type_cell else ""
        from PySide6.QtWidgets import QApplication
        from qfluentwidgets import Action, RoundMenu
        menu = RoundMenu(parent=self._table)
        _act_path = Action("Copy path")
        _act_path.triggered.connect(
            lambda: QApplication.clipboard().setText(full))
        menu.addAction(_act_path)
        _act_name = Action("Copy filename")
        _act_name.triggered.connect(
            lambda: QApplication.clipboard().setText(name))
        menu.addAction(_act_name)
        if ext and ext in getattr(self, "_type_exts", []):
            menu.addSeparator()
            _act_filter = Action(f"Show only {ext}")
            _act_filter.triggered.connect(
                lambda e=ext: self._set_type_filter(e))
            menu.addAction(_act_filter)
        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _on_asset_selected(self) -> None:
        """Kick off a background read of the clicked asset. The heavy work
        (extract / decompress / decode / parse) runs on a worker thread;
        results land in _on_preview_ready, so the UI can never freeze."""
        path = self._selected_path()
        if not path:
            return
        self._pv_gen += 1
        self._preview_bytes = None
        self._preview_name = os.path.basename(path)
        self._pv_extract.setEnabled(False)
        self._pv_title.setText(self._preview_name)
        self._pv_meta.setText("")
        self._show_text("Reading…", wrap=True)

        th = QThread(self)
        w = _PreviewWorker(
            self._index_path,
            str(self._game_dir) if self._game_dir else "",
            path, self._pv_gen, self._PREVIEW_SIZE_LIMIT,
            self._TABLE_SIZE_LIMIT, self._IMAGE_SIZE_LIMIT,
            self._PREVIEW_TEXT_CAP, self._PREVIEW_HEX_CAP)
        w.moveToThread(th)
        th.started.connect(w.run)
        w.ready.connect(self._on_preview_ready)
        w.ready.connect(th.quit)
        w.ready.connect(w.deleteLater)
        th.finished.connect(th.deleteLater)
        job = (th, w)
        self._pv_jobs.append(job)
        th.finished.connect(
            lambda job=job: job in self._pv_jobs and self._pv_jobs.remove(job))
        th.start()

    def _on_preview_ready(self, res: dict) -> None:
        if res.get("gen") != self._pv_gen:
            return   # a newer selection superseded this result
        self._reset_maker()   # clear any staged edits/controls from a prior asset
        self._stop_video()    # stop + hide any video playing from a prior asset
        # Image-only button; _show_image re-shows it, every other branch leaves
        # it hidden.
        self._pv_saveimg_btn.setVisible(False)
        row = res.get("row")
        meta = ""
        if row:
            flags = []
            if row["compressed"]:
                flags.append("LZ4")
            if row["encrypted"]:
                flags.append("encrypted")
            meta = (f"{row['ext']}  ·  {int(row['orig_size']):,} bytes  ·  "
                    f"archive {row['archive']}  ·  "
                    f"{', '.join(flags) or 'stored raw'}")
        kind = res.get("kind")

        if kind == "table":
            total = res.get("total", 0)
            shown = len(res.get("rows", []))
            more = f" (showing first {shown:,})" if shown < total else ""
            if res.get("health", 0.0) >= 0.9:
                note = ("\n⚠ field columns didn't parse cleanly for this "
                        "table on this build — trust _key and _name only")
            else:
                note = ("\nfield columns are experimental (from CDUMM's patch "
                        "parser); _key and _name are authoritative")
            if res.get("has_pos"):
                note += ("\nworld pos (X, Y, Z) = decoded, region-validated "
                         "map coordinates for mod-makers")
            self._pv_meta.setText(
                f"data table “{res.get('table', '')}”  ·  {total:,} records"
                f"{more}{note}\n{res.get('path', '')}")
            self._show_grid(res.get("cols", []), res.get("rows", []))
            self._pv_extract.setEnabled(True)
            self._enable_table_editing(
                res.get("table", ""), res.get("cols", []))
            self._gear_stats = res.get("gear_stats") or {}
            self._update_gear_button()
            return

        if kind == "image":
            img = res.get("img", {})
            self._pv_meta.setText(
                f"texture  ·  {img.get('orig_w', '?')}×"
                f"{img.get('orig_h', '?')}  ·  {img.get('mode', '')}"
                + (f"  ·  {meta}" if meta else "")
                + f"\n{res.get('path', '')}")
            self._show_image(img.get("png", b""))
            self._pv_extract.setEnabled(True)
            return

        if kind == "video":
            self._pv_meta.setText(
                (f"video  ·  {meta}" if meta else "video")
                + f"\n{res.get('path', '')}")
            self._show_video(res)
            self._pv_extract.setEnabled(True)
            return

        self._pv_meta.setText(f"{meta}\n{res.get('path', '')}" if meta
                              else res.get("path", ""))
        if kind == "text":
            body = res.get("text", "")
            if res.get("truncated"):
                body += ("\n\n… (truncated preview — use Extract raw for the "
                         "full file)")
            self._show_text(body, wrap=True)
            self._pv_extract.setEnabled(True)
        elif kind == "schema":
            self._show_schema(res)
            self._pv_extract.setEnabled(True)
        elif kind == "outline":
            strings = res.get("strings", [])
            self._show_text(
                "Structure outline — the field / type / object names embedded "
                "in this reflection-serialized binary ("
                f"{res.get('nstr', len(strings))} names). Use “Extract raw "
                "file…” for the full bytes.\n\n" + "\n".join(strings),
                wrap=True)
            self._pv_extract.setEnabled(True)
        elif kind == "struct":
            self._show_struct(res)
            self._pv_extract.setEnabled(True)
        elif kind == "audio":
            self._show_audio(res)
            self._pv_extract.setEnabled(True)
        elif kind == "hex":
            self._show_text(
                "Binary asset — no visual decoder for this format yet "
                "(textures, audio and models need converters). Hex view of "
                "the first bytes:\n\n" + res.get("text", ""), wrap=False)
            self._pv_extract.setEnabled(True)
        elif kind == "toobig":
            self._show_text(
                "Asset is too large to preview inline.\nUse “Extract raw "
                "file…” to save it to disk.", wrap=True)
            self._pv_extract.setEnabled(True)
        elif kind == "meta":
            self._show_text(
                "No game folder configured — can't read asset bytes.",
                wrap=True)
        else:  # error
            self._show_text(
                f"Could not read asset:\n{res.get('error', '')}", wrap=True)

    def _show_text(self, text: str, *, wrap: bool) -> None:
        self._pv_grid.setVisible(False)
        self._pv_img_scroll.setVisible(False)
        self._pv_3d_btn.setVisible(False)
        self._pv_play_btn.setVisible(False)
        self._pv_export_btn.setVisible(False)
        self._pv_getvgm_btn.setVisible(False)
        self._pv_text.setVisible(True)
        self._pv_text.setLineWrapMode(
            self._pv_text.LineWrapMode.WidgetWidth if wrap
            else self._pv_text.LineWrapMode.NoWrap)
        self._pv_text.setPlainText(text)

    def _show_video(self, res: dict) -> None:
        # Hide the other preview views, write the clip to a temp .mp4 and play.
        self._pv_text.setVisible(False)
        self._pv_grid.setVisible(False)
        self._pv_img_scroll.setVisible(False)
        self._pv_3d_btn.setVisible(False)
        self._pv_saveimg_btn.setVisible(False)
        self._pv_play_btn.setVisible(False)
        self._pv_export_btn.setVisible(False)
        self._pv_getvgm_btn.setVisible(False)
        if not self._video_ok:
            self._pv_text.setVisible(True)
            _why = getattr(self, "_video_err", "")
            self._pv_text.setPlainText(
                "Video playback isn't available in this build. Use "
                "“Extract raw file…” to save the .mp4 and open it in a player."
                + (f"\n\n({_why})" if _why else ""))
            return
        import os as _os
        import tempfile as _tf
        from PySide6.QtCore import QUrl
        try:
            fd, tmp = _tf.mkstemp(suffix=".mp4", prefix="cdumm_vid_")
            with _os.fdopen(fd, "wb") as fh:
                fh.write(res.get("data", b""))
            self._pv_vtemp = tmp
            self._pv_video.setVisible(True)
            self._pv_vrow.setVisible(True)
            self._pv_vplay.setText("⏸  Pause")
            self._pv_player.setSource(QUrl.fromLocalFile(tmp))
            self._pv_player.play()
        except Exception as e:  # noqa: BLE001
            self._pv_text.setVisible(True)
            self._pv_text.setPlainText(f"Could not play video: {e}")

    def _stop_video(self) -> None:
        """Stop playback, hide the video widgets and delete the temp clip.
        Safe to call whether or not a video is showing (or supported)."""
        if not getattr(self, "_video_ok", False):
            return
        try:
            from PySide6.QtCore import QUrl
            self._pv_player.stop()
            self._pv_player.setSource(QUrl())
        except Exception:  # noqa: BLE001
            pass
        self._pv_video.setVisible(False)
        self._pv_vrow.setVisible(False)
        if getattr(self, "_pv_vtemp", None):
            try:
                import os as _os
                _os.unlink(self._pv_vtemp)
            except OSError:
                pass
            self._pv_vtemp = None

    def _on_video_playpause(self) -> None:
        if not getattr(self, "_video_ok", False):
            return
        from PySide6.QtMultimedia import QMediaPlayer as _MP
        if self._pv_player.playbackState() == _MP.PlaybackState.PlayingState:
            self._pv_player.pause()
            self._pv_vplay.setText("▶  Play")
        else:
            self._pv_player.play()
            self._pv_vplay.setText("⏸  Pause")

    def _on_video_pos(self, pos: int) -> None:
        if getattr(self, "_video_ok", False) and not self._pv_vslider.isSliderDown():
            self._pv_vslider.setValue(pos)

    def _show_struct(self, res: dict) -> None:
        total = res.get("total_words", 0)
        shown = res.get("shown", 0)
        trailing = res.get("trailing", 0)
        more = f" · first {shown:,} of {total:,}" if shown < total else ""
        tail = f" · +{trailing} trailing byte(s)" if trailing else ""
        fmt = res.get("format")
        fmt_line = f"Format: {fmt}.  " if fmt else ""
        self._pv_meta.setText(
            fmt_line
            + "Struct view — this format has no embedded field names, so each "
            "32-bit word is shown as unsigned / signed / float. Values in the "
            "1,000,000+ range are flagged as likely record keys."
            f"  ({total:,} words{more}{tail})\n"
            + self._pv_meta.text())
        cols = ["Offset", "Bytes", "UInt32", "Int32", "Float32", "ASCII", ""]
        rows = [[r[0], r[1], r[2], r[3], r[4], r[5], "key" if r[6] else ""]
                for r in res.get("rows", [])]
        self._show_grid(cols, rows)

    def _show_schema(self, res: dict) -> None:
        fields = res.get("fields", [])
        objects = res.get("objects", [])
        refs = res.get("refs", [])
        rows = []
        last_obj = None
        for obj, name, typ in fields:            # object name only on its first
            rows.append([obj if obj != last_obj else "", name, typ])
            last_obj = obj
        if refs:                                 # asset references / values
            rows.append(["", "", ""])
            rows.append([f"references ({len(refs)})", "", ""])
            for r in refs:
                rows.append(["", r, ""])
        note = (
            f"Reflection schema — {res.get('nfields', len(fields))} fields "
            f"across {len(objects)} object(s)"
            + (f", {len(refs)} references" if refs else "")
            + ". These are the engine's own field + type names, read straight "
            "from the file (not inferred).")
        self._pv_meta.setText(note + "\n" + self._pv_meta.text())
        self._show_grid(["Object", "Field", "Type"], rows)

    def _show_audio(self, res: dict) -> None:
        a = res.get("audio", {})
        has_vgm = res.get("vgmstream", False)
        if a.get("kind") == "wem":
            dur = a.get("duration")
            durs = f"{dur:.2f} s" if dur else "(shown after decode)"
            body = ("Wwise audio stream (.wem)\n\n"
                    f"Codec         {a.get('codec')}\n"
                    f"Channels      {a.get('channels')}\n"
                    f"Sample rate   {a.get('sample_rate', 0):,} Hz\n"
                    f"Audio bytes   {a.get('data_bytes', 0):,}\n"
                    f"Duration      {durs}\n"
                    f"Chunks        {', '.join(a.get('chunks', []))}\n")
            playable = has_vgm
        else:  # .bnk SoundBank
            _ns = a.get('embedded_streams', 0)
            body = ("Wwise SoundBank (.bnk)\n\n"
                    f"Bank version       {a.get('bank_version')}\n"
                    f"Sections           {', '.join(a.get('sections', []))}\n"
                    f"Embedded streams   {_ns}\n")
            # A bank that carries embedded audio (DIDX/DATA) is playable —
            # vgmstream decodes its first embedded sound. Banks with no streams
            # (event / metadata only) have nothing to play.
            playable = has_vgm and _ns > 0
        note = ("\nThe game ships all sound through Wwise: .wem are the encoded "
                "streams (Wwise Vorbis) and .bnk are SoundBanks — Windows can't "
                "play these directly.\n")
        if has_vgm:
            if a.get("kind") != "wem" and playable:
                note += ("Use ▶ Play to hear the bank's first embedded sound or "
                         "Export as WAV; Extract raw file saves the whole .bnk.")
            else:
                note += ("Use ▶ Play to hear it or Export as WAV for a playable "
                         "copy; Extract raw file saves the original .wem/.bnk.")
        else:
            note += ("Playback needs vgmstream — drop vgmstream-cli.exe into the "
                     "app's tools/vgmstream folder. Extract raw file works now.")
        self._show_text(body + note, wrap=True)
        self._pv_play_btn.setVisible(True)
        self._pv_play_btn.setEnabled(playable)
        self._pv_export_btn.setVisible(True)
        self._pv_export_btn.setEnabled(playable)
        self._pv_getvgm_btn.setVisible(not has_vgm)   # one-click auto-install

    def _show_image(self, png: bytes) -> None:
        self._pv_text.setVisible(False)
        self._pv_grid.setVisible(False)
        self._pv_play_btn.setVisible(False)
        self._pv_export_btn.setVisible(False)
        self._pv_getvgm_btn.setVisible(False)
        self._pv_img_scroll.setVisible(True)
        self._pv_qimage = QImage.fromData(png, "PNG") if png else None
        _has_img = self._pv_qimage is not None and not self._pv_qimage.isNull()
        self._pv_3d_btn.setVisible(_has_img)
        self._pv_saveimg_btn.setVisible(_has_img)
        pm = QPixmap()
        if png:
            pm.loadFromData(png, "PNG")
        # Fit to the pane width (preserve aspect); the scroll area handles
        # anything taller than the viewport.
        avail = self._pv_img_scroll.viewport().width() or 600
        if not pm.isNull() and pm.width() > avail:
            pm = pm.scaledToWidth(
                avail, Qt.TransformationMode.SmoothTransformation)
        self._pv_image.setPixmap(pm)

    # ── Mod maker (Format 3) ─────────────────────────────────────────
    def _reset_maker(self) -> None:
        """Clear staged edits + hide maker controls (called every preview)."""
        self._pv_table = None
        self._pv_cols = []
        self._pv_schema = None
        self._mm_editable_cols = {}
        self._pending_edits = {}
        self._gear_stats = {}
        for w in (getattr(self, "_mm_status", None),
                  getattr(self, "_mm_make_btn", None),
                  getattr(self, "_mm_export_btn", None),
                  getattr(self, "_mm_gear_btn", None)):
            if w is not None:
                w.setVisible(False)
        grid = getattr(self, "_pv_grid", None)
        if grid is not None:
            try:
                grid.setEditTriggers(grid.EditTrigger.NoEditTriggers)
            except Exception:  # noqa: BLE001
                pass

    def _enable_table_editing(self, table: str, cols: list) -> None:
        """Make verified fixed-width cells of the just-shown table editable
        and reveal the maker controls. Stays read-only (no controls) when the
        table has no curated/verified fixed-width fields — so a guessed byte
        is never editable."""
        self._pv_cols = list(cols)
        try:
            from cdumm.semantic import parser as sem
            schema = sem.get_schema(table)
        except Exception:  # noqa: BLE001
            schema = None
        editable = self._editable_columns(cols, schema)
        grid = self._pv_grid
        grid.blockSignals(True)
        try:
            for r in range(grid.rowCount()):
                for c in range(grid.columnCount()):
                    it = grid.item(r, c)
                    if it is None:
                        continue
                    if c in editable and it.text() != "(unverified)":
                        it.setFlags(it.flags() | Qt.ItemFlag.ItemIsEditable)
                        it.setData(Qt.ItemDataRole.UserRole, it.text())
                    else:
                        it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
        finally:
            grid.blockSignals(False)
        if not editable:
            grid.setEditTriggers(grid.EditTrigger.NoEditTriggers)
            return
        grid.setEditTriggers(grid.EditTrigger.DoubleClicked)
        self._pv_table = table
        self._pv_schema = schema
        self._mm_editable_cols = editable
        self._mm_status.setVisible(True)
        self._mm_make_btn.setVisible(True)
        self._mm_export_btn.setVisible(True)
        self._refresh_maker_buttons()

    def _editable_columns(self, cols: list, schema) -> dict:
        """Map grid column index -> FieldSpec for columns a user may edit: a
        verified (or un-gated) fixed-width scalar field that isn't in the
        masked tail and isn't a metadata column (_key / _name / world pos)."""
        from cdumm.engine.format3_builder import is_editable_scalar_field
        out: dict = {}
        if not schema:
            return out
        verified = getattr(schema, "verified_fields", None)
        by_name = {f.name: f for f in schema.fields}
        # Same masking _shape_records uses: the first field with no decodable
        # width, and everything after it, can't be trusted byte-for-byte.
        masked, hit = set(), False
        for f in schema.fields:
            if f.name in ("_key", "_name"):
                continue
            if not hit and hasattr(f, "field_type") and (
                    not getattr(f, "struct_fmt", None)
                    and not getattr(f, "type_descriptor", None)
                    and f.field_type != "CString"):
                hit = True
            if hit:
                masked.add(f.name)
        for c, name in enumerate(cols):
            if name in ("_key", "_name", "world pos (X, Y, Z)"):
                continue
            spec = by_name.get(name)
            if spec is None or name in masked:
                continue
            # Only a curated table (verified set present) may be edited, and
            # only its verified fields — never an unproven offset. An
            # un-curated table (verified is None) exposes nothing to the maker.
            if verified is None or name not in verified:
                continue
            if is_editable_scalar_field(spec):
                out[c] = spec
        return out

    def _on_grid_cell_edited(self, item) -> None:
        """Stage a verified-cell edit as a FieldEdit, validated against the
        field type. Reverts the cell (to its original) on invalid input."""
        if item is None or not self._pv_table:
            return
        c = item.column()
        spec = self._mm_editable_cols.get(c)
        if spec is None:
            return
        from cdumm.engine.format3_builder import FieldEdit, parse_scalar_value
        grid = self._pv_grid
        r = item.row()
        key_item = grid.item(r, 0)
        name_item = grid.item(r, 1)
        try:
            key = int(str(key_item.text()).strip()) if key_item else 0
        except (ValueError, AttributeError):
            key = 0
        entry = name_item.text() if name_item else ""
        field = self._pv_cols[c] if c < len(self._pv_cols) else spec.name
        try:
            new_val = parse_scalar_value(spec, item.text())
        except (ValueError, TypeError):
            bad = item.text()
            orig = item.data(Qt.ItemDataRole.UserRole)
            grid.blockSignals(True)
            item.setText("" if orig is None else str(orig))
            grid.blockSignals(False)
            self._flash_maker(
                f"“{bad}” isn't a valid value for {field}.", error=True)
            return
        self._pending_edits[(key, field)] = FieldEdit(
            target=f"{self._pv_table}.pabgb", entry=entry, key=key,
            field=field, new=new_val,
            old=item.data(Qt.ItemDataRole.UserRole))
        self._refresh_maker_buttons()

    def _refresh_maker_buttons(self) -> None:
        n = len(self._pending_edits)
        self._mm_make_btn.setEnabled(n > 0)
        self._mm_export_btn.setEnabled(n > 0)
        self._mm_make_btn.setText(
            f"Make mod from {n} edit(s)…" if n else "Make mod from edits…")
        base = ("Editable table — double-click a verified value to change it, "
                "then make or export a mod.")
        self._mm_status.setText(
            base if n == 0 else f"{n} pending edit(s).  {base}")

    def _flash_maker(self, msg: str, error: bool = False) -> None:
        try:
            from qfluentwidgets import InfoBar, InfoBarPosition
            fn = InfoBar.warning if error else InfoBar.success
            fn("Mod maker", msg, duration=6000,
               position=InfoBarPosition.TOP, parent=self)
        except Exception:  # noqa: BLE001
            if getattr(self, "_mm_status", None) is not None:
                self._mm_status.setText(msg)

    def _mm_prompt_name(self, purpose: str) -> "str | None":
        from PySide6.QtWidgets import QInputDialog
        default = f"{self._pv_table} edits" if self._pv_table else "My mod"
        name, ok = QInputDialog.getText(self, purpose, "Mod name:", text=default)
        name = (name or "").strip()
        return name if (ok and name) else None

    def _on_make_mod(self) -> None:
        if not self._pending_edits:
            return
        if not (self._game_dir and self._db and self._snapshot
                and self._deltas_dir):
            self._flash_maker(
                "Set your Crimson Desert game folder first (Settings), then "
                "reopen this table.", error=True)
            return
        name = self._mm_prompt_name("Make mod from edits")
        if not name:
            return
        from cdumm.engine.format3_builder import create_mod_from_edits
        try:
            res = create_mod_from_edits(
                list(self._pending_edits.values()), title=name,
                game_dir=self._game_dir, db=self._db,
                snapshot=self._snapshot, deltas_dir=self._deltas_dir)
        except Exception as e:  # noqa: BLE001
            self._flash_maker(f"Couldn't create the mod: {e}", error=True)
            return
        if getattr(res, "error", None):
            self._flash_maker(res.error, error=True)
            return
        n = len(self._pending_edits)
        self._pending_edits = {}
        self._refresh_maker_buttons()
        self._flash_maker(
            f"Created “{name}” from {n} edit(s) — enable it in the Mods tab, "
            f"then Apply.", error=False)

    def _on_export_field_json(self) -> None:
        if not self._pending_edits:
            return
        name = self._mm_prompt_name("Export .field.json")
        if not name:
            return
        path, _sel = QFileDialog.getSaveFileName(
            self, "Export Format 3 mod", f"{name}.field.json",
            "Format 3 mod (*.field.json *.json)")
        if not path:
            return
        from cdumm.engine.format3_builder import (build_format3_json,
                                                  write_field_json)
        try:
            mod = build_format3_json(
                list(self._pending_edits.values()), title=name)
            write_field_json(mod, path)
        except Exception as e:  # noqa: BLE001
            self._flash_maker(f"Export failed: {e}", error=True)
            return
        self._flash_maker(
            f"Exported {len(self._pending_edits)} edit(s) to "
            f"{os.path.basename(path)}.", error=False)

    # ── gear-stat editor ─────────────────────────────────────────────
    def _selected_record_key(self) -> "int | None":
        """Record key of the selected grid row (column 0), or None."""
        grid = self._pv_grid
        r = grid.currentRow()
        if r < 0:
            return None
        cell = grid.item(r, 0)
        try:
            return int(str(cell.text()).strip()) if cell else None
        except (ValueError, AttributeError):
            return None

    def _update_gear_button(self) -> None:
        """Show 'Edit gear stats…' only for a row that actually has stats."""
        btn = getattr(self, "_mm_gear_btn", None)
        if btn is None:
            return
        key = self._selected_record_key()
        btn.setVisible(key is not None and key in self._gear_stats)

    def _get_stat_names(self) -> dict:
        """Stat id -> name, from the game's own statusinfo table.

        Read from the installed game rather than a hardcoded map, because a
        hardcoded map is exactly what rots when the game patches. Falls back
        to the CD 1.13 snapshot when the game isn't reachable.
        """
        if self._stat_names is None:
            from cdumm.engine.stat_names import load_stat_names
            try:
                self._stat_names = load_stat_names()
            except Exception:  # noqa: BLE001 — names are a nicety, not the data
                logger.exception("could not read stat names from the game")
                self._stat_names = {}
        return self._stat_names

    def _on_edit_gear_stats(self) -> None:
        """Edit the selected item's gear stats, staging each change as a
        Format 3 edit on that stat's exact nested path."""
        key = self._selected_record_key()
        stats = self._gear_stats.get(key) if key is not None else None
        if not stats:
            self._flash_maker("Select an item that has gear stats first.",
                              error=True)
            return
        grid = self._pv_grid
        name_cell = grid.item(grid.currentRow(), 1)
        entry = name_cell.text() if name_cell else ""

        edits = self._prompt_gear_stats(entry or f"item {key}", stats)
        if not edits:
            return
        from cdumm.engine.format3_builder import FieldEdit
        target = f"{self._pv_table}.pabgb"
        for stat, new in edits:
            # Keyed by path, so each tier is its own edit. The old editor
            # keyed by stat id and could only ever write one of them.
            self._pending_edits[(key, stat.path)] = FieldEdit(
                target=target, entry=entry, key=key, field=stat.path,
                new=new, old=stat.value)
        self._refresh_maker_buttons()
        self._flash_maker(
            f"Staged {len(edits)} stat edit(s) for “{entry or key}”.",
            error=False)

    def _prompt_gear_stats(self, title: str, stats: list) -> list:
        """Modal editor for one item's stats. Returns ``[(GearStat, new)]``
        for the values the user actually changed.

        Every occurrence gets its own row — base and each enhancement tier —
        because they are separate values in the file. Editing one does not
        touch the others, and the dialog says so rather than hiding it.
        """
        from PySide6.QtWidgets import (QAbstractItemView, QDialog,
                                       QDialogButtonBox, QHeaderView, QLabel,
                                       QTableWidget, QTableWidgetItem,
                                       QVBoxLayout)
        from cdumm.engine.stat_names import stat_label

        names = self._get_stat_names()
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Edit gear stats — {title}")
        dlg.resize(560, 460)
        outer = QVBoxLayout(dlg)
        blurb = QLabel(
            "Each row is a separate value in the game file. “Base” is the "
            "item as dropped; each “Enhance +N” is what it becomes at that "
            "upgrade level.\n"
            "Changing one row changes only that row — edit every tier if you "
            "want the change to hold as the item is upgraded.")
        blurb.setWordWrap(True)
        outer.addWidget(blurb)

        tbl = QTableWidget(len(stats), 3, dlg)
        tbl.setHorizontalHeaderLabels(["Stat", "Where", "Value"])
        tbl.verticalHeader().hide()
        tbl.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        for row, s in enumerate(stats):
            for col, text in ((0, stat_label(s.stat, names)), (1, s.where)):
                cell = QTableWidgetItem(text)
                cell.setFlags(Qt.ItemFlag.ItemIsEnabled)   # read-only
                tbl.setItem(row, col, cell)
            val = QTableWidgetItem(str(s.value))
            val.setData(Qt.ItemDataRole.UserRole, s.value)
            tbl.setItem(row, 2, val)
        hdr = tbl.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        outer.addWidget(tbl, 1)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        outer.addWidget(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return []

        out = []
        for row, s in enumerate(stats):
            cell = tbl.item(row, 2)
            text = (cell.text() if cell else "").strip()
            if not text:
                continue
            try:
                new = int(text)
            except ValueError:
                self._flash_maker(
                    f"“{text}” isn't a whole number "
                    f"({stat_label(s.stat, names)}, {s.where}) — skipped.",
                    error=True)
                continue
            if not (-(2 ** 63) <= new < 2 ** 63):
                self._flash_maker(
                    f"{stat_label(s.stat, names)} ({s.where}) is out of "
                    f"range — skipped.", error=True)
                continue
            if new != s.value:
                out.append((s, new))
        return out

    def _show_grid(self, cols: list, rows: list) -> None:
        self._pv_text.setVisible(False)
        self._pv_img_scroll.setVisible(False)
        self._pv_3d_btn.setVisible(False)
        self._pv_play_btn.setVisible(False)
        self._pv_export_btn.setVisible(False)
        self._pv_getvgm_btn.setVisible(False)
        self._pv_grid.setVisible(True)
        self._pv_grid.clear()
        self._pv_grid.setColumnCount(len(cols))
        self._pv_grid.setHorizontalHeaderLabels([str(c) for c in cols])
        self._pv_grid.setRowCount(len(rows))
        for r, rowvals in enumerate(rows):
            for c, v in enumerate(rowvals):
                self._pv_grid.setItem(r, c, QTableWidgetItem(v))
        # Auto-size only for narrow tables; wide ones (e.g. iteminfo's 113
        # fields) keep a default width and scroll, so sizing stays instant.
        if len(cols) <= 25:
            try:
                self._pv_grid.resizeColumnsToContents()
            except Exception:  # noqa: BLE001
                pass
        else:
            for c in range(len(cols)):
                self._pv_grid.setColumnWidth(c, 130)

    def _on_view_3d(self) -> None:
        """Open the current texture on a rotatable sphere/cube (pop-up).

        Qt3DWindow creates a native OpenGL window; doing that *inside* this
        click handler triggers a COM input-synchronous crash
        (0x8001010d / RPC_E_CANTCALLOUT_ININPUTSYNCCALL) in the packaged
        build — a hard native crash the try/except below can't catch. Defer to
        the next event-loop tick so the click event finishes dispatching first.
        """
        if self._pv_qimage is None or self._pv_qimage.isNull():
            return
        from PySide6.QtCore import QTimer
        self._set_status("Opening 3D preview…", "")
        QTimer.singleShot(0, self._open_3d_deferred)

    def _open_3d_deferred(self) -> None:
        if self._pv_qimage is None or self._pv_qimage.isNull():
            return
        try:
            dlg = _Texture3DView(
                self._pv_qimage, self._preview_name or "Texture", self)
        except Exception as ex:  # noqa: BLE001 — Qt3D / GPU/driver issue
            self._set_status(f"3D preview unavailable: {ex}", "#BF616A")
            return
        self._pv_3d_dlg = dlg          # keep a ref so it isn't GC'd
        dlg.show()
        self._set_status("", "")

    def _on_save_image(self) -> None:
        """Save the decoded texture as a standard image the OS can open.

        PNG by default — it keeps the transparent background exactly as shown
        in the preview. JPEG is offered too (smaller, but no transparency, so
        transparent areas are flattened onto white).
        """
        img = self._pv_qimage
        if img is None or img.isNull():
            return
        base = os.path.splitext(self._preview_name or "texture")[0]
        start = os.path.join(os.path.expanduser("~"), base + ".png")
        path, _sel = QFileDialog.getSaveFileName(
            self, "Save texture as image", start,
            "PNG image — keeps transparency (*.png);;JPEG image (*.jpg *.jpeg)")
        if not path:
            return
        ext = os.path.splitext(path)[1].lower()
        is_jpeg = ext in (".jpg", ".jpeg")
        out = img
        if is_jpeg and img.hasAlphaChannel():
            # JPEG has no alpha — flatten onto white so transparent regions
            # don't turn black.
            from PySide6.QtGui import QPainter, QColor
            out = QImage(img.size(), QImage.Format.Format_RGB32)
            out.fill(QColor("white"))
            p = QPainter(out)
            p.drawImage(0, 0, img)
            p.end()
        ok = (out.save(path, "JPG", 92) if is_jpeg else out.save(path, "PNG"))
        base_meta = self._pv_meta.text()
        self._pv_meta.setText(
            (f"Saved image → {path}" if ok
             else f"Could not save image to {path}") + "\n" + base_meta)

    def _extract_raw(self) -> None:
        """Save the selected asset's real (decoded) bytes to a file."""
        path = self._selected_path()
        if not path:
            return
        data = self._preview_bytes
        if data is None:                       # large asset — extract on demand
            try:
                con = sqlite3.connect(self._index_path)
                try:
                    data = game_index.extract_asset(
                        con, path, str(self._game_dir))
                finally:
                    con.close()
            except Exception as ex:  # noqa: BLE001
                self._set_status(f"Extract failed: {ex}", "#BF616A")
                return
        default = os.path.join(
            os.path.expanduser("~"),
            self._preview_name or os.path.basename(path))
        out, _ = QFileDialog.getSaveFileName(
            self, "Save extracted asset", default, "All files (*.*)")
        if not out:
            return
        try:
            with open(out, "wb") as f:
                f.write(data)
            self._set_status(f"Saved {len(data):,} bytes to {out}.", "#2E7D32")
        except Exception as ex:  # noqa: BLE001
            self._set_status(f"Save failed: {ex}", "#BF616A")

    # ── Wwise audio playback / export (via bundled vgmstream) ─────────
    def _audio_wav(self, path: str) -> str | None:
        """Decode the selected .wem to a temp WAV via vgmstream. Returns the
        temp path or None (with a status message) on failure."""
        data = self._preview_bytes
        if data is None:                       # large asset — read on demand
            try:
                con = sqlite3.connect(self._index_path)
                try:
                    data = game_index.extract_asset(
                        con, path, str(self._game_dir))
                finally:
                    con.close()
            except Exception as ex:  # noqa: BLE001
                self._set_status(f"Read failed: {ex}", "#BF616A")
                return None
        import tempfile
        fd, wav = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        if not game_index.convert_to_wav(data, wav):
            self._set_status(
                "Could not decode audio — is vgmstream installed?", "#BF616A")
            try:
                os.unlink(wav)
            except OSError:
                pass
            return None
        return wav

    def _on_play_audio(self) -> None:
        """Decode the .wem off the UI thread (vgmstream is a subprocess and
        froze the window if run inline), then play + show a live timer."""
        path = self._selected_path()
        if not path:
            return
        self._play_token += 1
        token = self._play_token
        name = self._preview_name or os.path.basename(path)
        self._stop_playback()                 # halt any current sound + timer
        self._pv_play_btn.setEnabled(False)
        self._set_status(f"Decoding {name}…", "")
        th = QThread(self)
        w = _AudioDecodeWorker(
            self._index_path,
            str(self._game_dir) if self._game_dir else "",
            path, self._preview_bytes, name, token)
        w.moveToThread(th)
        th.started.connect(w.run)
        w.done.connect(self._on_audio_decoded)
        w.done.connect(th.quit)
        w.done.connect(w.deleteLater)
        th.finished.connect(th.deleteLater)
        job = (th, w)
        self._pv_jobs.append(job)
        th.finished.connect(
            lambda job=job: job in self._pv_jobs and self._pv_jobs.remove(job))
        th.start()

    def _on_audio_decoded(self, res: dict) -> None:
        if res.get("token") != self._play_token:
            # a newer Play (or a different asset) superseded this — drop the
            # stale temp WAV so decodes don't pile up in %TEMP%.
            if res.get("wav"):
                try:
                    os.unlink(res["wav"])
                except OSError:
                    pass
            return
        self._pv_play_btn.setEnabled(True)
        if res.get("error"):
            self._set_status(res["error"], "#BF616A")
            return
        self._start_playback(
            res.get("wav"), res.get("name", ""), res.get("dur", 0.0))

    def _start_playback(self, wav: str, name: str, dur: float) -> None:
        try:                                   # winsound is Windows-only
            import winsound
            winsound.PlaySound(None, winsound.SND_PURGE)      # stop any prior
            winsound.PlaySound(wav, winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception as ex:  # noqa: BLE001
            self._set_status(f"Playback failed: {ex}", "#BF616A")
            return
        import time
        from PySide6.QtCore import QTimer
        self._play_name = name
        self._play_dur = float(dur or 0.0)
        self._play_started = time.monotonic()
        if getattr(self, "_play_timer", None) is None:
            self._play_timer = QTimer(self)
            self._play_timer.setInterval(200)   # live countdown, 5×/second
            self._play_timer.timeout.connect(self._tick_play)
        self._play_timer.start()
        self._tick_play()                       # paint the timer immediately
        # temp WAV is left for the async player; the OS reclaims %TEMP%.

    def _tick_play(self) -> None:
        import time
        dur = getattr(self, "_play_dur", 0.0)
        name = getattr(self, "_play_name", "")
        elapsed = time.monotonic() - getattr(
            self, "_play_started", time.monotonic())
        if dur and elapsed >= dur:
            self._stop_playback()
            self._set_status(f"Finished: {name}", "")
            return
        if dur:
            self._set_status(
                f"▶ {name}   {elapsed:0.1f} / {dur:0.1f}s", "#2E7D32")
        else:
            self._set_status(f"▶ {name}   {elapsed:0.1f}s", "#2E7D32")

    def _stop_playback(self) -> None:
        """Stop the async sound + the live timer (new Play, or playback end)."""
        t = getattr(self, "_play_timer", None)
        if t is not None:
            t.stop()
        try:
            import winsound
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:  # noqa: BLE001
            pass

    def _on_export_wav(self) -> None:
        path = self._selected_path()
        if not path:
            return
        wav = self._audio_wav(path)
        if not wav:
            return
        base = os.path.splitext(
            self._preview_name or os.path.basename(path))[0]
        default = os.path.join(os.path.expanduser("~"), base + ".wav")
        out, _ = QFileDialog.getSaveFileName(
            self, "Save WAV", default, "WAV audio (*.wav)")
        try:
            if out:
                import shutil
                shutil.copyfile(wav, out)
                self._set_status(f"Saved WAV to {out}.", "#2E7D32")
        except Exception as ex:  # noqa: BLE001
            self._set_status(f"Save failed: {ex}", "#BF616A")
        finally:
            try:
                os.unlink(wav)
            except OSError:
                pass

    def _on_get_vgmstream(self) -> None:
        """One-click: download the right vgmstream build for this OS from the
        official GitHub releases and install it into the app's tools folder."""
        pkg = os.path.dirname(
            os.path.dirname(os.path.abspath(game_index.__file__)))  # …/cdumm
        dest = os.path.join(pkg, "tools", "vgmstream")
        self._pv_getvgm_btn.setEnabled(False)
        self._pv_getvgm_btn.setText("⬇  Downloading vgmstream…")
        self._set_status("Downloading vgmstream from GitHub…", "#2E7D32")
        th = QThread(self)
        w = _VgmDownloadWorker(dest)
        w.moveToThread(th)
        th.started.connect(w.run)
        w.done.connect(self._on_vgm_downloaded)
        w.done.connect(th.quit)
        w.done.connect(w.deleteLater)
        th.finished.connect(th.deleteLater)
        job = (th, w)
        self._pv_jobs.append(job)
        th.finished.connect(
            lambda job=job: job in self._pv_jobs and self._pv_jobs.remove(job))
        th.start()

    def _on_vgm_downloaded(self, ok: bool, msg: str) -> None:
        self._pv_getvgm_btn.setEnabled(True)
        self._pv_getvgm_btn.setText("⬇  Enable audio playback")
        if ok:
            self._set_status(
                f"vgmstream {msg} installed — audio playback enabled.",
                "#2E7D32")
            self._on_asset_selected()      # re-preview → Play/Export enable
        else:
            self._set_status(f"vgmstream setup failed: {msg}", "#BF616A")
