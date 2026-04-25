"""Partial HTML patch + merge support — JMM v9.9.3 parity.

Ports JMM's ``HtmlPatchApplier.cs`` to Python so mods authored for
JMM's ``*.html.patch`` and ``*.html.merge`` formats import and apply
identically in CDUMM.

Two parsing modes coexist in the same patch file (the parsers run in
sequence and concatenate their ops):

1. **HTML-tag operations** (terse, intuitive)::

       <set at="#btn" class="+active -disabled" data-state="open" />
       <remove at=".dead" />
       <replace at="#header"><h1>New</h1></replace>
       <inner at="#title">replacement inner HTML</inner>
       <replace-inner at="#title">same as above</replace-inner>
       <append at="#list"><li>last</li></append>
       <prepend at="#list"><li>first</li></prepend>
       <before at="#sib">prev sibling</before>
       <after at="#sib">next sibling</after>
       <insert-before at="#sib">prev sibling</insert-before>
       <insert-after at="#sib">next sibling</insert-after>

   The ``<set>`` tag's ``class`` attribute supports ``+token`` to add
   and ``-token`` to remove individual classes. Other attrs on
   ``<set>`` set the named attribute on the matched element.

2. **HTML-comment directives** (block-payload form)::

       <!-- @replace selector="#header" -->
         <h1>New</h1>
       <!-- @end -->

       <!-- @set-attr selector="img.banner" name="src" value="new.png" -->
       <!-- @add-class selector=".btn" value="primary" -->

Selectors use CSS-style syntax: ``tag``, ``#id``, ``.class``,
``[attr=value]``, descendant chains separated by spaces.
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
class HtmlPatchOp:
    op: str = ""
    selector: str = ""
    name: Optional[str] = None
    value: Optional[str] = None
    payload: Optional[str] = None
    source_mod_name: str = ""


@dataclass
class _HtmlElement:
    tag_name: str = ""
    attrs: dict[str, str] = field(default_factory=dict)
    open_start: int = 0
    open_end: int = 0
    close_start: int = -1
    close_end: int = -1
    self_closing: bool = False

    @property
    def inner_start(self) -> int:
        return self.open_end + 1

    @property
    def inner_end(self) -> int:
        return self.close_start

    @property
    def whole_start(self) -> int:
        return self.open_start

    @property
    def whole_end(self) -> int:
        if self.self_closing:
            return self.open_end + 1
        return self.close_end + 1


@dataclass
class _SelectorSeg:
    tag: Optional[str] = None
    id: Optional[str] = None
    classes: list[str] = field(default_factory=list)
    attrs: list[tuple[str, str]] = field(default_factory=list)


_OP_TAGS = frozenset({
    "set", "remove", "replace", "replace-inner", "inner",
    "append", "prepend", "before", "after",
    "insert-before", "insert-after",
})

# HTML5 void elements + ``include`` (used by the game's UI templating).
_VOID_ELEMENTS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "keygen", "link", "meta", "param", "source", "track", "wbr",
    "include",
})

_DIRECTIVE_PAYLOAD_OPS = frozenset({
    "replace", "replace-inner", "append", "prepend",
    "insert-before", "insert-after",
})


# ── Detection ────────────────────────────────────────────────────────


def detect_patch_file(path: Path) -> Optional[str]:
    name = path.name.lower()
    if name.endswith(".html.patch"):
        return "html_patch"
    if name.endswith(".html.merge"):
        return "html_merge"
    return None


def derive_target_from_patch_path(patch_path: Path,
                                  mod_root: Path) -> Optional[str]:
    try:
        rel = Path(patch_path).relative_to(mod_root)
    except ValueError:
        return None
    name = rel.name
    low = name.lower()
    if low.endswith(".html.patch"):
        name = name[: -len(".patch")]
    elif low.endswith(".html.merge"):
        name = name[: -len(".merge")]
    target = rel.with_name(name)
    return target.as_posix()


# ── Public apply API ─────────────────────────────────────────────────


def apply_patches(original_bytes: bytes,
                  patch_files: list[tuple[str, Path]]
                  ) -> tuple[Optional[bytes], list[str]]:
    """Apply ``*.html.patch`` files to vanilla HTML bytes.

    Returns ``(new_bytes, log)``. ``(None, log)`` on UTF-8 decode
    failure of the original.
    """
    log: list[str] = []
    try:
        text = _decode_utf8(original_bytes)
    except Exception as e:
        log.append(f"    [HTML] ERROR: Cannot decode original as UTF-8: {e}")
        return None, log

    ops: list[HtmlPatchOp] = []
    for mod_name, path in patch_files:
        try:
            content = Path(path).read_text(encoding="utf-8")
            tag_ops = parse_simple_syntax(content, mod_name, log)
            dir_ops = parse_patch_file(content, mod_name, log)
            combined = tag_ops + dir_ops
            ops.extend(combined)
            log.append(
                f"    [HTML] Loaded {len(combined)} op(s) from "
                f"{Path(path).name} ({mod_name})")
        except Exception as e:
            log.append(f"    [HTML] ERROR loading {path}: {e}")

    if not ops:
        log.append("    [HTML] No ops to apply.")
        return original_bytes, log

    applied = skipped = 0
    for op in ops:
        label = f"[{op.source_mod_name}] {op.op} '{op.selector}'"
        try:
            new_text = _apply_one(text, op, log, label)
            if new_text is None or new_text == text:
                skipped += 1
                continue
            text = new_text
            applied += 1
        except Exception as e:
            log.append(f"    [HTML] ERROR {label}: {e}")
            skipped += 1

    log.append(f"    [HTML] {applied} applied, {skipped} skipped")
    return text.encode("utf-8"), log


# ── Operation dispatcher ─────────────────────────────────────────────


def _apply_one(text: str, op: HtmlPatchOp,
               log: list[str], label: str) -> Optional[str]:
    matches = _find_all(text, op.selector)
    if not matches and op.op != "append":
        log.append(f"    [HTML] SKIP {label}: selector matched nothing")
        return text
    out = text
    # Apply in reverse document order so earlier offsets stay valid.
    for el in sorted(matches, key=lambda e: e.open_start, reverse=True):
        if op.op == "set-attr":
            out = _op_set_attr(out, el, op.name or "", op.value or "")
        elif op.op == "remove-attr":
            out = _op_remove_attr(out, el, op.name or "")
        elif op.op == "add-class":
            out = _op_add_class(out, el, op.value or "")
        elif op.op == "remove-class":
            out = _op_remove_class(out, el, op.value or "")
        elif op.op == "remove":
            out = _op_remove(out, el)
        elif op.op == "replace":
            out = _op_replace(out, el, op.payload or "")
        elif op.op == "replace-inner":
            out = _op_replace_inner(out, el, op.payload or "")
        elif op.op == "append":
            out = _op_append(out, el, op.payload or "")
        elif op.op == "prepend":
            out = _op_prepend(out, el, op.payload or "")
        elif op.op == "insert-before":
            out = _op_insert_before(out, el, op.payload or "")
        elif op.op == "insert-after":
            out = _op_insert_after(out, el, op.payload or "")
        else:
            log.append(f"    [HTML] SKIP {label}: unknown op")
            return text
        # Re-scan after each edit since offsets shifted.
        if op.op != "set-attr" and op.op != "remove-attr" and \
                op.op != "add-class" and op.op != "remove-class":
            # full structural edits invalidate later matches in `matches`
            # — re-find under the same selector against the new text
            matches_new = _find_all(out, op.selector)
            # Use the first remaining unedited match in document order;
            # since we iterate in reverse this break is fine.
            del matches_new
            break
    return out


# ── Per-op implementations ───────────────────────────────────────────


def _op_set_attr(text: str, el: _HtmlElement, name: str, value: str) -> str:
    open_tag = text[el.open_start:el.open_end + 1]
    new_open = _set_attr_in_open_tag(open_tag, name, value)
    return text[:el.open_start] + new_open + text[el.open_end + 1:]


def _op_remove_attr(text: str, el: _HtmlElement, name: str) -> str:
    open_tag = text[el.open_start:el.open_end + 1]
    new_open = _remove_attr_in_open_tag(open_tag, name)
    return text[:el.open_start] + new_open + text[el.open_end + 1:]


def _op_add_class(text: str, el: _HtmlElement, cls: str) -> str:
    if not cls or cls.isspace():
        return text
    existing = el.attrs.get("class", "")
    tokens = existing.split()
    if cls in tokens:
        return text
    tokens.append(cls)
    return _op_set_attr(text, el, "class", " ".join(tokens))


def _op_remove_class(text: str, el: _HtmlElement, cls: str) -> str:
    if not cls or cls.isspace():
        return text
    existing = el.attrs.get("class")
    if existing is None:
        return text
    tokens = [t for t in existing.split() if t != cls]
    return _op_set_attr(text, el, "class", " ".join(tokens))


def _op_remove(text: str, el: _HtmlElement) -> str:
    return text[:el.whole_start] + text[el.whole_end:]


def _op_replace(text: str, el: _HtmlElement, payload: str) -> str:
    return text[:el.whole_start] + payload.strip() + text[el.whole_end:]


def _op_replace_inner(text: str, el: _HtmlElement, payload: str) -> str:
    if el.self_closing:
        return text
    return text[:el.inner_start] + payload + text[el.inner_end:]


def _op_append(text: str, el: _HtmlElement, payload: str) -> str:
    if el.self_closing:
        return text
    indent = _detect_child_indent(text, el)
    insert = _ensure_leading_newline(payload, indent)
    return text[:el.inner_end] + insert + text[el.inner_end:]


def _op_prepend(text: str, el: _HtmlElement, payload: str) -> str:
    if el.self_closing:
        return text
    indent = _detect_child_indent(text, el)
    insert = _ensure_leading_newline(payload, indent)
    return text[:el.inner_start] + insert + text[el.inner_start:]


def _op_insert_before(text: str, el: _HtmlElement, payload: str) -> str:
    indent = _detect_sibling_indent(text, el)
    return (text[:el.whole_start] + payload.rstrip() + "\n"
            + indent + text[el.whole_start:])


def _op_insert_after(text: str, el: _HtmlElement, payload: str) -> str:
    indent = _detect_sibling_indent(text, el)
    return (text[:el.whole_end] + "\n" + indent + payload.lstrip()
            + text[el.whole_end:])


# ── Open-tag attr edits ──────────────────────────────────────────────


_OPEN_TAG_ATTR_RE_TEMPLATE = (
    r"(\s)({name})\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s/>]+)")


def _set_attr_in_open_tag(open_tag: str, name: str, value: str) -> str:
    pattern = _OPEN_TAG_ATTR_RE_TEMPLATE.format(name=re.escape(name))
    rx = re.compile(pattern, re.IGNORECASE)
    if rx.search(open_tag):
        return rx.sub(
            lambda m: f'{m.group(1)}{m.group(2)}="{_escape_attr(value)}"',
            open_tag, count=1)
    # No existing attr — insert before the closing '>' or '/>'
    end = len(open_tag) - 1
    if open_tag.endswith("/>"):
        end = len(open_tag) - 2
    while end > 0 and open_tag[end - 1].isspace():
        end -= 1
    return (open_tag[:end] + f' {name}="{_escape_attr(value)}"'
            + open_tag[end:])


def _remove_attr_in_open_tag(open_tag: str, name: str) -> str:
    pattern = (r"\s+" + re.escape(name)
               + r"\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s/>]+)")
    return re.sub(pattern, "", open_tag, count=1, flags=re.IGNORECASE)


def _escape_attr(value: str) -> str:
    return value.replace('"', "&quot;")


# ── Indentation detection ────────────────────────────────────────────


def _detect_child_indent(text: str, el: _HtmlElement) -> str:
    i = el.inner_start
    while i < el.inner_end and text[i] in ("\r", "\n"):
        i += 1
    indent_start = i
    while i < el.inner_end and text[i] in (" ", "\t"):
        i += 1
    if i > indent_start:
        return text[indent_start:i]
    return "\t"


def _detect_sibling_indent(text: str, el: _HtmlElement) -> str:
    i = el.open_start - 1
    while i > 0 and text[i] != "\n":
        i -= 1
    if i < 0:
        return ""
    line_start = i + 1
    j = line_start
    while j < el.open_start and text[j] in (" ", "\t"):
        j += 1
    return text[line_start:j]


def _ensure_leading_newline(payload: str, indent: str) -> str:
    return "\n" + indent + payload.strip() + "\n"


# ── Simple-syntax (HTML-tag op) parser ──────────────────────────────


def parse_simple_syntax(content: str, mod_name: str,
                        log: list[str]) -> list[HtmlPatchOp]:
    """Parse the HTML-tag operation form.

    Tag names that match an op keyword (set / remove / replace / inner
    / append / prepend / before / after / insert-before /
    insert-after / replace-inner) are treated as op tags. The ``at``
    attribute carries the selector. Block-style ops use the inner HTML
    as the payload."""
    ops: list[HtmlPatchOp] = []
    elements = _scan_elements(content)
    consumed_until = 0
    for el in sorted(elements, key=lambda e: e.open_start):
        if el.open_start < consumed_until:
            continue
        tag = el.tag_name.lower()
        if tag == "patch" or tag not in _OP_TAGS:
            continue
        sel = el.attrs.get("at")
        if not sel or sel.isspace():
            log.append(
                f"    [HTML] WARN ({mod_name}): <{tag}> missing "
                "at=\"selector\" — skipped")
            continue
        if tag == "remove":
            ops.append(HtmlPatchOp(
                op="remove", selector=sel,
                source_mod_name=mod_name))
            continue
        if tag == "set":
            for attr_name, attr_value in el.attrs.items():
                if attr_name.lower() == "at":
                    continue
                if attr_name.lower() == "class" and _has_class_prefix(attr_value):
                    for token in attr_value.split():
                        if token.startswith("+") and len(token) > 1:
                            ops.append(HtmlPatchOp(
                                op="add-class", selector=sel,
                                value=token[1:],
                                source_mod_name=mod_name))
                        elif token.startswith("-") and len(token) > 1:
                            ops.append(HtmlPatchOp(
                                op="remove-class", selector=sel,
                                value=token[1:],
                                source_mod_name=mod_name))
                else:
                    ops.append(HtmlPatchOp(
                        op="set-attr", selector=sel,
                        name=attr_name, value=attr_value,
                        source_mod_name=mod_name))
            continue
        if el.self_closing:
            log.append(
                f"    [HTML] WARN ({mod_name}): <{tag} at=\"{sel}\"/> "
                "is self-closing — needs a payload")
            continue
        payload = content[el.inner_start:el.inner_end]
        # Aliases: inner -> replace-inner; before -> insert-before;
        # after -> insert-after.
        canon = {
            "inner": "replace-inner",
            "before": "insert-before",
            "after": "insert-after",
        }.get(tag, tag)
        ops.append(HtmlPatchOp(
            op=canon, selector=sel, payload=payload,
            source_mod_name=mod_name))
        consumed_until = el.whole_end
    return ops


def _has_class_prefix(value: str) -> bool:
    for token in value.split():
        if token.startswith("+") or token.startswith("-"):
            return True
    return False


# ── Comment-directive parser ────────────────────────────────────────


def parse_patch_file(content: str, mod_name: str,
                     log: list[str]) -> list[HtmlPatchOp]:
    """Parse the HTML-comment directive form.

    Recognises directives inside ``<!-- @op ... -->`` blocks. For ops
    that take a body (``replace``, ``append``, etc.), the body extends
    until the matching ``<!-- @end -->`` marker. Single-line ops
    (``set-attr``, ``remove-attr``, ``add-class``, ``remove-class``,
    ``remove``) carry their parameters as attribute-style key=value
    pairs in the directive itself.
    """
    ops: list[HtmlPatchOp] = []
    n = len(content)
    i = 0
    while i < n:
        start = content.find("<!--", i)
        if start < 0:
            break
        end = content.find("-->", start + 4)
        if end < 0:
            break
        body = content[start + 4:end].strip()
        i = end + 3
        if (not body.startswith("@")
                or body.lower().startswith("@cd-html-")
                or body.lower().startswith("@end")):
            continue
        op_name, attrs = _parse_directive(body)
        if not op_name:
            continue
        payload: Optional[str] = None
        if op_name in _DIRECTIVE_PAYLOAD_OPS:
            end_pos = _find_end_directive(content, i)
            if end_pos < 0:
                log.append(
                    f"    [HTML] WARN ({mod_name}): @{op_name} "
                    "missing matching <!-- @end --> — skipped")
                continue
            payload = content[i:end_pos]
            close = content.find("-->", end_pos)
            if close >= 0:
                i = close + 3
        op = HtmlPatchOp(
            op=op_name,
            selector=attrs.get("selector", ""),
            name=attrs.get("name"),
            value=attrs.get("value"),
            payload=payload,
            source_mod_name=mod_name)
        if not op.selector or op.selector.isspace():
            log.append(
                f"    [HTML] WARN ({mod_name}): @{op_name} "
                "missing selector=\"...\" — skipped")
            continue
        ops.append(op)
    return ops


def _find_end_directive(content: str, frm: int) -> int:
    n = len(content)
    i = frm
    while i < n:
        start = content.find("<!--", i)
        if start < 0:
            return -1
        end = content.find("-->", start + 4)
        if end < 0:
            return -1
        if content[start + 4:end].strip().lower() == "@end":
            return start
        i = end + 3
    return -1


_DIRECTIVE_HEAD_RE = re.compile(r"^@([\w-]+)\s*(.*)$", re.DOTALL)
_DIRECTIVE_ATTR_RE = re.compile(
    r"([\w-]+)\s*=\s*(\"([^\"]*)\"|'([^']*)')")


def _parse_directive(body: str) -> tuple[str, dict[str, str]]:
    m = _DIRECTIVE_HEAD_RE.match(body)
    if not m:
        return "", {}
    op = m.group(1).lower()
    rest = m.group(2)
    attrs: dict[str, str] = {}
    for am in _DIRECTIVE_ATTR_RE.finditer(rest):
        attrs[am.group(1)] = am.group(3) if am.group(3) is not None else am.group(4)
    return op, attrs


# ── Selector matching ───────────────────────────────────────────────


def _find_all(text: str, selector: str) -> list[_HtmlElement]:
    chain = _parse_selector(selector)
    all_els = _scan_elements(text)
    return [el for el in all_els if _matches_chain(el, all_els, chain)]


def _matches_chain(el: _HtmlElement, all_els: list[_HtmlElement],
                   chain: list[_SelectorSeg]) -> bool:
    if not chain:
        return False
    if not _matches(el, chain[-1]):
        return False
    cur = el
    for k in range(len(chain) - 2, -1, -1):
        seg = chain[k]
        found = False
        for cand in all_els:
            if (cand.open_start < cur.open_start
                    and cand.whole_end > cur.whole_end
                    and _matches(cand, seg)):
                cur = cand
                found = True
                break
        if not found:
            return False
    return True


def _parse_selector(sel: str) -> list[_SelectorSeg]:
    out: list[_SelectorSeg] = []
    for chunk in sel.split():
        seg = _SelectorSeg()
        j = 0
        n = len(chunk)
        if j < n and (chunk[j].isalpha() or chunk[j] == "_"):
            start = j
            while j < n and (chunk[j].isalnum() or chunk[j] in "-_"):
                j += 1
            seg.tag = chunk[start:j]
        while j < n:
            c = chunk[j]
            if c == "#":
                j += 1
                start = j
                while j < n and (chunk[j].isalnum() or chunk[j] in "-_"):
                    j += 1
                seg.id = chunk[start:j]
                continue
            if c == ".":
                j += 1
                start = j
                while j < n and (chunk[j].isalnum() or chunk[j] in "-_"):
                    j += 1
                seg.classes.append(chunk[start:j])
                continue
            if c == "[":
                close = chunk.find("]", j)
                if close < 0:
                    break
                inner = chunk[j + 1:close]
                m = re.match(
                    r"^([\w-]+)\s*=\s*[\"']?([^\"']*)[\"']?$", inner)
                if m:
                    seg.attrs.append((m.group(1), m.group(2)))
                j = close + 1
                continue
            j += 1
        out.append(seg)
    return out


def _matches(el: _HtmlElement, seg: _SelectorSeg) -> bool:
    if seg.tag is not None and el.tag_name.lower() != seg.tag.lower():
        return False
    if seg.id is not None:
        v = el.attrs.get("id")
        if v != seg.id:
            return False
    for cls in seg.classes:
        v = el.attrs.get("class")
        if v is None:
            return False
        if cls not in v.split():
            return False
    for attr_name, attr_value in seg.attrs:
        v = el.attrs.get(attr_name)
        if v != attr_value:
            return False
    return True


# ── HTML element scanner ────────────────────────────────────────────


def _scan_elements(text: str) -> list[_HtmlElement]:
    """Tokenise the HTML into open/close tags and pair them up.

    Mirrors JMM's scanner: a stack tracks open tags; encountering
    ``</tag>`` pops the stack until it finds a matching open and
    marks it closed (any open tags it skipped past become
    self-closing — JMM's heuristic for malformed HTML)."""
    n = len(text)
    elements: list[_HtmlElement] = []
    stack: list[int] = []
    i = 0
    while i < n:
        if text[i] != "<":
            i += 1
            continue
        if i + 3 < n and text[i + 1:i + 4] == "!--":
            close = text.find("-->", i + 4)
            if close < 0:
                break
            i = close + 3
            continue
        if i + 1 < n and text[i + 1] == "!":
            close = text.find(">", i + 2)
            if close < 0:
                break
            i = close + 1
            continue
        if i + 1 < n and text[i + 1] == "?":
            close = text.find("?>", i + 2)
            if close < 0:
                break
            i = close + 2
            continue
        if i + 1 < n and text[i + 1] == "/":
            close = text.find(">", i + 2)
            if close < 0:
                break
            tag_name = _parse_closing_tag_name(text, i + 2, close)
            while stack:
                idx = stack.pop()
                if elements[idx].tag_name.lower() == tag_name.lower():
                    elements[idx].close_start = i
                    elements[idx].close_end = close
                    break
                elements[idx].self_closing = True
            i = close + 1
            continue
        if i + 1 < n and (text[i + 1].isalpha() or text[i + 1] == "_"):
            tag_start = i + 1
            j = tag_start
            while j < n and (text[j].isalnum() or text[j] in "-_:"):
                j += 1
            tag_name = text[tag_start:j]
            close = j
            while close < n:
                c = text[close]
                if c in ("\"", "'"):
                    end = text.find(c, close + 1)
                    if end < 0:
                        close = n
                        break
                    close = end + 1
                    continue
                if c == ">":
                    break
                close += 1
            if close >= n:
                break
            self_closing = ((close > 0 and text[close - 1] == "/")
                            or tag_name.lower() in _VOID_ELEMENTS)
            attrs_end = close - (1 if text[close - 1] == "/" else 0)
            el = _HtmlElement(
                tag_name=tag_name,
                attrs=_parse_attrs(text, j, attrs_end),
                open_start=i,
                open_end=close,
                self_closing=self_closing,
            )
            elements.append(el)
            if not self_closing:
                stack.append(len(elements) - 1)
            i = close + 1
            continue
        i += 1
    while stack:
        elements[stack.pop()].self_closing = True
    return elements


def _parse_closing_tag_name(text: str, start: int, end: int) -> str:
    i = start
    while i < end and (text[i].isalnum() or text[i] in "-_:"):
        i += 1
    return text[start:i]


def _parse_attrs(text: str, start: int, end: int) -> dict[str, str]:
    out: dict[str, str] = {}
    i = start
    while i < end:
        while i < end and text[i].isspace():
            i += 1
        if i >= end:
            break
        key_start = i
        while (i < end and not text[i].isspace()
               and text[i] not in "=/>"):
            i += 1
        if i == key_start:
            i += 1
            continue
        key = text[key_start:i]
        while i < end and text[i].isspace():
            i += 1
        value = ""
        if i < end and text[i] == "=":
            i += 1
            while i < end and text[i].isspace():
                i += 1
            if i < end and text[i] in ("\"", "'"):
                quote = text[i]
                i += 1
                v_start = i
                while i < end and text[i] != quote:
                    i += 1
                value = text[v_start:i]
                if i < end:
                    i += 1
            else:
                v_start = i
                while (i < end and not text[i].isspace()
                       and text[i] not in "/>"):
                    i += 1
                value = text[v_start:i]
        out[key] = value
    return out


def _decode_utf8(data: bytes) -> str:
    if len(data) >= 3 and data[0] == 0xEF and data[1] == 0xBB and data[2] == 0xBF:
        return data[3:].decode("utf-8")
    return data.decode("utf-8")
