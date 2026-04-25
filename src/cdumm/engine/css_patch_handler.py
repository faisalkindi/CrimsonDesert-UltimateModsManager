"""Partial CSS patch + merge support — JMM v9.9.3 parity.

Ports JMM's ``CssPatchApplier.cs`` to Python so mods authored for JMM's
``*.css.patch`` and ``*.css.merge`` formats import and apply identically
in CDUMM. Both file formats target a single CSS file and produce a
modified body that ships through the overlay engine.

File formats:

* ``*.css.patch`` — directive-driven. Selectors come with a current op
  (``merge`` is the default) set by CSS-comment directives::

      /* @merge */     -> next rule(s) merged property-by-property
      /* @replace */   -> next rule replaces the entire body
      /* @add */       -> rule added only if selector doesn't exist
      /* @remove ".target-selector" */   -> inline removal of a rule

* ``*.css.merge`` — every rule in the file is a merge op (no directives
  needed). Syntactic sugar for the common case.

Operations:

* ``merge``    — splice mod's properties into the existing rule body.
                 Existing properties NOT named in the mod stay intact;
                 properties the mod names override or add.
* ``replace``  — replace the rule's entire body. If the selector
                 doesn't exist, the rule is appended fresh.
* ``add``      — append rule iff selector is not already present;
                 otherwise skip silently.
* ``remove``   — delete the rule (and its trailing newline).

Selector matching: case-sensitive after whitespace normalisation
(collapsed runs of whitespace to a single space, trim).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ── Data classes ─────────────────────────────────────────────────────


@dataclass
class CssPatchOp:
    op: str = "merge"
    selector: str = ""
    raw_block: str = ""
    source_mod_name: str = ""


@dataclass
class _CssRule:
    selector: str
    start_index: int
    brace_open: int
    brace_close: int
    body: str


# ── Detection ────────────────────────────────────────────────────────


def detect_patch_file(path: Path) -> Optional[str]:
    """Return ``"css_patch"`` for ``*.css.patch`` files, ``"css_merge"``
    for ``*.css.merge`` files, ``None`` otherwise. Detection is purely
    extension-based — JMM's convention. Anything else is left to the
    XML or generic handler.
    """
    name = path.name.lower()
    if name.endswith(".css.patch"):
        return "css_patch"
    if name.endswith(".css.merge"):
        return "css_merge"
    return None


def derive_target_from_patch_path(patch_path: Path,
                                  mod_root: Path) -> Optional[str]:
    """``foo/bar.css.patch`` -> ``foo/bar.css`` relative to ``mod_root``."""
    try:
        rel = Path(patch_path).relative_to(mod_root)
    except ValueError:
        return None
    name = rel.name
    low = name.lower()
    if low.endswith(".css.patch"):
        name = name[: -len(".patch")]
    elif low.endswith(".css.merge"):
        name = name[: -len(".merge")]
    target = rel.with_name(name)
    return target.as_posix()


# ── Public apply API ─────────────────────────────────────────────────


def apply_patches(original_bytes: bytes,
                  patch_files: list[tuple[str, Path]]
                  ) -> tuple[Optional[bytes], list[str]]:
    """Apply ``*.css.patch`` files against vanilla CSS bytes.

    ``patch_files`` is a list of ``(mod_name, patch_path)`` tuples — the
    same shape JMM uses. Returns ``(new_bytes, log_lines)``. Returns
    ``(None, log)`` only on a UTF-8 decode failure of the original.

    Multiple patch files are applied in the order given (caller must
    pre-sort by priority: lower priority first so higher-priority mods
    overwrite later).
    """
    log: list[str] = []
    try:
        text = _decode_utf8(original_bytes)
    except Exception as e:
        log.append(f"    [CSS] ERROR: Cannot decode original CSS as UTF-8: {e}")
        return None, log

    ops: list[CssPatchOp] = []
    for mod_name, path in patch_files:
        try:
            content = Path(path).read_text(encoding="utf-8")
            parsed = parse_patch_file(content, mod_name)
            ops.extend(parsed)
            log.append(
                f"    [CSS] Loaded {len(parsed)} op(s) from "
                f"{Path(path).name} ({mod_name})")
        except Exception as e:
            log.append(f"    [CSS] ERROR loading {path}: {e}")

    if not ops:
        log.append("    [CSS] No ops to apply.")
        return original_bytes, log

    applied = added = skipped = 0
    for op in ops:
        label = f"[{op.source_mod_name}] {op.op} '{op.selector}'"
        try:
            if op.op == "merge":
                new_text = _apply_merge_op(text, op, log, label)
            elif op.op == "replace":
                new_text = _apply_replace_op(text, op, log, label)
            elif op.op == "add":
                new_text = _apply_add_op(text, op, log, label)
            elif op.op == "remove":
                new_text = _apply_remove_op(text, op, log, label)
            else:
                new_text = None

            if new_text is None:
                log.append(f"    [CSS] SKIP {label}: rule not found or unknown op")
                skipped += 1
                continue
            if new_text == text:
                skipped += 1
                continue
            text = new_text
            if op.op == "add":
                added += 1
            applied += 1
        except Exception as e:
            log.append(f"    [CSS] ERROR {label}: {e}")
            skipped += 1

    log.append(f"    [CSS] {applied} applied ({added} added), {skipped} skipped")
    return text.encode("utf-8"), log


def apply_merge(original_bytes: bytes,
                merge_files: list[tuple[str, Path]]
                ) -> tuple[Optional[bytes], list[str]]:
    """Apply ``*.css.merge`` files against vanilla CSS bytes. Every rule
    in each merge file becomes a ``merge`` op."""
    log: list[str] = []
    try:
        text = _decode_utf8(original_bytes)
    except Exception as e:
        log.append(f"    [CSS] ERROR: Cannot decode original CSS as UTF-8: {e}")
        return None, log

    applied = skipped = 0
    for mod_name, path in merge_files:
        try:
            content = Path(path).read_text(encoding="utf-8")
        except Exception as e:
            log.append(f"    [CSS] ERROR loading merge {path}: {e}")
            continue
        rules = _parse_rules(content)
        log.append(
            f"    [CSS-merge] {Path(path).name} ({mod_name}): "
            f"{len(rules)} rule(s)")
        for rule in rules:
            op = CssPatchOp(
                op="merge", selector=rule.selector,
                raw_block=rule.body, source_mod_name=mod_name)
            label = f"[{mod_name}] merge '{op.selector}'"
            new_text = _apply_merge_op(text, op, log, label)
            if new_text is None or new_text == text:
                skipped += 1
                continue
            text = new_text
            applied += 1

    log.append(f"    [CSS-merge] {applied} applied, {skipped} skipped")
    return text.encode("utf-8"), log


# ── Patch file parsing ───────────────────────────────────────────────


def parse_patch_file(content: str, mod_name: str) -> list[CssPatchOp]:
    """Parse a ``*.css.patch`` file. Default op is ``merge``;
    ``/* @replace */``, ``/* @add */`` directives switch the op for
    the next rule. ``/* @remove "selector" */`` is an inline op with
    no rule body."""
    ops: list[CssPatchOp] = []
    i = 0
    current_op = "merge"
    n = len(content)

    while i < n:
        # skip whitespace
        while i < n and content[i].isspace():
            i += 1
        if i >= n:
            break
        # comment / directive
        if i + 1 < n and content[i] == "/" and content[i + 1] == "*":
            close = content.find("*/", i + 2)
            if close < 0:
                break
            text = content[i + 2:close].strip()
            i = close + 2
            if not text.startswith("@"):
                continue
            low = text.lower()
            if low.startswith("@remove"):
                sel = text[len("@remove"):].strip().strip('"').strip()
                if sel:
                    ops.append(CssPatchOp(
                        op="remove", selector=sel,
                        source_mod_name=mod_name))
            elif low == "@merge":
                current_op = "merge"
            elif low == "@replace":
                current_op = "replace"
            elif low == "@add":
                current_op = "add"
            continue

        # rule: selector { body }
        sel_start = i
        brace_open = _find_brace_open(content, i)
        if brace_open < 0:
            break
        brace_close = _find_brace_close(content, brace_open)
        if brace_close < 0:
            break
        selector = content[sel_start:brace_open].strip()
        raw_block = content[brace_open + 1:brace_close]
        ops.append(CssPatchOp(
            op=current_op,
            selector=_normalise_selector(selector),
            raw_block=raw_block,
            source_mod_name=mod_name))
        # JMM resets the directive state to "merge" after each rule.
        current_op = "merge"
        i = brace_close + 1

    return ops


# ── Operations ───────────────────────────────────────────────────────


def _apply_merge_op(text: str, op: CssPatchOp,
                    log: list[str], label: str) -> Optional[str]:
    rule = _find_rule(text, op.selector)
    if rule is None:
        log.append(
            f"    [CSS] {label}: selector not found "
            "— appending as new rule")
        return _append_rule(text, op.selector, op.raw_block)
    existing = _parse_properties(rule.body)
    incoming = _parse_properties(op.raw_block)
    existing.update(incoming)
    new_body = _serialize_properties(existing, rule.body)
    return _splice_body(text, rule, new_body)


def _apply_replace_op(text: str, op: CssPatchOp,
                      log: list[str], label: str) -> Optional[str]:
    rule = _find_rule(text, op.selector)
    if rule is None:
        log.append(f"    [CSS] {label}: selector not found — appending instead")
        return _append_rule(text, op.selector, op.raw_block)
    return _splice_body(text, rule, op.raw_block)


def _apply_add_op(text: str, op: CssPatchOp,
                  log: list[str], label: str) -> Optional[str]:
    if _find_rule(text, op.selector) is not None:
        log.append(
            f"    [CSS] {label}: selector already exists — skipped "
            "(use @merge or @replace)")
        return text  # signal "no change"; caller treats as skip
    return _append_rule(text, op.selector, op.raw_block)


def _apply_remove_op(text: str, op: CssPatchOp,
                     log: list[str], label: str) -> Optional[str]:
    rule = _find_rule(text, op.selector)
    if rule is None:
        return text
    end = rule.brace_close + 1
    if end < len(text) and text[end] == "\r":
        end += 1
    if end < len(text) and text[end] == "\n":
        end += 1
    return text[:rule.start_index] + text[end:]


# ── CSS scanner ──────────────────────────────────────────────────────


def _parse_rules(text: str) -> list[_CssRule]:
    rules: list[_CssRule] = []
    n = len(text)
    i = 0
    while i < n:
        # skip whitespace + block comments
        while i < n:
            if text[i].isspace():
                i += 1
                continue
            if i + 1 < n and text[i] == "/" and text[i + 1] == "*":
                close = text.find("*/", i + 2)
                if close < 0:
                    return rules
                i = close + 2
                continue
            break
        if i >= n:
            break
        sel_start = i
        brace_open = _find_brace_open(text, i)
        if brace_open < 0:
            break
        brace_close = _find_brace_close(text, brace_open)
        if brace_close < 0:
            break
        selector = text[sel_start:brace_open].strip()
        body = text[brace_open + 1:brace_close]
        rules.append(_CssRule(
            selector=_normalise_selector(selector),
            start_index=sel_start,
            brace_open=brace_open,
            brace_close=brace_close,
            body=body))
        i = brace_close + 1
    return rules


def _find_rule(text: str, target_selector: str) -> Optional[_CssRule]:
    rules = _parse_rules(text)
    norm = _normalise_selector(target_selector)
    for r in rules:
        if r.selector == norm:
            return r
    return None


def _find_brace_open(text: str, start: int) -> int:
    n = len(text)
    i = start
    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            close = text.find("*/", i + 2)
            if close < 0:
                return -1
            i = close + 2
            continue
        if c in ("\"", "'"):
            i = _skip_string(text, i) + 1
            continue
        if c == "{":
            return i
        if c in (";", "}"):
            return -1
        i += 1
    return -1


def _find_brace_close(text: str, brace_open: int) -> int:
    n = len(text)
    depth = 1
    i = brace_open + 1
    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            close = text.find("*/", i + 2)
            if close < 0:
                return -1
            i = close + 2
            continue
        if c in ("\"", "'"):
            i = _skip_string(text, i) + 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _skip_string(text: str, start: int) -> int:
    quote = text[start]
    i = start + 1
    n = len(text)
    while i < n:
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == quote:
            return i
        i += 1
    return n - 1


# ── Properties (the merge unit) ──────────────────────────────────────


def _parse_properties(body: str) -> dict[str, str]:
    """Split a ``{ ... }`` body into a property dict.

    Strips block comments. Properties are case-insensitive on the key
    side (matches JMM's StringComparer.OrdinalIgnoreCase). Last
    occurrence wins for duplicates.
    """
    cleaned = re.sub(r"/\*.*?\*/", "", body, flags=re.DOTALL)
    props: dict[str, str] = {}
    for chunk in _split_top_level(cleaned, ";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        colon = chunk.find(":")
        if colon <= 0:
            continue
        key = chunk[:colon].strip()
        value = chunk[colon + 1:].strip()
        if not key:
            continue
        # Preserve the FIRST-seen casing of duplicate keys but write the
        # latest value, matching dict-update semantics.
        existing_key = next(
            (k for k in props if k.lower() == key.lower()), None)
        if existing_key is not None:
            props[existing_key] = value
        else:
            props[key] = value
    return props


def _split_top_level(text: str, sep: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    last = 0
    for i, c in enumerate(text):
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == sep and depth == 0:
            parts.append(text[last:i])
            last = i + 1
    if last < len(text):
        parts.append(text[last:])
    return parts


def _serialize_properties(props: dict[str, str], original_body: str) -> str:
    """Emit a body block preserving the indent + line-ending style of
    the original. Mirrors JMM's serialiser so our output looks the
    same after a round trip."""
    indent = "    "
    m = re.search(r"\n([ \t]+)\S", original_body)
    if m:
        indent = m.group(1)
    newline = "\r\n" if "\r\n" in original_body else "\n"
    out: list[str] = [newline]
    for key, value in props.items():
        out.append(f"{indent}{key}: {value};{newline}")
    return "".join(out)


# ── Splicing helpers ─────────────────────────────────────────────────


def _splice_body(text: str, rule: _CssRule, new_body: str) -> str:
    return text[:rule.brace_open + 1] + new_body + text[rule.brace_close:]


def _append_rule(text: str, selector: str, body: str) -> str:
    out = [text]
    if text and not text.endswith("\n"):
        out.append("\n")
    out.append("\n")
    out.append(selector)
    out.append(" {")
    out.append(body)
    if not body.endswith("\n"):
        out.append("\n")
    out.append("}\n")
    return "".join(out)


def _normalise_selector(selector: str) -> str:
    cleaned = re.sub(r"/\*.*?\*/", "", selector, flags=re.DOTALL)
    return re.sub(r"\s+", " ", cleaned).strip()


def _decode_utf8(data: bytes) -> str:
    if len(data) >= 3 and data[0] == 0xEF and data[1] == 0xBB and data[2] == 0xBF:
        return data[3:].decode("utf-8")
    return data.decode("utf-8")


# ── Overlay batch processor ──────────────────────────────────────────


def process_css_patches_for_overlay(
        patch_items: list[dict], game_dir) -> list[tuple[bytes, dict]]:
    """Apply all *.css.patch / *.css.merge mods against the CSS files
    they target and emit (content_bytes, metadata) tuples ready for
    the overlay builder.

    Mirrors process_xml_patches_for_overlay's pattern:
      * group by target file_path
      * sort each group by priority ASC (higher number = later = wins)
      * extract vanilla bytes via PAMT lookup
      * apply patches first, then merges
      * emit overlay entry only if bytes actually changed

    patch_items: list of dicts with keys mod_id, mod_name, kind
    ('css_patch' | 'css_merge'), delta_path, file_path, priority.
    """
    from cdumm.engine.json_patch_handler import (
        _find_pamt_entry, _extract_from_paz,
    )

    game_dir = Path(game_dir)
    vanilla_dir = game_dir / "CDMods" / "vanilla"
    if not vanilla_dir.exists():
        vanilla_dir = game_dir

    by_target: dict[str, list[dict]] = {}
    for item in patch_items:
        fp = (item.get("file_path") or "").strip()
        if not fp:
            continue
        by_target.setdefault(fp, []).append(item)

    out: list[tuple[bytes, dict]] = []
    for target_path, items in by_target.items():
        items.sort(key=lambda d: (
            int(d.get("priority") or 0),
            int(d.get("mod_id") or 0)))
        target_basename = (target_path.split("/", 1)[-1]
                           if "/" in target_path else target_path)
        entry = (_find_pamt_entry(target_basename, vanilla_dir)
                 or _find_pamt_entry(target_basename, game_dir))
        if entry is None:
            logger.warning("css_patch: target not found in PAMT — %s",
                           target_path)
            continue
        try:
            vanilla_bytes = _extract_from_paz(entry)
        except Exception as e:
            logger.error("css_patch: extract failed for %s: %s",
                         target_path, e)
            continue

        patches: list[tuple[str, Path]] = []
        merges: list[tuple[str, Path]] = []
        for it in items:
            kind = it.get("kind")
            dp = Path(it.get("delta_path") or "")
            if not dp.exists():
                logger.warning("css_patch: patch file missing: %s", dp)
                continue
            mod_name = it.get("mod_name") or dp.stem
            if kind == "css_patch":
                patches.append((mod_name, dp))
            elif kind == "css_merge":
                merges.append((mod_name, dp))

        current = bytes(vanilla_bytes)
        if patches:
            new, log = apply_patches(current, patches)
            for line in log:
                logger.info(line)
            if new is not None:
                current = new
        if merges:
            new, log = apply_merge(current, merges)
            for line in log:
                logger.info(line)
            if new is not None:
                current = new

        if current == vanilla_bytes:
            logger.debug("css_patch: no net change for %s, skipping",
                         target_path)
            continue

        pamt_dir = Path(entry.paz_file).parent.name
        meta: dict = {
            "entry_path": entry.path,
            "pamt_dir": pamt_dir,
            "compression_type": entry.compression_type,
        }
        if getattr(entry, "encrypted", False):
            meta["encrypted"] = True
            meta["crypto_filename"] = entry.path.rsplit("/", 1)[-1]
            meta["vanilla_flags"] = entry.flags & 0xFFFF
        out.append((bytes(current), meta))
        logger.info(
            "css_patch: produced overlay entry for %s (%d bytes) "
            "from %d file(s)", target_path, len(current), len(items))

    return out
