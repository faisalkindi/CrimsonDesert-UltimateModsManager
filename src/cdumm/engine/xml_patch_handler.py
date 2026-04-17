"""XPath-based XML patch + identity-key merge handler for CDUMM.

Ports JMM V9.9.1 ``XmlPatchApplier`` (MPL-2.0) to Python / lxml. Applies
the same operation set — replace / add / add-before / add-after / remove
/ set-attr / remove-attr — against either JSON-encoded patch files
(``{"operations":[{"op":"replace","xpath":"//foo","value":"..."},...]}``)
or XML-encoded ``<xml-patch>`` documents. Also supports identity-key
merge for per-element attribute updates.

Key behaviours preserved from JMM:

  * BOM preserved (UTF-8 with BOM stays UTF-8 with BOM on output).
  * Multi-root XML fragments wrapped in a sentinel root during parsing,
    stripped back out on output.
  * ``</>`` shorthand closing tags rewritten to ``</tagname>`` before
    parsing (a Crimson-Desert-specific quirk).
  * Identity attribute priority on merge: Key > Name > Id > key > name
    > id, with fallback to any ``_key`` / ``_name`` / ``_id`` attribute
    found on the peer element.

Not ported (yet):

  * EXSLT regex extensions on XPath.
"""

from __future__ import annotations

import io
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from lxml import etree

logger = logging.getLogger(__name__)

_SENTINEL_ROOT = "__cdumm_root__"
_IDENTITY_ATTR_PRIORITY = ("Key", "Name", "Id", "key", "name", "id")
_KNOWN_OPS = {
    "replace", "add", "add-before", "add-after",
    "remove", "set-attr", "remove-attr",
}


@dataclass
class XmlPatchOp:
    op: str = ""
    xpath: str = ""
    value: Optional[str] = None
    attribute: Optional[str] = None
    comment: Optional[str] = None
    # Deferred ``find``/``key=`` form (JMM XmlPatchApplier.cs:617-662).
    # When set, the XPath is resolved against the live doc at apply time so
    # identity attribute priority (Key/Name/Id/…) matches JMM exactly.
    find_tag: Optional[str] = None
    find_key: Optional[str] = None


@dataclass
class XmlPatchFile:
    operations: list[XmlPatchOp] = field(default_factory=list)


# ── Detection ────────────────────────────────────────────────────────

def detect_patch_file(path: Path) -> Optional[str]:
    """Identify a file as an XML patch or merge.

    Returns ``"xml_patch"``, ``"xml_merge"``, or ``None``. JMM conventions:
      * ``*.xml.patch`` / ``*.json.patch`` / ``*.xml.merge`` extensions are
        strong hints.
      * Otherwise the file contents are sniffed: JSON ``{"operations": [...]}``
        or XML root ``<xml-patch>`` → patch; XML root ``<xml-merge>`` → merge.
    """
    path = Path(path)
    low = path.name.lower()
    if low.endswith(".xml.patch") or low.endswith(".json.patch"):
        return "xml_patch"
    if low.endswith(".xml.merge") or low.endswith(".json.merge"):
        return "xml_merge"
    if not path.is_file():
        return None
    # Cap the probe size — most patch files are < 256 KB; anything bigger
    # is unlikely to be a patch and sniffing slows down scans.
    try:
        head = path.read_bytes()[:8192].decode("utf-8", errors="replace")
    except Exception:
        return None
    stripped = head.lstrip()
    if stripped.startswith("<"):
        try:
            root = etree.fromstring(head.encode("utf-8"),
                                    parser=etree.XMLParser(recover=True))
            if root is None:
                return None
            tag = root.tag.lower() if isinstance(root.tag, str) else ""
            if tag == "xml-patch":
                return "xml_patch"
            if tag == "xml-merge":
                return "xml_merge"
        except etree.XMLSyntaxError:
            return None
        return None
    if stripped.startswith("{"):
        try:
            data = json.loads(head)
        except json.JSONDecodeError:
            # The truncated 8-KB probe may have cut the JSON off mid-token.
            # Fall back to reading the full file only if it looks worth it.
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return None
        if isinstance(data, dict) and isinstance(data.get("operations"), list):
            return "xml_patch"
    return None


