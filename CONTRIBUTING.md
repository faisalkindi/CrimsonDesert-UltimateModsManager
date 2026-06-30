# Contributing to CDUMM

Thanks for your interest in improving the Crimson Desert Ultimate Mods Manager.
Bug reports, fixes, and well-scoped features are all welcome.

## Reporting bugs

CDUMM has a built-in report generator — use it. It captures your app version,
game version, storage layout, installed mods, and the recent log, which is
almost always what's needed to diagnose an issue.

1. In CDUMM, generate a bug report (the report screen / "Copy bug report").
2. Open a [bug report issue](https://github.com/faisalkindi/CrimsonDesert-UltimateModsManager/issues/new/choose)
   and paste it in.
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

Please run the test suite and the linter before opening a PR:

```bash
python -m pytest        # test suite (tests/)
ruff check .            # lint (line length 100, target py310)
```

Notes:

- Windows is the primary platform and the CI (`windows-tests`) runs the suite
  there. Linux/macOS have their own platform tests.
- A few full-table apply tests are heavy and marked `slow`; the fast suite runs
  with `-m "not slow"`.
- New fixes should come with a **regression test** that fails before the change
  and passes after. The `tests/` directory has many small, focused examples to
  mirror.

## Pull requests

1. Fork the repo and create a branch off `master`
   (e.g. `fix/short-description` or `feat/short-description`).
2. Keep changes surgical and on-topic — one logical change per PR. Match the
   surrounding code style rather than reformatting unrelated code.
3. Describe **what** broke and **why** your change fixes it. Link the issue it
   addresses (`Fixes #123`) and include the verification you ran.
4. Make sure `pytest` and `ruff` pass.

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
