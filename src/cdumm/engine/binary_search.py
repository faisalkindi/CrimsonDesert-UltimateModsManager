"""Delta Debugging (ddmin) algorithm for finding minimal crash-inducing mod set.

Based on Andreas Zeller's ddmin algorithm. Finds the smallest subset of
enabled mods that causes a crash. Works for single bad mods, conflict pairs,
and multi-mod interactions.

Best case: ~2*log2(n) rounds (single bad mod)
Typical: ~15-20 rounds for 12 mods
Worst case: n² + 3n (pathological, unlikely)
"""

import logging

logger = logging.getLogger(__name__)


class DeltaDebugSession:
    """Manages the ddmin algorithm state."""

    def __init__(self, mod_manager, extra_mods: list[dict] | None = None):
        self._mm = mod_manager
        self.original_state = {m["id"]: m["enabled"] for m in mod_manager.list_mods()}
        self.enabled_mods = [m for m in mod_manager.list_mods() if m["enabled"]]
        # Include extra mods (e.g. ASI plugins with fake negative IDs)
        if extra_mods:
            for em in extra_mods:
                self.original_state[em["id"]] = em["enabled"]
                if em["enabled"]:
                    self.enabled_mods.append(em)
        self.all_ids = [m["id"] for m in self.enabled_mods]

        # ddmin state
        self._changes = list(self.all_ids)  # current failure-inducing set
        self._n = 2  # number of partitions
        self._partition_index = 0  # which partition we're testing
        self._testing_complement = False  # testing partition or complement?
        self._test_set = []  # current set being tested

        self.round_number = 0
        self.history = []
        self.phase = "running"  # running, done
        self.current_group = []

    def get_mod_name(self, mod_id: int) -> str:
        for m in self.enabled_mods:
            if m["id"] == mod_id:
                return m["name"]
        return f"Mod {mod_id}"

    def get_phase_description(self) -> str:
        if self.phase == "done":
            return "Done"
        n_changes = len(self._changes)
        return f"Testing ({self._n} partitions, {n_changes} mods remaining)"

    def start_round(self) -> dict[int, bool]:
        """Get the next test configuration. Returns {mod_id: enabled}."""
        self.round_number += 1

        if self.phase == "done":
            return {mid: False for mid in self.original_state}

        # Build partitions
        partitions = self._split(self._changes, self._n)

        if self._partition_index < len(partitions):
            if not self._testing_complement:
                # Test the partition (small subset)
                self._test_set = partitions[self._partition_index]
            else:
                # Test the complement (everything except this partition)
                self._test_set = [
                    mid for mid in self._changes
                    if mid not in partitions[self._partition_index]
                ]
        else:
            # Shouldn't happen — advance will handle
            self._test_set = list(self._changes)

        self.current_group = list(self._test_set)

        # Build enable/disable map
        changes = {mid: False for mid in self.original_state}
        for mid in self._test_set:
            changes[mid] = True

        logger.info("Round %d: testing %d mods (n=%d, part=%d, complement=%s)",
                     self.round_number, len(self._test_set),
                     self._n, self._partition_index, self._testing_complement)
        return changes

    def report_crash(self, crashed: bool) -> str:
        """Process feedback and advance the algorithm. Returns status message."""
        self.history.append({
            "round": self.round_number,
            "tested": [self.get_mod_name(m) for m in self.current_group],
            "count": len(self.current_group),
            "crashed": crashed,
        })

        partitions = self._split(self._changes, self._n)

        if not self._testing_complement:
            # We tested a partition
            if crashed:
                # Partition alone crashes — reduce to it
                self._changes = list(self._test_set)
                self._n = 2
                self._partition_index = 0
                self._testing_complement = False
                return self._check_done(f"Narrowed to {len(self._changes)} mods")
            else:
                # Partition didn't crash — try its complement
                self._testing_complement = True
                return "Testing complement..."
        else:
            # We tested a complement
            if crashed:
                # Complement crashes — reduce to it (remove this partition)
                self._changes = list(self._test_set)
                self._n = max(self._n - 1, 2)
                self._partition_index = 0
                self._testing_complement = False
                return self._check_done(f"Narrowed to {len(self._changes)} mods")
            else:
                # Neither partition nor complement crashes alone
                # Move to next partition
                self._testing_complement = False
                self._partition_index += 1

                if self._partition_index >= len(partitions):
                    # Tried all partitions at this granularity
                    if self._n >= len(self._changes):
                        # Can't split further — we have the minimal set
                        self.phase = "done"
                        return "Found minimal crash set"
                    # Increase granularity
                    self._n = min(self._n * 2, len(self._changes))
                    self._partition_index = 0
                    return f"Increasing granularity to {self._n} partitions"

                return "Testing next partition..."

    def _check_done(self, msg: str) -> str:
        """Check if we've narrowed to minimum."""
        if len(self._changes) <= 1:
            self.phase = "done"
            return "Found the problem mod"
        if self._n > len(self._changes):
            self._n = len(self._changes)
        return msg

    def _split(self, items: list, n: int) -> list[list]:
        """Split items into n roughly equal partitions."""
        k = max(1, len(items) // n)
        parts = []
        for i in range(0, len(items), k):
            part = items[i:i + k]
            if part:
                parts.append(part)
        return parts

    def is_done(self) -> bool:
        return self.phase == "done"

    def get_restore_changes(self) -> dict[int, bool]:
        return dict(self.original_state)

    def get_result(self) -> dict:
        minimal_set = self._changes if self.phase == "done" else []
        return {
            "minimal_set": [
                {"id": mid, "name": self.get_mod_name(mid)}
                for mid in minimal_set
            ],
            "rounds": self.round_number,
            "history": self.history,
            "is_single": len(minimal_set) == 1,
            "is_combination": len(minimal_set) > 1,
        }


# Backward compat alias
BinarySearchSession = DeltaDebugSession
