"""Auto-bisect worker for the "Find Culprit" tool.

The Fluent rewrite (v3.0) moved the dialog UI into ``tool_page.py``
but accidentally deleted this module along with the worker class
that does the real work. v3.2 shipped with the import still
present at ``tool_page.py:1270`` — clicking "Find Culprit" raised
``ModuleNotFoundError`` and crashed the whole app (Priston201,
issue #45).

This module re-introduces only the worker. The UI is owned by
``tool_page.py``. Messaging goes through a plain Python
``queue.Queue`` (set by the caller as ``worker.msg_queue``) because
the worker runs in a ``threading.Thread`` rather than Qt's thread
pool — Qt signals fired from a non-Qt thread are silently dropped.
"""
from __future__ import annotations

import logging
import math
import time
from pathlib import Path
from queue import Queue
from typing import Any

from PySide6.QtCore import Qt

from cdumm.engine.apply_engine import ApplyWorker
from cdumm.engine.binary_search import DeltaDebugSession
from cdumm.engine.mod_manager import ModManager
from cdumm.storage.database import Database

logger = logging.getLogger(__name__)


class _AutoBisectWorker:
    """Drives the full auto-bisection loop on a background thread.

    Instantiated by ``ToolPage._on_run_clicked``. The caller assigns
    ``worker.msg_queue`` and calls ``worker.run()`` inside a plain
    ``threading.Thread``. The UI thread polls the queue every 200ms.

    Message protocol (tuples pushed to ``msg_queue``):
        ``("log", str)``                 — append a log line
        ``("progress", current, total)`` — update the progress bar
        ``("finished", dict)``           — bisection finished cleanly
        ``("error", str)``               — bisection aborted with error
    """

    def __init__(
        self,
        session: DeltaDebugSession | None,
        mm: ModManager,
        game_dir: Path | None,
        vanilla_dir: Path | None,
        db: Database | None,
        asi_mods: dict[int, dict] | None = None,
    ) -> None:
        self._session = session
        self._mm = mm
        self._game_dir = game_dir
        self._vanilla_dir = vanilla_dir
        self._db = db
        self._asi_mods = asi_mods or {}
        self._asi_manager = None
        self._cancelled = False
        self._paused = False
        # Set by `_should_break` whenever a launch_and_test was
        # aborted because the user paused (not because the game
        # crashed). The post-call check uses this instead of reading
        # `self._paused` live, which is racy: the user could resume
        # in the gap between `launch_and_test` returning and our
        # check, and we would then report a paused-out result as a
        # real "no crash" outcome and feed garbage to ddmin.
        self._broke_for_pause = False
        # Caller assigns this after construction
        # (see tool_page.py:1313).
        self.msg_queue: Queue | None = None

        if self._asi_mods and game_dir is not None:
            try:
                from cdumm.asi.asi_manager import AsiManager
                self._asi_manager = AsiManager(game_dir / "bin64")
            except Exception:
                self._asi_manager = None

    # ── External controls ───────────────────────────────────────

    def cancel(self) -> None:
        self._cancelled = True
        # If we're paused, unblock so the loop can exit.
        self._paused = False

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    # ── Messaging helpers ───────────────────────────────────────

    def _emit(self, kind: str, *payload: Any) -> None:
        if self.msg_queue is None:
            return
        if payload:
            if len(payload) == 1:
                self.msg_queue.put((kind, payload[0]))
            else:
                self.msg_queue.put((kind, *payload))
        else:
            self.msg_queue.put((kind,))

    def _log(self, text: str) -> None:
        self._emit("log", text)

    def _wait_if_paused(self) -> None:
        """Block while the user has the run paused.

        Returns promptly if cancelled. Sleeps in 200ms slices so a
        cancel during pause is responsive.
        """
        while self._paused and not self._cancelled:
            time.sleep(0.2)

    def _set_mod_enabled(
        self, mod_id: int, enabled: bool, thread_mm: ModManager
    ) -> None:
        """Toggle a mod. ASI plugins live on disk (rename), PAZ
        mods live in the DB."""
        if mod_id < 0 and mod_id in self._asi_mods and self._asi_manager:
            plugin = self._asi_mods[mod_id].get("_plugin")
            if not plugin:
                return
            try:
                # Re-scan: file path may have changed if a previous
                # round flipped this plugin.
                for p in self._asi_manager.scan():
                    if p.name == plugin.name:
                        plugin = p
                        break
                if enabled:
                    self._asi_manager.enable(plugin)
                else:
                    self._asi_manager.disable(plugin)
            except OSError as e:
                self._log(
                    f"  Warning: failed to toggle ASI {plugin.name}: {e}"
                )
        else:
            thread_mm.set_enabled(mod_id, enabled)

    # ── Main loop ───────────────────────────────────────────────

    def run(self) -> None:
        from cdumm.engine.game_monitor import launch_and_test

        # SQLite handles can't cross threads — open a fresh one here.
        db_path = self._game_dir / "CDMods" / "cdumm.db"
        thread_db = Database(db_path)
        thread_db.initialize()
        thread_mm = ModManager(
            thread_db, self._game_dir / "CDMods" / "deltas"
        )

        # cancel_check passed into launch_and_test must also break
        # out of the long stable-window wait when the user pauses,
        # otherwise pause feels broken (game keeps running). The
        # closure also flips `_broke_for_pause` so the post-call
        # branch can tell "game crashed" from "user paused".
        def _should_break() -> bool:
            if self._cancelled:
                return True
            if self._paused:
                self._broke_for_pause = True
                return True
            return False

        # Snapshot from the thread's own ModManager view, which
        # always reflects truth on disk. Falling back to the
        # caller-thread session here was unsafe when session was
        # None (test paths) and stale when session was set (the
        # state could have changed between session construction and
        # the worker thread starting).
        all_culprits: list[tuple[int, str]] = []
        original_state: dict[int, bool] = {
            m["id"]: m["enabled"] for m in thread_mm.list_mods()
        }
        for em in self._asi_mods.values():
            original_state[em["id"]] = em.get("enabled", False)
        total_rounds = 0

        try:
            # Outer: find a culprit, disable it, re-test until stable.
            while not self._cancelled:
                self._wait_if_paused()
                if self._cancelled:
                    break

                enabled_mods = [
                    m for m in thread_mm.list_mods() if m["enabled"]
                ]
                # Include enabled ASI plugins as fake-id mods so they
                # participate in bisection.
                for em in self._asi_mods.values():
                    if em.get("enabled"):
                        enabled_mods.append(em)
                n = len(enabled_mods)

                if n < 2:
                    self._log(
                        "Less than 2 mods remaining — done."
                    )
                    break

                estimated = max(1, 2 * math.ceil(math.log2(n)))
                self._log("\n" + ("═" * 40))
                if all_culprits:
                    found = ", ".join(name for _, name in all_culprits)
                    self._log(
                        f"Re-testing {n} remaining mods "
                        f"(already found: {found})..."
                    )
                else:
                    self._log(f"Testing {n} enabled mods...")
                self._log("═" * 40)

                # Verify the crash still reproduces.
                self._log("\nVerifying crash reproduces...")
                for m in enabled_mods:
                    self._set_mod_enabled(m["id"], True, thread_mm)

                verify_errors: list[str] = []
                worker = ApplyWorker(
                    self._game_dir, self._vanilla_dir,
                    db_path, force_outdated=True
                )
                # Explicit DirectConnection: ApplyWorker.run() is
                # called synchronously here on this background
                # thread, so emit() must invoke the slot inline.
                worker.error_occurred.connect(
                    verify_errors.append, Qt.DirectConnection
                )
                worker.run()

                if verify_errors:
                    self._log(
                        "  ✗ Apply failed during verification: "
                        f"{verify_errors[0]}"
                    )
                    self._log(
                        "  Continuing anyway — may be transient."
                    )

                if self._cancelled:
                    break
                self._wait_if_paused()
                if self._cancelled:
                    break

                self._broke_for_pause = False
                crashed = launch_and_test(
                    self._game_dir,
                    stable_seconds=90,
                    launch_timeout=60,
                    log_cb=lambda msg: self._log(f"  {msg}"),
                    cancel_check=_should_break,
                )

                # _broke_for_pause is the authoritative signal here;
                # reading self._paused live races against the user
                # clicking Resume in the gap.
                if self._broke_for_pause and not self._cancelled:
                    self._wait_if_paused()
                    if self._cancelled:
                        break
                    continue
                if self._cancelled:
                    break

                if not crashed:
                    self._log(
                        "✓ Game is stable — no more crashes!"
                    )
                    break

                self._log(
                    "✗ Crash confirmed. Starting bisection...\n"
                )

                # Fresh ddmin session for this round.
                session = DeltaDebugSession(
                    thread_mm,
                    extra_mods=list(self._asi_mods.values()),
                )
                round_num = 0

                while not session.is_done() and not self._cancelled:
                    self._wait_if_paused()
                    if self._cancelled:
                        break

                    round_num += 1
                    total_rounds += 1
                    config = session.start_round()
                    test_count = len(session.current_group)

                    self._log(f"\n─── Round {round_num} ───")
                    self._log(
                        f"Testing {test_count} of "
                        f"{len(session._changes)} suspects"
                    )
                    self._emit(
                        "progress",
                        total_rounds, total_rounds + estimated,
                    )

                    self._log("Applying mod configuration...")
                    for mod_id, en in config.items():
                        self._set_mod_enabled(mod_id, en, thread_mm)

                    apply_errors: list[str] = []
                    worker = ApplyWorker(
                        self._game_dir, self._vanilla_dir,
                        db_path, force_outdated=True
                    )
                    worker.error_occurred.connect(
                        apply_errors.append, Qt.DirectConnection
                    )
                    worker.run()

                    if apply_errors:
                        self._log(
                            f"  ✗ Apply failed: {apply_errors[0]}"
                        )
                        self._log(
                            "  Treating as crash for this round."
                        )
                        session.report_crash(True)
                        self._save_progress(thread_db, session)
                        continue

                    if self._cancelled:
                        break
                    self._wait_if_paused()
                    if self._cancelled:
                        break

                    self._log("Launching game through Steam...")
                    self._broke_for_pause = False
                    crashed = launch_and_test(
                        self._game_dir,
                        stable_seconds=90,
                        launch_timeout=60,
                        log_cb=lambda msg: self._log(f"  {msg}"),
                        cancel_check=_should_break,
                    )

                    # See verify-block comment: _broke_for_pause is
                    # set inside _should_break at the moment pause
                    # took effect; checking self._paused live here
                    # would race a fast Resume click and mis-record
                    # this round's result.
                    if self._broke_for_pause and not self._cancelled:
                        self._wait_if_paused()
                        if self._cancelled:
                            break
                        # Don't report_crash on a paused-out result.
                        continue
                    if self._cancelled:
                        break

                    self._log(
                        f"Result: {'CRASHED' if crashed else 'OK'}"
                    )
                    session.report_crash(crashed)
                    self._save_progress(thread_db, session)

                if self._cancelled:
                    break

                result = session.get_result()
                minimal = result.get("minimal_set", [])
                if minimal:
                    context_names = [
                        em["name"] for em in enabled_mods
                        if em["name"] not in [m["name"] for m in minimal]
                    ]
                    for m in minimal:
                        name = m["name"]
                        all_culprits.append((m["id"], name))
                        self._set_mod_enabled(
                            m["id"], False, thread_mm
                        )
                        # PAZ mods only — ASI mods use negative IDs.
                        if m["id"] > 0:
                            try:
                                thread_mm.flag_crash(
                                    m["id"],
                                    crashes_alone=len(minimal) == 1,
                                    context_mods=context_names[:10],
                                    rounds=round_num,
                                )
                            except Exception:
                                logger.debug(
                                    "flag_crash failed for mod %s",
                                    m["id"], exc_info=True,
                                )
                        self._log(f"\n★ CULPRIT FOUND: {name}")
                        self._log(
                            "  Flagged in crash registry. "
                            "Checking for more..."
                        )
                else:
                    self._log("No single culprit found in this pass.")
                    break

            # Restore original mod state, keeping culprits disabled.
            self._log("\nRestoring mod state...")
            culprit_ids = {cid for cid, _ in all_culprits}
            for mod_id, en in original_state.items():
                if mod_id in culprit_ids:
                    self._set_mod_enabled(mod_id, False, thread_mm)
                else:
                    self._set_mod_enabled(mod_id, en, thread_mm)

            worker = ApplyWorker(
                self._game_dir, self._vanilla_dir,
                db_path, force_outdated=True
            )
            worker.run()
            thread_db.close()

            if self._cancelled:
                self._log("Bisection cancelled. Mods restored.")
                self._emit("error", "Cancelled by user")
            else:
                final_result = {
                    "minimal_set": [
                        {"id": cid, "name": n}
                        for cid, n in all_culprits
                    ],
                    "rounds": total_rounds,
                    "is_single": len(all_culprits) == 1,
                    "is_combination": False,
                }
                self._log(
                    f"\nDone! Found {len(all_culprits)} problem "
                    f"mod(s) in {total_rounds} rounds."
                )
                self._emit("finished", final_result)

        except Exception as e:
            logger.error("Auto bisect failed: %s", e, exc_info=True)
            try:
                for mod_id, en in original_state.items():
                    self._set_mod_enabled(mod_id, en, thread_mm)
                thread_db.close()
            except Exception:
                pass
            self._emit("error", str(e))

    # ── Resume-after-restart support ────────────────────────────

    def _save_progress(self, db: Database, session: DeltaDebugSession) -> None:
        """Persist ddmin state so a crashed CDUMM session can resume.

        Best-effort. Failure here shouldn't abort the bisection.
        """
        try:
            import json
            s = session
            data = json.dumps({
                "all_ids": s.all_ids,
                "changes": s._changes,
                "n": s._n,
                "partition_index": s._partition_index,
                "testing_complement": s._testing_complement,
                "round_number": s.round_number,
                "history": s.history,
                "phase": s.phase,
            })
            db.connection.execute(
                "CREATE TABLE IF NOT EXISTS ddmin_progress "
                "(id INTEGER PRIMARY KEY, data TEXT)"
            )
            db.connection.execute(
                "INSERT OR REPLACE INTO ddmin_progress "
                "(id, data) VALUES (1, ?)",
                (data,),
            )
            db.connection.commit()
        except Exception:
            logger.debug("save_progress failed", exc_info=True)