def derive_target_from_patch_path(patch_path: Path, mod_root: Path) -> Optional[str]:
    """Turn a patch file path into the target game-file path.

    Strategy:
      * Strip trailing ``.patch`` / ``.merge`` suffix.
      * Return the path RELATIVE to ``mod_root`` (forward slashes).
      * If the patch file isn't under ``mod_root`` or the extension chain
        doesn't yield a recognisable game file, returns None.
    """
    try:
        rel = Path(patch_path).relative_to(mod_root)
    except ValueError:
        return None
    name = rel.name
    low = name.lower()
    if low.endswith(".xml.patch") or low.endswith(".json.patch"):
        name = name[: -len(".patch")]
    elif low.endswith(".xml.merge") or low.endswith(".json.merge"):
        name = name[: -len(".merge")]
    # else: bare .json / .xml patch files keep their name as-is; caller
    # must provide an explicit target.
    target = rel.with_name(name)
    return target.as_posix()


# ── Load ─────────────────────────────────────────────────────────────

def load_patch_file(patch_path: Path) -> tuple[Optional[XmlPatchFile], Optional[str]]:
    """Load a patch file. Returns ``(patch, error)``. Exactly one is None.

    Accepts both JSON patch files and XML-root ``<xml-patch>`` files. The
    file type is decided by the first non-whitespace character.
    """
    try:
        text = Path(patch_path).read_text(encoding="utf-8")
    except Exception as e:
        return None, f"Error reading patch file: {e}"
    stripped = text.lstrip()
    if stripped.startswith("<"):
        return _load_xml_patch(stripped)
    return _load_json_patch(text)


