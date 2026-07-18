"""`.cdmod` (``crimson-mod-package`` v1) -> Format 3.

A new package format appearing on Nexus (GitHub #288). It is a zip:

    manifest.json
    patches/semantic.json
    reports/conversion.json      (optional, informational)

and its ``semantic.json`` is Format 3 wearing different key names. The
manifest says so itself: ``"source": {"format": "format3"}``.

    .cdmod semantic-patch          Format 3
    ---------------------------    ------------------
    targets[].operations[]         targets[].intents[]
    operation.path                 intent.field
    operation.selector.key         intent.key
    operation.selector.string_key  intent.entry
    operation.value                intent.new
    operation.op                   intent.op        (same)

So this module translates the envelope and hands off to the existing
Format 3 pipeline rather than adding a second decoder.

IMPORTANT -- why `.cdmod` must NOT just be mapped to "zip": a plain-zip
import would extract it, find `patches/semantic.json`, fail to recognise it
as Format 3 (no `intents`), and import a mod that changes nothing. That is
the exact silent-no-op shape of #259 / #275 / #278 / #285. Refuse loudly or
translate properly; never half-accept.
"""
from __future__ import annotations

import json
import logging
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

CDMOD_SUFFIX = ".cdmod"
_FORMAT = "crimson-mod-package"
_SEMANTIC = "semantic-patch"


class CdmodError(Exception):
    """The file claims to be a .cdmod but we can't faithfully translate it.

    Raised rather than returning a partial mod: a .cdmod we only half
    understand would import clean and silently under-apply.
    """


def is_cdmod(path: Path) -> bool:
    return path.suffix.lower() == CDMOD_SUFFIX and zipfile.is_zipfile(path)


def _read_json(zf: zipfile.ZipFile, name: str):
    try:
        return json.loads(zf.read(name).decode("utf-8-sig"))
    except KeyError:
        raise CdmodError(f"{name} is missing from the .cdmod package")
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise CdmodError(f"{name} is not readable JSON: {e}")


def _op_to_intent(op: dict, where: str) -> dict:
    if not isinstance(op, dict):
        raise CdmodError(f"{where}: operation is not an object")

    path = op.get("path")
    if not path:
        raise CdmodError(f"{where}: operation has no 'path'")

    sel = op.get("selector") or {}
    if not isinstance(sel, dict):
        raise CdmodError(f"{where}: 'selector' is not an object")

    key = sel.get("key")
    if key is None:
        # Without a key we cannot address a record. Do not guess.
        raise CdmodError(
            f"{where}: operation on {path!r} has no selector.key, so there "
            f"is no record to apply it to")

    # `entry` is REQUIRED by the Format 3 parser, even when empty -- it
    # raises "intent #0 is missing required key 'entry'" otherwise. Every
    # operation in the real No Fall Damage package happens to carry a
    # string_key, so omitting it when absent looked fine against that one
    # file and only showed up against a synthetic package with none. Emit it
    # unconditionally; "" is what CDUMM's own mods use when there's no name.
    return {
        "key": key,
        "field": path,
        "op": op.get("op", "set"),
        "new": op.get("value"),
        "entry": sel.get("string_key") or "",
    }


def cdmod_to_format3(path: Path) -> dict:
    """Translate a .cdmod into a Format 3 (v3.1, multi-target) document.

    Raises CdmodError rather than returning something partial.
    """
    if not zipfile.is_zipfile(path):
        raise CdmodError("not a zip archive")

    with zipfile.ZipFile(path) as zf:
        manifest = _read_json(zf, "manifest.json")
        if not isinstance(manifest, dict):
            raise CdmodError("manifest.json is not an object")

        fmt = manifest.get("format")
        if fmt != _FORMAT:
            raise CdmodError(
                f"unknown package format {fmt!r} (expected {_FORMAT!r})")

        ver = manifest.get("format_version")
        if ver != 1:
            # A v2 could reshape operations; translating it blind would
            # silently drop or mis-map fields.
            raise CdmodError(
                f"unsupported {_FORMAT} format_version {ver!r} — CDUMM only "
                f"knows version 1; refusing rather than guessing the layout")

        components = manifest.get("components") or []
        if not isinstance(components, list):
            raise CdmodError("manifest 'components' is not a list")

        targets: list[dict] = []
        seen_kinds: set[str] = set()
        for comp in components:
            if not isinstance(comp, dict):
                continue
            kind = comp.get("type")
            seen_kinds.add(str(kind))
            if kind != _SEMANTIC:
                continue
            cpath = comp.get("path")
            if not cpath:
                raise CdmodError(f"{_SEMANTIC} component has no 'path'")

            doc = _read_json(zf, cpath)
            for t in (doc.get("targets") or []):
                file = t.get("file")
                if not file:
                    raise CdmodError(f"{cpath}: target has no 'file'")
                ops = t.get("operations") or []
                intents = [
                    _op_to_intent(op, f"{cpath}:{file}[{i}]")
                    for i, op in enumerate(ops)
                ]
                if intents:
                    targets.append({"file": file, "intents": intents})

    if not targets:
        raise CdmodError(
            "no semantic-patch operations found (components: "
            + ", ".join(sorted(seen_kinds) or ["none"]) + ")")

    mi = {
        "title": manifest.get("name") or path.stem,
        "version": str(manifest.get("version") or ""),
        "author": manifest.get("author") or "",
        "description": manifest.get("description") or "",
    }
    n = sum(len(t["intents"]) for t in targets)
    logger.info(
        "cdmod: translated %r -> Format 3: %d target(s), %d intent(s)",
        path.name, len(targets), n)
    return {
        "format": 3,
        "format_minor": 1,
        "modinfo": mi,
        "targets": targets,
    }
