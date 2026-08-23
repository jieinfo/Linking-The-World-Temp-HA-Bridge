"""Regression tests for latest-value command coalescing."""

from __future__ import annotations

import importlib
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_NAME = "custom_components.linking_the_world_temp_ha"
if PACKAGE_NAME not in sys.modules:
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(ROOT / "custom_components" / "linking_the_world_temp_ha")]
    sys.modules[PACKAGE_NAME] = package

queue = importlib.import_module(f"{PACKAGE_NAME}.command_queue")


def pending(expected: str = "21"):
    return queue.PendingCommand(
        "客餐厅 温控面板 设定温度",
        "thermostat_panel",
        {"target_temperature": expected},
        10.0,
        18.0,
        12.0,
        b"panelmac",
        3,
        int(expected) * 2,
    )


def replacement(expected: str):
    return queue.QueuedCommand(
        "客餐厅 温控面板 设定温度",
        "thermostat_panel",
        {"target_temperature": expected},
        b"panelmac",
        3,
        int(expected) * 2,
    )


class CommandQueueTests(unittest.TestCase):
    def test_repeated_pending_value_cancels_a_stale_replacement(self) -> None:
        queued = queue.coalesce_latest(pending(), None, replacement("22"), 10.2)
        self.assertIsNotNone(queued)
        self.assertIsNone(
            queue.coalesce_latest(pending(), queued, replacement("21"), 10.4)
        )

    def test_identical_replacement_does_not_extend_the_debounce(self) -> None:
        first = queue.coalesce_latest(pending(), None, replacement("22"), 10.2)
        assert first is not None
        repeated = queue.coalesce_latest(pending(), first, replacement("22"), 10.6)
        self.assertIs(repeated, first)

    def test_latest_value_is_sent_after_input_settles(self) -> None:
        first = queue.coalesce_latest(pending(), None, replacement("22"), 10.2)
        assert first is not None
        latest = queue.coalesce_latest(pending(), first, replacement("23"), 10.5)
        assert latest is not None
        self.assertEqual(latest.expected, {"target_temperature": "23"})
        self.assertFalse(queue.replacement_is_ready(pending(), latest, 11.24))
        self.assertTrue(queue.replacement_is_ready(pending(), latest, 11.25))

    def test_continuous_input_has_a_bounded_wait(self) -> None:
        current = queue.coalesce_latest(pending(), None, replacement("22"), 10.0)
        assert current is not None
        for index, value in enumerate(("23", "24", "25", "26"), start=1):
            current = queue.coalesce_latest(
                pending(), current, replacement(value), 10.0 + index * 0.7
            )
            assert current is not None
        self.assertEqual(current.promote_at, 13.0)
        self.assertTrue(queue.replacement_is_ready(pending(), current, 13.0))

    def test_pending_timeout_remains_the_hard_deadline(self) -> None:
        short_pending = pending()
        short_pending.deadline = 10.5
        queued = queue.coalesce_latest(
            short_pending, None, replacement("22"), 10.0
        )
        assert queued is not None
        self.assertTrue(
            queue.replacement_is_ready(short_pending, queued, short_pending.deadline)
        )

    def test_temperature_setpoint_can_only_be_retried_once(self) -> None:
        command = pending("22")
        self.assertTrue(queue.temperature_retry_is_allowed(command))
        command.attempts = 2
        self.assertFalse(queue.temperature_retry_is_allowed(command))

    def test_non_temperature_command_is_not_retryable(self) -> None:
        command = pending("22")
        command.target = "system"
        self.assertFalse(queue.temperature_retry_is_allowed(command))


if __name__ == "__main__":
    unittest.main()