def _load_json_patch(text: str) -> tuple[Optional[XmlPatchFile], Optional[str]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return None, f"JSON parse error: {e}"
    ops_raw = data.get("operations") if isinstance(data, dict) else None
    if not ops_raw:
        return None, "Empty or invalid patch file"
    patch = XmlPatchFile()
    for raw in ops_raw:
        if not isinstance(raw, dict):
            return None, "Operation not a dict"
        op = (raw.get("op") or "").strip().lower()
        xpath = (raw.get("xpath") or "").strip()
        if not op:
            return None, "Operation missing 'op' field"
        if not xpath:
            return None, "Operation missing 'xpath' field"
        if op not in _KNOWN_OPS:
            return None, f"Unknown operation '{op}'"
        attribute = raw.get("attribute")
        value = raw.get("value")
        if op in ("set-attr", "remove-attr") and not (attribute or "").strip():
            return None, f"'{op}' requires 'attribute' field"
        if op not in ("remove", "remove-attr") and value is None:
            return None, f"'{op}' requires 'value' field"
        patch.operations.append(XmlPatchOp(
            op=op, xpath=xpath, value=value,
            attribute=attribute, comment=raw.get("comment"),
        ))
    return patch, None


def _load_xml_patch(text: str) -> tuple[Optional[XmlPatchFile], Optional[str]]:
    try:
        root = etree.fromstring(text.encode("utf-8"))
    except etree.XMLSyntaxError as e:
        return None, f"XML parse error: {e}"
    if root.tag.lower() != "xml-patch":
        return None, "XML patch: root element must be <xml-patch>"
    patch = XmlPatchFile()
    for el in root:
        if isinstance(el, etree._Comment):
            continue
        tag = el.tag.lower()
        target = el.get("target")
        find = el.get("find")
        key_value = el.get("key")
        into = el.get("into")
        match_attrs = {
            k[len("match-"):]: v
            for k, v in el.attrib.items()
            if k.startswith("match-")
        }

        op_map = {
            "set":           ("set-attr",    None),
            "unset":         ("remove-attr", None),
            "insert":        ("add",         "inner"),
            "insert-before": ("add-before",  "inner"),
            "insert-after":  ("add-after",   "inner"),
            "replace":       ("replace",     "inner"),
            "delete":        ("remove",      None),
            "set-attr":      ("set-attr",    None),
            "remove-attr":   ("remove-attr", None),
            "add":           ("add",         "inner"),
            "add-before":    ("add-before",  "inner"),
            "add-after":     ("add-after",   "inner"),
            "remove":        ("remove",      None),
        }
        if tag not in op_map:
            return None, f"Unknown XML patch element: <{el.tag}>"
        canonical_op, value_source = op_map[tag]

        op = XmlPatchOp(op=canonical_op)
        if tag in ("set", "set-attr"):
            op.attribute = el.get("attr") or el.get("attribute")
            op.value = el.get("value")
        elif tag in ("unset", "remove-attr"):
            op.attribute = el.get("attr") or el.get("attribute")
        elif value_source == "inner":
            op.value = _get_inner_xml(el)
            if tag == "insert" and into is not None:
                target = "//" + into

        if target:
            op.xpath = target
        elif find:
            if match_attrs:
                # Fully specified — translate to a single XPath now.
                predicates = " and ".join(
                    f"@{k}='{_xpath_escape(v)}'" for k, v in match_attrs.items()
                )
                op.xpath = f"//{find}[{predicates}]"
            elif key_value is not None:
                # Identity-key lookup — defer resolution to apply time so
                # JMM's attr-priority (Key > Name > Id > key > name > id,
                # then any ``_key``/``_name``/``_id`` suffix attr on a
                # peer) fires against the actual target document.
                op.find_tag = find
                op.find_key = key_value
                op.xpath = f"//{find}"  # sentinel; _resolve_find_xpath overrides
            else:
                op.xpath = f"//{find}"
        elif not into:
            return None, f"<{el.tag}> requires 'target', 'find', or 'into' attribute"

        if not op.xpath:
            return None, f"<{el.tag}> requires 'target' or 'find' attribute"
        if op.op in ("set-attr", "remove-attr") and not (op.attribute or "").strip():
            return None, f"<{el.tag}> requires 'attr' attribute"
        if op.op not in ("remove", "remove-attr") and op.value is None:
            return None, f"<{el.tag}> requires content or 'value' attribute"
        patch.operations.append(op)

    if not patch.operations:
        return None, "XML patch: no operations found"
    return patch, None


# ── Helpers ──────────────────────────────────────────────────────────

def _get_inner_xml(el) -> str:
    """Serialise element's children + text (XmlReader.ReadInnerXml equivalent)."""
    parts: list[str] = []
    if el.text:
        parts.append(el.text)
    for child in el:
        parts.append(etree.tostring(child, encoding="unicode"))
        if child.tail:
            parts.append(child.tail)
    return "".join(parts).strip()


def _xpath_escape(value: str) -> str:
    """Mirror JMM's EscapeXPathString: only produce concat() when the string
    contains BOTH single and double quotes. Otherwise leave the caller's
    ``'...'`` wrapping intact and just return the raw value."""
    if "'" not in value or '"' not in value:
        return value
    parts = value.split("'")
    return "concat('" + "',\"'\",'".join(parts) + "')"


def _normalise_game_xml(text: str) -> tuple[str, bool]:
    """Rewrite ``</>`` shorthand closing tags to full ``</tagname>`` and
    wrap in a sentinel root when the document has multiple top-level
    elements. Returns ``(normalised_text, was_wrapped)``."""
    if "</>" in text:
        buf: list[str] = []
        stack: list[str] = []
        i = 0
        n = len(text)
        while i < n:
            if text[i] == "<":
                j = text.find(">", i)
                if j < 0:
                    buf.append(text[i:])
                    break
                tag = text[i:j + 1]
                if tag == "</>":
                    name = stack.pop() if stack else ""
                    buf.append(f"</{name}>")
                elif tag.startswith("</"):
                    if stack:
                        stack.pop()
                    buf.append(tag)
                elif tag.startswith("<?") or tag.startswith("<!"):
                    buf.append(tag)
                elif tag.endswith("/>"):
                    buf.append(tag)
                else:
                    k = i + 1
                    while k < j and text[k] not in " \t\r\n>":
                        k += 1
                    stack.append(text[i + 1:k])
                    buf.append(tag)
                i = j + 1
            else:
                buf.append(text[i])
                i += 1
        text = "".join(buf)

    wrapped = _has_multiple_roots(text)
    if wrapped:
        # Pull any leading XML declaration and DOCTYPE out before wrapping
        # so they stay at the document start after sentinel-wrapping.
        prologue = ""
        stripped = text.lstrip()
        lead_ws_len = len(text) - len(stripped)
        remaining = stripped
        while remaining.startswith("<?") or remaining.startswith("<!"):
            close = remaining.find("?>") if remaining.startswith("<?") else remaining.find(">")
            if close < 0:
                break
            end = close + (2 if remaining.startswith("<?") else 1)
            prologue += remaining[:end]
            remaining = remaining[end:].lstrip()
        text = text[:lead_ws_len] + prologue + (
            f"<{_SENTINEL_ROOT}>" + remaining + f"</{_SENTINEL_ROOT}>"
        )
    return text, wrapped


def _has_multiple_roots(text: str) -> bool:
    root_count = 0
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "<":
            i += 1
            continue
        j = text.find(">", i)
        if j < 0:
            break
        tag = text[i:j + 1]
        if tag.startswith("<?") or tag.startswith("<!--") or tag.startswith("<!"):
            i = j + 1
            continue
        if tag.startswith("</"):
            depth -= 1
            i = j + 1
            continue
        self_closing = tag.endswith("/>")
        if depth == 0:
            root_count += 1
            if root_count > 1:
                return True
        if not self_closing:
            depth += 1
        i = j + 1
    return False


def _decode_with_bom(data: bytes) -> tuple[str, str, bool]:
    """Decode bytes → (text, encoding_name, had_bom)."""
    # UTF-8 BOM
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8"), "utf-8", True
    # UTF-16 LE/BE
    if data.startswith(b"\xff\xfe"):
        return data[2:].decode("utf-16-le"), "utf-16-le", True
    if data.startswith(b"\xfe\xff"):
        return data[2:].decode("utf-16-be"), "utf-16-be", True
    return data.decode("utf-8", errors="replace"), "utf-8", False


def _encode_with_bom(text: str, encoding: str, add_bom: bool) -> bytes:
    raw = text.encode(encoding)
    if not add_bom:
        return raw
    bom = {
        "utf-8": b"\xef\xbb\xbf",
        "utf-16-le": b"\xff\xfe",
        "utf-16-be": b"\xfe\xff",
    }.get(encoding, b"")
    return bom + raw


def _strip_sentinel(serialised: str) -> str:
    open_tag = f"<{_SENTINEL_ROOT}>"
    close_tag = f"</{_SENTINEL_ROOT}>"
    start = serialised.find(open_tag)
    end = serialised.rfind(close_tag)
    if start < 0 or end <= start:
        return serialised
    return (
        serialised[:start]
        + serialised[start + len(open_tag):end]
        + serialised[end + len(close_tag):]
    )


# ── Apply ────────────────────────────────────────────────────────────

def _resolve_find_xpath(tree, find_tag: str, key_value: str) -> str:
    """Return the XPath JMM would have built for ``find="tag" key="..."``.

    Attempts each attribute in ``_IDENTITY_ATTR_PRIORITY`` in order; if
    none of those have any hits on the live document, scans the first
    matching element's attributes for a suffix-based identity attr
    (``*_key``, ``*_id``, ``*_name``). Falls back to ``@Key='…'`` — JMM's
    last-resort behaviour at ``XmlPatchApplier.cs:662``.
    """
    escaped = _xpath_escape(key_value)
    for attr in _IDENTITY_ATTR_PRIORITY:
        probe = f"//{find_tag}[@{attr}='{escaped}']"
        try:
            if tree.xpath(probe):
                return probe
        except etree.XPathEvalError:
            continue

    # Peer-attribute inspection: look at the first element of this tag and
    # see which suffix-based attr it carries.
    try:
        peer = next(iter(tree.xpath(f"//{find_tag}")), None)
    except etree.XPathEvalError:
        peer = None
    if peer is not None:
        for attr in _IDENTITY_ATTR_PRIORITY:
            if peer.get(attr) is not None:
                probe = f"//{find_tag}[@{attr}='{escaped}']"
                try:
                    if tree.xpath(probe):
                        return probe
                except etree.XPathEvalError:
                    continue
        for k in peer.attrib.keys():
            lk = k.lower()
            if lk.endswith("_key") or lk.endswith("_id") or lk.endswith("_name"):
                probe = f"//{find_tag}[@{k}='{escaped}']"
                try:
                    if tree.xpath(probe):
                        return probe
                except etree.XPathEvalError:
                    continue
    return f"//{find_tag}[@Key='{escaped}']"


def apply_patches(xml_data: bytes,
                  patches: list[tuple[str, XmlPatchFile]]
                  ) -> tuple[Optional[bytes], list[str]]:
    """Apply a list of patch files to ``xml_data``.

    ``patches`` is ``[(mod_name, XmlPatchFile), ...]``. Returns the new
    bytes (or None on parse failure) and a human-readable log list.
    """
    log: list[str] = []
    try:
        text, encoding, had_bom = _decode_with_bom(xml_data)
        normalised, wrapped = _normalise_game_xml(text)
        # Preserve whitespace so re-serialising doesn't reformat the file.
        tree = etree.ElementTree(etree.fromstring(
            normalised.encode("utf-8"),
            parser=etree.XMLParser(remove_blank_text=False),
        ))
    except Exception as e:
        log.append(f"    [XML] ERROR: Failed to parse XML: {e}")
        return None, log

    applied_total = 0
    failed_total = 0
    for mod_name, patch in patches:
        log.append(f"    [XML] Applying {len(patch.operations)} operation(s) from {mod_name}")
        for i, op in enumerate(patch.operations, 1):
            # Resolve `find`/`key=` lookups at apply time so JMM's identity-
            # attribute priority (Key, Name, Id, key, name, id, then any
            # ``_key`` / ``_name`` / ``_id`` suffix attr on a peer) can
            # actually inspect the live target doc.
            if op.find_tag and op.find_key is not None:
                xpath = _resolve_find_xpath(tree, op.find_tag, op.find_key)
            else:
                xpath = op.xpath
            label = f"#{i} {op.op} xpath=\"{xpath}\""
            if op.comment:
                label += f" ({op.comment})"
            try:
                targets = tree.xpath(xpath)
            except etree.XPathEvalError as xe:
                log.append(f"    [XML]   {label} — ERROR: Invalid XPath: {xe}")
                failed_total += 1
                continue
            except Exception as ex:
                log.append(f"    [XML]   {label} — ERROR: {ex}")
                failed_total += 1
                continue

            if not targets:
                log.append(f"    [XML]   {label} — WARNING: no elements matched")
                failed_total += 1
                continue

            try:
                _run_op(op, targets)
            except Exception as ex:
                log.append(f"    [XML]   {label} — ERROR: {ex}")
                failed_total += 1
                continue
            log.append(f"    [XML]   {label} — OK ({len(targets)} element(s))")
            applied_total += 1
    log.append(f"    [XML] Result: {applied_total} applied, {failed_total} failed")

    try:
        serialised = etree.tostring(tree, encoding="unicode")
        if wrapped:
            serialised = _strip_sentinel(serialised)
        return _encode_with_bom(serialised, encoding, had_bom), log
    except Exception as e:
        log.append(f"    [XML] ERROR: Failed to serialize XML: {e}")
        return None, log


def _run_op(op: XmlPatchOp, targets: list) -> None:
    op_name = op.op
    for el in targets:
        if not isinstance(el, etree._Element):
            # Attribute/text results from XPath are not supported for ops.
            continue
        if op_name == "replace":
            _replace_element(el, op.value)
        elif op_name == "add":
            _add_into(el, op.value)
        elif op_name == "add-before":
            _add_sibling(el, op.value, before=True)
        elif op_name == "add-after":
            _add_sibling(el, op.value, before=False)
        elif op_name == "remove":
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)
        elif op_name == "set-attr":
            el.set(op.attribute, "" if op.value is None else op.value)
        elif op_name == "remove-attr":
            if op.attribute in el.attrib:
                del el.attrib[op.attribute]


