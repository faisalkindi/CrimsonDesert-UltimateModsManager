"""G1: right-click → "Reimport from source" for one or many selected
mods. Regenerates deltas against current vanilla without requiring
the user to drag-drop each zip manually.

Context: a game update invalidates every mod's stored delta (they
were computed against OLD vanilla). After Steam 1.04 Faisal had 22
mods needing manual re-import. This feature does the bulk of that
for him.
"""
from __future__ import annotations

import re
from pathlib import Path


def _mods_page_src() -> str:
    return (Path(__file__).resolve().parents[1]
            / "src" / "cdumm" / "gui" / "pages" / "mods_page.py"
            ).read_text(encoding="utf-8")


def _worker_src() -> str:
    return (Path(__file__).resolve().parents[1]
            / "src" / "cdumm" / "worker_process.py").read_text(
                encoding="utf-8")


# ── Worker wiring ─────────────────────────────────────────────────────

def test_worker_accepts_reimport_batch_command():
    src = _worker_src()
    assert "reimport_batch" in src, (
        "worker_process must accept a 'reimport_batch' command so the "
        "GUI can spawn a subprocess that re-runs import with "
        "existing_mod_id preserved per mod")


def test_reimport_batch_uses_existing_mod_id_per_entry():
    """The worker must call import handlers with existing_mod_id so
    we don't duplicate mod rows — we want to REGENERATE deltas for
    the same mod id, not create a new one."""
    src = _worker_src()
    # Find the reimport batch function.
    anchor = src.find("def _run_reimport_batch")
    assert anchor != -1, (
        "expected _run_reimport_batch function in worker_process")
    # Scope: body of the function.
    next_def = src.find("\ndef ", anchor + 20)
    body = src[anchor:next_def if next_def != -1 else anchor + 5000]
    # Must pass existing_mod_id= to the import handlers.
    assert "existing_mod_id=" in body, (
        "_run_reimport_batch must pass existing_mod_id per entry so "
        "the import handler preserves the existing mod row")


# ── GUI context-menu wiring ──────────────────────────────────────────

def test_context_menu_has_reimport_for_multi_select():
    src = _mods_page_src()
    anchor = src.find("def _show_mod_context_menu")
    assert anchor != -1
    next_def = src.find("\n    def ", anchor + 20)
    body = src[anchor:next_def if next_def != -1 else anchor + 8000]
    # Must add a reimport action for multi-select.
    assert re.search(r"[Rr]eimport", body), (
        "multi-select context menu must include a Reimport action")
    # Must reference batch handler.
    assert "_ctx_batch_reimport" in body, (
        "multi-select Reimport action must call _ctx_batch_reimport")


def test_context_menu_has_reimport_for_single_select():
    """Single-select case should also offer Reimport since a user may
    right-click one mod after a game update."""
    src = _mods_page_src()
    anchor = src.find("def _show_mod_context_menu")
    assert anchor != -1
    next_def = src.find("\n    def ", anchor + 20)
    body = src[anchor:next_def if next_def != -1 else anchor + 8000]
    # Single-select block is after the `else:` after multi-branch.
    # Just assert both a batch and single variant are wired.
    assert body.count("reimport") + body.count("Reimport") >= 2, (
        "both multi-select AND single-select branches should "
        "include a Reimport entry")


def test_batch_handler_exists():
    src = _mods_page_src()
    assert "def _ctx_batch_reimport" in src, (
        "need a _ctx_batch_reimport(self, mod_ids) method that "
        "spawns the reimport_batch worker with the selected mods'"
        " source paths")


def test_batch_reimport_prioritises_json_source_over_folder():
    """Glider Stamina / Infinite Horse regression: when a JSON-patch
    mod was originally imported from a multi-preset folder, the
    sources/<id>/ archive holds ALL the original presets (5 for
    Glider, 9 for Horse). If reimport hands the FOLDER to the worker,
    import_from_folder splits each preset into its own mod row and
    explodes one mod into N. json_source already points at the user's
    chosen single preset (deltas/<id>/source.json), so it must be the
    primary source for reimport — folder is the fallback for
    PAZ-archive mods that have no json_source.
    """
    src = _mods_page_src()
    anchor = src.find("def _ctx_batch_reimport")
    assert anchor != -1
    next_def = src.find("\n    def ", anchor + 20)
    body = src[anchor:next_def if next_def != -1 else anchor + 8000]
    # The json_source check must appear before the source_path check
    # in the resolution chain. Find the first occurrence of each.
    js_idx = body.find('m.get("json_source")')
    sp_idx = body.find('m.get("source_path")')
    assert js_idx != -1, (
        "_ctx_batch_reimport must read json_source from the mod row")
    assert sp_idx != -1, (
        "_ctx_batch_reimport must read source_path from the mod row")
    # Both must appear, but the json_source resolution branch
    # (`if js and os.path.isfile(js)`) must precede the source_path
    # folder branch (`elif sp and os.path.isdir(sp)`).
    js_branch = body.find("if js and os.path.isfile(js)")
    sp_dir_branch = body.find("elif sp and os.path.isdir(sp)")
    assert js_branch != -1 and sp_dir_branch != -1, (
        "expected explicit if/elif chain checking json_source first, "
        "source_path folder second")
    assert js_branch < sp_dir_branch, (
        "json_source must be checked BEFORE source_path folder so "
        "JSON-patch mods reimport their chosen preset, not every "
        "preset in the archived sources/<id>/ folder. Reversing this "
        "order causes the Glider Stamina / Infinite Horse explosion "
        "where one mod becomes 5+ separate rows.")
