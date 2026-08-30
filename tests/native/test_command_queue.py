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


def replacement(expected: str, *, intent: str = "target_temperature"):
    return queue.QueuedCommand(
        "客餐厅 温控面板 设定温度",
        "thermostat_panel",
        {intent: expected},
        b"panelmac",
        3,
        int(expected) * 2 if intent == "target_temperature" else None,
    )


class CommandQueueTests(unittest.TestCase):
    def test_non_heat_humidifier_off_reply_rejects_attempted_enable(self) -> None:
        command = queue.PendingCommand(
            "冬季加湿",
            "system",
            {"winter_humidifier": "ON"},
            10.0,
            18.0,
            12.0,
            b"systemmac",
            5,
            1,
        )

        self.assertTrue(
            queue.controller_rejected_command(
                command,
                {"mode": "cool", "winter_humidifier": "OFF"},
            )
        )
        self.assertFalse(
            queue.controller_rejected_command(
                command,
                {"mode": "heat", "winter_humidifier": "OFF"},
            )
        )

    def test_first_status_poll_leaves_one_second_for_push_confirmation(self) -> None:
        self.assertEqual(queue.first_status_poll_at(10.0, 8.0), 11.0)

    def test_short_confirmation_window_still_leaves_time_for_fallback(self) -> None:
        self.assertEqual(queue.first_status_poll_at(10.0, 1.0), 10.5)

    def test_repeated_pending_value_cancels_a_stale_replacement(self) -> None:
        queued = queue.coalesce_queued(pending(), (), replacement("22"))
        self.assertEqual(
            queue.coalesce_queued(pending(), queued, replacement("21")), []
        )

    def test_only_the_latest_value_is_retained(self) -> None:
        first = queue.coalesce_queued(pending(), (), replacement("22"))
        latest = queue.coalesce_queued(pending(), first, replacement("23"))
        self.assertEqual(
            [command.expected for command in latest],
            [{"target_temperature": "23"}],
        )

    def test_independent_intents_keep_their_arrival_order(self) -> None:
        temperature = queue.coalesce_queued(pending(), (), replacement("22"))
        power = queue.coalesce_queued(
            pending(), temperature, replacement("OFF", intent="power")
        )
        latest_temperature = queue.coalesce_queued(pending(), power, replacement("23"))
        self.assertEqual(
            [command.expected for command in latest_temperature],
            [{"power": "OFF"}, {"target_temperature": "23"}],
        )

    def test_queued_value_has_no_early_promotion_deadline(self) -> None:
        queued = queue.coalesce_queued(pending(), (), replacement("22"))[0]
        self.assertFalse(hasattr(queued, "promote_at"))

    def test_tracked_command_requires_one_intent(self) -> None:
        with self.assertRaises(ValueError):
            queue.command_intent({})
        with self.assertRaises(ValueError):
            queue.command_intent({"power": "ON", "mode": "cool"})

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