def _replace_element(el, value: Optional[str]) -> None:
    if value is None:
        return
    stripped = value.lstrip()
    parsed = _parse_fragment(value) if stripped.startswith("<") else None
    if parsed is not None:
        parent = el.getparent()
        if parent is None:
            # Replacing root — copy tag + children into place.
            el.tag = parsed.tag
            el.attrib.clear()
            for k, v in parsed.attrib.items():
                el.set(k, v)
            for child in list(el):
                el.remove(child)
            el.text = parsed.text
            for child in parsed:
                el.append(child)
            return
        idx = parent.index(el)
        parent.remove(el)
        parent.insert(idx, parsed)
    else:
        for child in list(el):
            el.remove(child)
        el.text = value


def _add_into(el, value: Optional[str]) -> None:
    if value is None:
        return
    parsed = _parse_fragment(value)
    if parsed is not None:
        el.append(parsed)
    else:
        # Plain text — append to last existing child's tail or element's text.
        existing = list(el)
        if existing:
            existing[-1].tail = (existing[-1].tail or "") + value
        else:
            el.text = (el.text or "") + value


def _add_sibling(el, value: Optional[str], *, before: bool) -> None:
    if value is None:
        return
    parent = el.getparent()
    if parent is None:
        return
    idx = parent.index(el)
    parsed = _parse_fragment(value)
    if parsed is not None:
        parent.insert(idx if before else idx + 1, parsed)
    else:
        # Plain text as sibling is unusual; attach as tail text.
        if before:
            prev_tail = el.getprevious()
            if prev_tail is not None:
                prev_tail.tail = (prev_tail.tail or "") + value
            else:
                parent.text = (parent.text or "") + value
        else:
            el.tail = (el.tail or "") + value


