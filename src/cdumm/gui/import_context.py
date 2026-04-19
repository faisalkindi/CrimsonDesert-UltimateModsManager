"""Per-launch snapshot of the window's import-context fields.

mods_page.py stages fields like `_update_priority`, `_configurable_source`
on the main window before calling `_launch_import_worker(path)`. On
completion, `_on_finished` reads those same fields to persist their
values into the DB (priority restore, configurable flag, etc.).

Between launch and finished, the fields are stored on the shared
window instance. If the user triggers another swap before the first
proc completes, the new values would overwrite the first proc's
state, and the first proc's handler would persist the wrong values.

Snapshotting at launch + clearing the window-side slots lets each
launch ride with its own context on the QProcess instance.
"""
from __future__ import annotations

IMPORT_CONTEXT_KEYS: tuple[str, ...] = (
    "update_priority",
    "update_enabled",
    "configurable_source",
    "configurable_labels",
    "variant_leaf_rel",
    "original_drop_path",
)


def snapshot_and_clear_import_context(win) -> dict:
    """Read the import-context fields off `win`, reset them, return the snapshot.

    Reads are via getattr with a None default so missing attributes
    don't raise. Clears every field except `_original_drop_path` —
    that one is reused by the error reporter across retries, so we
    don't wipe it from the window (the snapshot still carries the
    value for the launch that captured it).
    """
    ctx = {k: getattr(win, f"_{k}", None) for k in IMPORT_CONTEXT_KEYS}
    win._update_priority = None
    win._update_enabled = None
    win._configurable_source = None
    win._configurable_labels = None
    win._variant_leaf_rel = None
    return ctx
