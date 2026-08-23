"""Regression test for Home Assistant task lifecycle integration."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HUB_SOURCE = (
    ROOT / "custom_components" / "linking_the_world_temp_ha" / "hub.py"
).read_text(encoding="utf-8")


class TaskLifecycleTests(unittest.TestCase):
    def test_long_running_session_uses_config_entry_background_task(self) -> None:
        """The TCP loop must not be tracked as a Home Assistant startup task."""
        self.assertIn("self.entry.async_create_background_task(", HUB_SOURCE)
        self.assertNotIn("self.hass.async_create_task(", HUB_SOURCE)


if __name__ == "__main__":
    unittest.main()