def _parse_fragment(value: str):
    stripped = value.lstrip()
    if not stripped.startswith("<"):
        return None
    try:
        return etree.fromstring(value)
    except etree.XMLSyntaxError:
        # Wrap in sentinel and return first child if it parses.
        try:
            wrapped = f"<{_SENTINEL_ROOT}>{value}</{_SENTINEL_ROOT}>"
            root = etree.fromstring(wrapped)
            children = list(root)
            return children[0] if children else None
        except etree.XMLSyntaxError:
            return None


# ── Merge ────────────────────────────────────────────────────────────

def apply_merge(xml_data: bytes,
                merge_files: list[tuple[str, Path]]
                ) -> tuple[Optional[bytes], list[str]]:
    """Apply identity-key merge. Mirrors JMM ``ApplyMerge``."""
    log: list[str] = []
    try:
        text, encoding, had_bom = _decode_with_bom(xml_data)
        normalised, wrapped = _normalise_game_xml(text)
        tree = etree.ElementTree(etree.fromstring(
            normalised.encode("utf-8"),
            parser=etree.XMLParser(remove_blank_text=False),
        ))
    except Exception as e:
        log.append(f"    [MERGE] ERROR: Failed to parse target XML: {e}")
        return None, log

    merged_total = added_total = deleted_total = failed = 0
    for mod_name, merge_path in merge_files:
        try:
            raw = Path(merge_path).read_text(encoding="utf-8")
        except Exception as e:
            log.append(f"    [MERGE] ERROR in {mod_name}: {e}")
            failed += 1
            continue
        stripped = raw.lstrip()
        if not stripped:
            log.append(f"    [MERGE] WARNING in {mod_name}: empty merge file — skipped")
            continue
        used_sentinel = False
        try:
            merge_root = etree.fromstring(raw.encode("utf-8"))
        except etree.XMLSyntaxError:
            try:
                merge_root = etree.fromstring(
                    f"<{_SENTINEL_ROOT}>{raw}</{_SENTINEL_ROOT}>".encode("utf-8"))
                used_sentinel = True
            except etree.XMLSyntaxError as e:
                log.append(f"    [MERGE] ERROR in {mod_name}: {e}")
                failed += 1
                continue

        log.append(f"    [MERGE] Applying merge from {mod_name}")
        elements_to_process = (
            list(merge_root)
            if (merge_root.tag == _SENTINEL_ROOT
                or merge_root.tag.lower() == "xml-merge"
                or used_sentinel)
            else [merge_root]
        )
        for entry in elements_to_process:
            if len(list(entry)) > 0:
                section_name = entry.tag
                section = None
                for candidate in tree.getroot().iter(section_name):
                    section = candidate
                    break
                if section is None:
                    log.append(f"    [MERGE]   Section <{section_name}> not found — skipping")
                    failed += 1
                    continue
                for child in entry:
                    m, a, d = _merge_element(child, section, tree, log)
                    merged_total += m
                    added_total += a
                    deleted_total += d
            else:
                m, a, d = _merge_element(entry, None, tree, log)
                merged_total += m
                added_total += a
                deleted_total += d

    log.append(f"    [MERGE] Result: {merged_total} merged, {added_total} added, "
               f"{deleted_total} deleted, {failed} failed")

    try:
        serialised = etree.tostring(tree, encoding="unicode")
        if wrapped:
            serialised = _strip_sentinel(serialised)
        return _encode_with_bom(serialised, encoding, had_bom), log
    except Exception as e:
        log.append(f"    [MERGE] ERROR: Failed to serialize XML: {e}")
        return None, log


