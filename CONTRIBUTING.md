# Contributing to CDUMM

Thanks for your interest in improving the Crimson Desert Ultimate Mods Manager.
Bug reports, fixes, and well-scoped features are all welcome.

## Reporting bugs

CDUMM has a built-in report generator — use it. It captures your app version,
game version, storage layout, installed mods, and the recent log, which is
almost always what's needed to diagnose an issue.

1. In CDUMM, open **Bug Report** at the bottom of the sidebar and click
   **Refresh Report**: the page builds its report when CDUMM starts and
   doesn't update it on its own. Describe what happened, pick a severity, and
   click **Report on GitHub**. CDUMM copies the report to your clipboard and
   opens GitHub's **Bug report** form, or **Crash report** for a crash, with
   your description filled in.
2. Paste the report into the form's **CDUMM bug report** box and fill in the
   rest. If no browser opens, the report is still on your clipboard: open a
   [new issue](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/issues/new/choose),
   pick the form, and paste it there.
3. **Include one concrete example.** "Updates don't work" or "a mod won't
   apply" is hard to chase; "mod X (Nexus page link), version Y, shows error Z"
   is actionable. Name the specific mod, link its Nexus page, and say exactly
   what goes wrong.

Before filing, check the report's own TL;DR — it flags common environment
problems (running as administrator, the game under `Program Files`, the
`RUNASADMIN` compatibility flag) that break mod installs and aren't CDUMM bugs.

## Development setup

CDUMM is a Python application built on PySide6.

```bash
git clone https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager.git
cd CrimsonDesert-UltimateModsManager
python -m venv .venv
# Windows:  .venv\Scripts\activate
# Linux/macOS:  source .venv/bin/activate
pip install -e ".[dev]"
```

Run the app from source (the entry point is `src/cdumm/main.py`):

```bash
python src/cdumm/main.py
```

## Tests and linting

Please run the test suite, and lint the Python files you add, before opening
a PR:

```bash
python -m pytest                  # test suite (tests/)
ruff check path/to/new_file.py    # lint each Python file you add
```

Notes:

- Windows is the primary platform and the CI (`windows-tests`) runs the suite
  there. Linux/macOS have their own platform tests.
- A few full-table apply tests are heavy and marked `slow`; the fast suite runs
  with `-m "not slow"`.
- CI's `lint (ruff)` job runs ruff 0.16.0, the version pinned in the `dev`
  extra, with the settings in `pyproject.toml` (line length 100, target
  py310). The job fails only on findings in Python files the PR **adds**.
  Files the PR **modifies** are checked too, but their findings are only
  listed in the log, because the repo already carries a backlog. So
  `ruff check .` isn't expected to be clean, and you don't need to fix
  findings that were already in a file you touch.
- New fixes should come with a **regression test** that fails before the change
  and passes after. The `tests/` directory has many small, focused examples to
  mirror.

### If CDUMM or Crimson Desert is on your machine

Some tests reach your real CDUMM data or game install, and some of them write
there. CI has neither, so it never sees these writes; they only happen on
contributors' machines.

- **Your game install.** `test_multifile_mod_partial_skip.py`,
  `test_multifile_skip_surfaces_to_user.py` and
  `test_format3_target_basename_resolves.py::test_full_extraction_for_format3_target_succeeds`
  use the game at the hard-coded path
  `E:\SteamLibrary\steamapps\common\Crimson Desert`. The two
  `..._on_the_installed_game` tests in
  `test_ordered_fields_inline_error_block_swap.py` (slow suite) use
  `CDUMM_GAME_DIR` if it holds the game, and otherwise look in a
  `SteamLibrary` or `Steam` folder at the root of `C:` to `F:`. All of them
  build CDUMM's PAMT index caches (`.pamt_index_v5_*.cache`, hundreds of MB
  each) in the game's `CDMods` folder, or wherever CDUMM's settings moved it,
  and delete caches of other cache versions there.
  `test_multifile_skip_surfaces_to_user.py` also unpacks its test zip under
  `CDMods\_import_staging\`. `test_format3_reimport_self_source.py` (the same
  hard-coded `E:` path) hard-links the game's `0008\0.pamt` and `0008\0.paz`
  into a temporary folder, or copies them if it can't. On the same drive, a
  hard link is the game's own file.
- **A moved CDMods folder.** After you move CDMods in CDUMM's settings,
  `%LOCALAPPDATA%\cdumm\cdmods_path.txt` names the new folder, and many of
  CDUMM's lookups use that folder for any game folder, a test's temporary one
  included. Tests then write there: import staging folders, PAMT index caches
  (`test_format3_reimport_self_source.py` leaves a new one on every run) and
  `.post_apply_hash_cache.json`. The CLI tests in `test_cli_launch_game.py`
  and `test_cli_warning_visibility.py` open the `cdumm.db` there and run its
  schema setup and migrations. If the file names your game's own `CDMods`
  (after moving it back there, say), all of this lands in the game install.
- **CDUMM's crash trace.** Importing `cdumm.main` runs its start-up code on
  your real data folder: `%LOCALAPPDATA%\cdumm\crash_trace.txt` replaces
  `crash_trace.prev.txt`, and a new, empty one is opened.
  `test_emergency_crash_dump_skips_clean_exit.py` imports it as soon as
  pytest collects the file.

To keep a run away from all of this on Windows, point `LOCALAPPDATA` at a
scratch folder in the terminal you run the tests from, which moves CDUMM's
data folder there, and leave out the files that use a game install (their
other tests go with them):

```powershell
$env:LOCALAPPDATA = "$env:TEMP\cdumm-test-appdata"
python -m pytest --ignore=tests/test_multifile_mod_partial_skip.py --ignore=tests/test_multifile_skip_surfaces_to_user.py --ignore=tests/test_format3_target_basename_resolves.py --ignore=tests/test_ordered_fields_inline_error_block_swap.py --ignore=tests/test_format3_reimport_self_source.py
```

## Pull requests

1. Fork the repo and create a branch off `master`
   (e.g. `fix/short-description` or `feat/short-description`).
2. Keep changes surgical and on-topic — one logical change per PR. Match the
   surrounding code style rather than reformatting unrelated code.
3. Describe **what** broke and **why** your change fixes it. Link the issue it
   addresses (`Fixes #123`) and include the verification you ran.
4. Make sure `pytest` passes and any Python files you add pass `ruff check`.

## Code layout

- `src/cdumm/engine/` — import, apply, delta, and format handlers (the core).
- `src/cdumm/archive/` — PAZ/PAMT/PAPGT archive and overlay handling.
- `src/cdumm/gui/` — the PySide6 / Fluent-Widgets interface.
- `src/cdumm/storage/` — database, config, game discovery.
- `tests/` — pytest suite.
- `src/cdumm/_vendor/` — vendored third-party code (keeps its own license; see
  the `LICENSE_MPL2` files there). Don't relicense vendored code.

## License

By contributing, you agree that your contributions are licensed under the same
license as the project. See [`LICENSE`](LICENSE).
