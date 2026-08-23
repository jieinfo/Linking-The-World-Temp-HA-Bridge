"""Regression tests for conservative thermostat command serialization."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HUB_PATH = ROOT / "custom_components" / "linking_the_world_temp_ha" / "hub.py"
HUB_SOURCE = HUB_PATH.read_text(encoding="utf-8")
HUB_TREE = ast.parse(HUB_SOURCE)


def method_source(name: str) -> str:
    """Return one hub method from the parsed source."""
    for node in ast.walk(HUB_TREE):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            source = ast.get_source_segment(HUB_SOURCE, node)
            assert source is not None
            return source
    raise AssertionError(f"hub method not found: {name}")


class SerialTemperatureCommandTests(unittest.TestCase):
    def test_session_loop_never_promotes_an_unconfirmed_replacement(self) -> None:
        session_loop = method_source("_async_session_loop")
        self.assertNotIn("_promote_superseded", session_loop)

    def test_unsent_queued_value_cannot_confirm_the_pending_command(self) -> None:
        confirm_pending = method_source("_confirm_pending")
        self.assertNotIn("self._queued", confirm_pending)
        self.assertIn("self._pending", confirm_pending)


if __name__ == "__main__":
    unittest.main()