def _merge_element(merge_el, parent_hint, tree, log) -> tuple[int, int, int]:
    """Return (merged, added, deleted)."""
    tag = merge_el.tag
    key_attr, key_value = _find_identity_attribute(merge_el, parent_hint, tree, tag)
    delete_attr = merge_el.get("__delete")
    is_delete = (delete_attr is not None
                 and delete_attr.lower() == "true")

    if not key_attr or key_value is None:
        if is_delete:
            log.append(f"    [MERGE]   - <{tag}> cannot delete without identity key")
            return 0, 0, 0
        target_parent = parent_hint if parent_hint is not None else tree.getroot()
        if target_parent is not None:
            target_parent.append(etree.fromstring(etree.tostring(merge_el)))
            log.append(f"    [MERGE]   + <{tag}> added (no identity key)")
            return 0, 1, 0
        return 0, 0, 0

    # Find existing element matching (tag, key_attr, key_value).
    matches = (parent_hint.iter(tag) if parent_hint is not None
               else tree.getroot().iter(tag))
    existing = next(
        (el for el in matches if el.get(key_attr) == key_value), None)

    if is_delete:
        if existing is not None:
            parent = existing.getparent()
            if parent is not None:
                parent.remove(existing)
                log.append(f"    [MERGE]   - <{tag} {key_attr}=\"{key_value}\"> deleted")
                return 0, 0, 1
        log.append(f"    [MERGE]   - <{tag} {key_attr}=\"{key_value}\"> not found for delete")
        return 0, 0, 0

    if existing is not None:
        updated = 0
        for k, v in merge_el.attrib.items():
            if k == key_attr or k.startswith("__"):
                continue
            existing.set(k, v)
            updated += 1
        log.append(f"    [MERGE]   ~ <{tag} {key_attr}=\"{key_value}\"> merged "
                   f"({updated} attr(s))")
        return 1, 0, 0

    # Not found — add new element under the best parent we can find.
    clone_bytes = etree.tostring(merge_el)
    clone = etree.fromstring(clone_bytes)
    if "__delete" in clone.attrib:
        del clone.attrib["__delete"]
    if parent_hint is not None:
        parent_hint.append(clone)
    else:
        anchor = next(iter(tree.getroot().iter(tag)), None)
        anchor_parent = anchor.getparent() if anchor is not None else tree.getroot()
        anchor_parent.append(clone)
    log.append(f"    [MERGE]   + <{tag} {key_attr}=\"{key_value}\"> added (new)")
    return 0, 1, 0


