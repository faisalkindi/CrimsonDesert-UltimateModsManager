"""Game Data page — build + browse a searchable catalog of the installed game.

A tool page over ``cdumm.engine.game_index``: one button builds (or refreshes)
a SQLite index of every archive asset + the keyed game-data tables, on a
background thread. You can choose where the index file is saved, open its
folder, and search the indexed assets in a table right in the app.

Strings are literal (not tr() keys) for this first version, so it adds no new
localization keys; localization is a follow-up.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (QDialog, QFileDialog, QHBoxLayout, QHeaderView,
                               QLabel, QScrollArea, QSizePolicy, QSplitter,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from cdumm.engine import game_index
from cdumm.gui.pages.tool_page import ToolPageBase
from cdumm.platform import IS_MACOS, IS_WINDOWS


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


def _shape_records(records: dict, schema) -> tuple[list, list, int, float]:
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
    cols = ["_key", "_name"] + field_names
    keys = sorted(records)[:_GRID_ROW_CAP]
    rows = [[_cell(records[k].get(c)) for c in cols] for k in keys]

    suspect = 0
    sample = keys[:60]
    for fn in field_names:
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
    health = (suspect / len(field_names)) if field_names else 0.0
    return cols, rows, len(records), health


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
                    recs = sem.parse_records(table, body, header)
                except Exception:  # noqa: BLE001 — fall back to a raw view
                    recs = {}
                if recs:
                    cols, rows, total, health = _shape_records(
                        recs, sem.get_schema(table))
                    res.update(kind="table", table=table, cols=cols,
                               rows=rows, total=total, health=health)
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
        # Reflection-serialized binaries (.paseq/.prefab/.meshinfo/...) embed
        # their field/type/object names as text — surface those as a readable
        # outline instead of raw hex.
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

        # Most portable way to get a QImage into a Qt3D texture across PySide6
        # builds: a temp PNG loaded by QTextureLoader (a painted-texture
        # subclass rendered nothing on this build).
        fd, self._tmp_png = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        qimage.save(self._tmp_png, "PNG")

        self._window = _E.Qt3DWindow()
        self._window.defaultFrameGraph().setClearColor(QColor("#20222E"))
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
        bar.addWidget(CaptionLabel("Drag to orbit · scroll to zoom", self))
        root_layout.addLayout(bar)
        root_layout.addWidget(container, 1)

        self._root = _C.QEntity()
        self._cam = self._window.camera()
        self._cam.lens().setPerspectiveProjection(45.0, 1.2, 0.1, 1000.0)
        self._cam.setPosition(QVector3D(0, 0, 3.2))
        self._cam.setViewCenter(QVector3D(0, 0, 0))
        ctrl = _E.QOrbitCameraController(self._root)
        ctrl.setCamera(self._cam)
        ctrl.setLinearSpeed(60.0)
        ctrl.setLookSpeed(180.0)

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
        self._keep = [ctrl, tex, mat]
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

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        try:
            w = max(1, self._window.width())
            h = max(1, self._window.height())
            self._cam.lens().setPerspectiveProjection(45.0, w / h, 0.1, 1000.0)
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
        self._table.setColumnCount(4)
        self._table.setHorizontalHeaderLabels(
            ["Path", "Archive", "Type", "Size (bytes)"])
        self._table.verticalHeader().hide()
        self._table.setEditTriggers(self._table.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(
            self._table.SelectionBehavior.SelectRows)
        # Larger, more readable text + roomier rows.
        _tf = self._table.font()
        _tf.setPixelSize(15)
        self._table.setFont(_tf)
        _hdr = self._table.horizontalHeader()
        _hf = _hdr.font()
        _hf.setPixelSize(15)
        _hdr.setFont(_hf)
        # Path stretches to fill the slack; the other three get fixed widths.
        # This keeps the columns spanning the full table width (so the vertical
        # scrollbar hugs the last column) WITHOUT ResizeToContents, which
        # re-measures every row on every update and can stall the UI thread.
        _hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for _c in (1, 2, 3):
            _hdr.setSectionResizeMode(_c, QHeaderView.ResizeMode.Interactive)
        self._table.setColumnWidth(1, 90)
        self._table.setColumnWidth(2, 140)
        self._table.setColumnWidth(3, 130)
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
        self._pv_grid.setEditTriggers(self._pv_grid.EditTrigger.NoEditTriggers)
        _gf = self._pv_grid.font()
        _gf.setPixelSize(13)
        self._pv_grid.setFont(_gf)
        self._pv_grid.verticalHeader().setDefaultSectionSize(30)
        self._pv_grid.setVisible(False)
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

        # "View in 3D" — only shown for image previews; opens a pop-up with
        # the texture on a rotatable sphere/cube (Qt3D).
        self._pv_3d_btn = PushButton("View in 3D  ⬤", pane)
        self._pv_3d_btn.setVisible(False)
        self._pv_3d_btn.clicked.connect(self._on_view_3d)
        pv.addWidget(self._pv_3d_btn)

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

        detail = ""
        try:
            con = sqlite3.connect(self._index_path)
            try:
                tables = game_index.list_data_tables(con)[:12]
            finally:
                con.close()
            detail = "\n".join(
                f"• {t['name']}  ({t['orig_size']:,} bytes)"
                for t in tables)
        except Exception as ex:  # noqa: BLE001
            detail = f"(could not read tables: {ex})"
        card = self._add_result_card("Largest keyed game-data tables", detail)
        card.setMaximumWidth(600)   # hug the content instead of spanning the page
        self._results_layout.setAlignment(card, Qt.AlignLeft)
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

    def _on_search(self, text: str) -> None:
        text = (text or "").strip()
        if not os.path.exists(self._index_path):
            self._hits.setText("Build the index first to search.")
            self._table.setRowCount(0)
            return
        if len(text) < 2:
            self._hits.setText("Type at least 2 characters to search.")
            self._table.setRowCount(0)
            return
        limit = self._result_limit()
        try:
            con = sqlite3.connect(self._index_path)
            try:
                rows = game_index.search_assets(con, query=text, limit=limit)
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
        self._table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self._table.setItem(i, 0, QTableWidgetItem(str(r["path"])))
            self._table.setItem(i, 1, QTableWidgetItem(str(r["archive"])))
            self._table.setItem(i, 2, QTableWidgetItem(str(r["ext"])))
            self._table.setItem(
                i, 3, QTableWidgetItem(f"{int(r['orig_size']):,}"))

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
        cell = self._table.item(items[0].row(), 0)
        return cell.text() if cell else None

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
            self._pv_meta.setText(
                f"data table “{res.get('table', '')}”  ·  {total:,} records"
                f"{more}{note}\n{res.get('path', '')}")
            self._show_grid(res.get("cols", []), res.get("rows", []))
            self._pv_extract.setEnabled(True)
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

        self._pv_meta.setText(f"{meta}\n{res.get('path', '')}" if meta
                              else res.get("path", ""))
        if kind == "text":
            body = res.get("text", "")
            if res.get("truncated"):
                body += ("\n\n… (truncated preview — use Extract raw for the "
                         "full file)")
            self._show_text(body, wrap=True)
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

    def _show_struct(self, res: dict) -> None:
        total = res.get("total_words", 0)
        shown = res.get("shown", 0)
        trailing = res.get("trailing", 0)
        more = f" · first {shown:,} of {total:,}" if shown < total else ""
        tail = f" · +{trailing} trailing byte(s)" if trailing else ""
        self._pv_meta.setText(
            "Struct view — this format has no embedded field names, so each "
            "32-bit word is shown as unsigned / signed / float. Values in the "
            "1,000,000+ range are flagged as likely record keys."
            f"  ({total:,} words{more}{tail})\n"
            + self._pv_meta.text())
        cols = ["Offset", "Bytes", "UInt32", "Int32", "Float32", "ASCII", ""]
        rows = [[r[0], r[1], r[2], r[3], r[4], r[5], "key" if r[6] else ""]
                for r in res.get("rows", [])]
        self._show_grid(cols, rows)

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
            body = ("Wwise SoundBank (.bnk)\n\n"
                    f"Bank version       {a.get('bank_version')}\n"
                    f"Sections           {', '.join(a.get('sections', []))}\n"
                    f"Embedded streams   {a.get('embedded_streams', 0)}\n")
            playable = False   # banks hold many subsongs — extract, don't play
        note = ("\nThe game ships all sound through Wwise: .wem are the encoded "
                "streams (Wwise Vorbis) and .bnk are SoundBanks — Windows can't "
                "play these directly.\n")
        if has_vgm:
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
        self._pv_3d_btn.setVisible(
            self._pv_qimage is not None and not self._pv_qimage.isNull())
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
        """Open the current texture on a rotatable sphere/cube (pop-up)."""
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
        path = self._selected_path()
        if not path:
            return
        wav = self._audio_wav(path)
        if not wav:
            return
        name = self._preview_name or os.path.basename(path)
        try:                                   # winsound is Windows-only
            import winsound
            winsound.PlaySound(None, winsound.SND_PURGE)      # stop any prior
            winsound.PlaySound(wav, winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception as ex:  # noqa: BLE001
            self._set_status(f"Playback failed: {ex}", "#BF616A")
            return
        # Read the decoded WAV's real length so we can flip the status back
        # to "finished" live when it ends (winsound gives no end callback).
        dur = 0.0
        try:
            import contextlib
            import wave
            with contextlib.closing(wave.open(wav, "rb")) as w:
                if w.getframerate():
                    dur = w.getnframes() / float(w.getframerate())
        except Exception:  # noqa: BLE001
            pass
        self._play_token += 1
        token = self._play_token
        tag = f"  ({dur:.1f}s)" if dur else ""
        self._set_status(f"▶ Playing: {name}{tag}", "#2E7D32")
        if dur > 0:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(
                int(dur * 1000) + 150,
                lambda t=token, n=name: self._on_play_finished(t, n))
        # temp WAV is left for the async player; the OS reclaims %TEMP%.

    def _on_play_finished(self, token: int, name: str) -> None:
        if token == self._play_token:      # not superseded by a newer Play
            self._set_status(f"Finished: {name}", "")

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
