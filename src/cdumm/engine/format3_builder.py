"""Mod-maker foundation — turn staged Game Data edits into a Format 3
(``.field.json``) mod, and optionally import it.

This is the Qt-free bridge between the Game Data tab's (future) editable
grid and CDUMM's existing Format 3 pipeline. It assembles the exact
``.field.json`` shape that ``import_from_natt_format_3`` already knows how
to validate (``validate_intents``) and apply, so every safety layer is
inherited unchanged:

* schema / ``field_schema`` resolution decides whether a field is writable;
* the verified-fields gate (``TableSchema.verified_fields``) makes
  ``validate_intents`` *skip* any edit to an unverified field; and
* the round-trip apply is the same one downloaded mods go through.

The builder holds no offsets, no struct packing and no apply logic of its
own — it only serialises intents. That keeps the mod maker safe by
construction: it can never write bytes the engine wouldn't already accept
from a hand-authored mod.
"""
from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class FieldEdit:
    """One staged edit captured from the Game Data grid.

    ``target`` is the ``.pabgb`` table (e.g. ``"wantedinfo.pabgb"``);
    ``entry``/``key`` identify the record (display name + numeric id);
    ``field`` is the schema field name; and ``new`` is the replacement
    value, whose type must match the field (int / float / str / list) —
    the caller validates that against the ``FieldSpec`` before staging.
    ``old`` is optional, used for display/audit only, and is never written
    into the mod.
    """

    target: str
    entry: str
    key: int
    field: str
    new: object
    old: object = None


def _safe_filename(name: str) -> str:
    stem = re.sub(r"[^\w.\- ]+", "", str(name)).strip().replace(" ", "_")
    return stem or "cdumm_mod"


def build_format3_json(
    edits: Iterable[FieldEdit],
    *,
    title: str,
    author: str = "CDUMM Mod Maker",
    version: str = "1.0",
    description: str | None = None,
) -> dict:
    """Assemble a Format 3 mod ``dict`` from one or more :class:`FieldEdit`.

    A Format 3 mod carries a single ``target`` table, so every edit must
    target the same table. Raises ``ValueError`` on no edits or mixed
    targets. ``old`` is intentionally dropped — it is display metadata, not
    part of the mod.
    """
    edits = list(edits)
    if not edits:
        raise ValueError("no edits — nothing to build a mod from")
    targets = sorted({e.target for e in edits})
    if len(targets) != 1:
        raise ValueError(
            "all edits must target one table; got " + ", ".join(targets))

    intents = [
        {
            "entry": e.entry,
            "key": int(e.key),
            "field": e.field,
            "op": "set",
            "new": e.new,
        }
        for e in edits
    ]
    return {
        "modinfo": {
            "title": title,
            "version": version,
            "author": author,
            "description": description
            or f"{len(intents)} field edit(s) made with CDUMM's mod maker",
            "note": "Format 3 — created with CDUMM's in-app mod maker",
        },
        "format": 3,
        "target": targets[0],
        "intents": intents,
    }


def write_field_json(mod: dict, out_path: str | Path) -> Path:
    """Serialise a Format 3 mod ``dict`` to ``out_path`` as UTF-8 JSON.

    The *export* primitive — a future "Export .field.json" button calls
    this so a user can save/share the mod without importing it. Parent
    directories are created as needed.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(mod, indent=2, ensure_ascii=False), encoding="utf-8")
    return out_path


def create_mod_from_edits(
    edits: Iterable[FieldEdit],
    *,
    title: str,
    game_dir: Path,
    db,
    snapshot,
    deltas_dir: Path,
    author: str = "CDUMM Mod Maker",
    existing_mod_id: int | None = None,
):
    """Build a Format 3 mod from ``edits``, write it to a ``.field.json``
    under ``deltas_dir``, and import it through the normal Format 3 path.

    Returns the ``ModImportResult`` from ``import_from_natt_format_3``. Its
    ``.error`` is set if the engine rejected the edits — e.g. an edit to an
    unverified or unsupported field, which ``validate_intents`` skips — so
    the caller (GUI) should surface ``.error`` and, on success, refresh the
    mod list from the returned ``mod_id``.
    """
    # Imported lazily so this module stays importable (and unit-testable)
    # without pulling the full import handler in at module load.
    from cdumm.engine.import_handler import import_from_natt_format_3

    mod = build_format3_json(edits, title=title, author=author)

    Path(deltas_dir).mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="cdumm_modmaker_", dir=str(deltas_dir)))
    json_path = write_field_json(
        mod, stage / f"{_safe_filename(title)}.field.json")

    return import_from_natt_format_3(
        json_path=json_path,
        game_dir=Path(game_dir),
        db=db,
        snapshot=snapshot,
        deltas_dir=Path(deltas_dir),
        existing_mod_id=existing_mod_id,
    )