def process_xml_patches_for_overlay(
    patch_items: list[dict],
    game_dir: Path,
) -> list[tuple[bytes, dict]]:
    """Apply all XML patches / merges against the vanilla XMLs they target
    and emit ``(content_bytes, metadata)`` tuples ready for the overlay
    builder.

    ``patch_items`` is a list of dicts with keys::

        {
            "mod_id":     int,
            "mod_name":   str,
            "kind":       "xml_patch" | "xml_merge",
            "delta_path": str,   # path on disk to the patch / merge file
            "file_path":  str,   # "NNNN/relative/path.xml" — target game file
            "priority":   int,   # higher = later (JMM convention)
        }

    Patches for the same target are applied in priority order (ascending),
    so higher-priority mods' operations execute LAST and win conflicts.
    Merges run AFTER all patches for a given target.
    """
    # Lazy imports to keep this module importable without the full engine.
    from cdumm.engine.json_patch_handler import (
        _find_pamt_entry, _extract_from_paz,
    )

    vanilla_dir = game_dir / "CDMods" / "vanilla"
    if not vanilla_dir.exists():
        vanilla_dir = game_dir

    # Group by target path (normalised).
    by_target: dict[str, list[dict]] = {}
    for item in patch_items:
        fp = (item.get("file_path") or "").strip()
        if not fp:
            continue
        by_target.setdefault(fp, []).append(item)

    overlay_entries: list[tuple[bytes, dict]] = []
    for target_path, items in by_target.items():
        items.sort(key=lambda d: (int(d.get("priority") or 0), int(d.get("mod_id") or 0)))

        # Resolve via PAMT in the target's PAZ dir. `file_path` ships as
        # "NNNN/relative.xml"; feed the 'relative.xml' basename chain so
        # _find_pamt_entry can match the PAMT path (which usually omits the
        # dir-number prefix).
        target_basename = target_path.split("/", 1)[-1] if "/" in target_path else target_path
        entry = (_find_pamt_entry(target_basename, vanilla_dir)
                 or _find_pamt_entry(target_basename, game_dir))
        if entry is None:
            logger.warning("xml_patch: target not found in PAMT — %s", target_path)
            continue

        try:
            vanilla_bytes = _extract_from_paz(entry)
        except Exception as e:
            logger.error("xml_patch: extract failed for %s: %s", target_path, e)
            continue

        # Collect patches + merges.
        patches: list[tuple[str, XmlPatchFile]] = []
        merges: list[tuple[str, Path]] = []
        for it in items:
            kind = it.get("kind")
            dp = Path(it.get("delta_path") or "")
            if not dp.exists():
                logger.warning("xml_patch: patch file missing: %s", dp)
                continue
            if kind == "xml_patch":
                p, err = load_patch_file(dp)
                if err or p is None:
                    logger.warning("xml_patch: load failed (%s): %s",
                                   dp.name, err)
                    continue
                patches.append((it.get("mod_name") or dp.stem, p))
            elif kind == "xml_merge":
                merges.append((it.get("mod_name") or dp.stem, dp))

        current = bytes(vanilla_bytes)
        if patches:
            out, log = apply_patches(current, patches)
            for line in log:
                logger.info(line)
            if out is not None:
                current = out
        if merges:
            out, log = apply_merge(current, merges)
            for line in log:
                logger.info(line)
            if out is not None:
                current = out

        if current == vanilla_bytes:
            logger.debug("xml_patch: no net change for %s, skipping", target_path)
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
        overlay_entries.append((bytes(current), meta))
        logger.info("xml_patch: produced overlay entry for %s (%d bytes) "
                    "from %d patch/merge file(s)",
                    target_path, len(current), len(items))

    return overlay_entries


def _find_identity_attribute(merge_el, parent_hint, tree, tag_name
                             ) -> tuple[Optional[str], Optional[str]]:
    for name in _IDENTITY_ATTR_PRIORITY:
        v = merge_el.get(name)
        if v is not None:
            return name, v
    for k, v in merge_el.attrib.items():
        if k.startswith("__"):
            continue
        lk = k.lower()
        if lk.endswith("_key") or lk.endswith("_id") or lk.endswith("_name"):
            return k, v
    # Fall back to inspecting an existing peer for a likely identity attr.
    peers = (parent_hint.iter(tag_name) if parent_hint is not None
             else tree.getroot().iter(tag_name))
    peer = next(peers, None)
    if peer is not None:
        for name in _IDENTITY_ATTR_PRIORITY:
            if peer.get(name) is not None:
                v = merge_el.get(name)
                if v is not None:
                    return name, v
        for k in peer.attrib.keys():
            lk = k.lower()
            if lk.endswith("_key") or lk.endswith("_id") or lk.endswith("_name"):
                v = merge_el.get(k)
                if v is not None:
                    return k, v
    return None, None
