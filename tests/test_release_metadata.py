"""Release metadata contract tests."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.release_metadata import ReleaseMetadataError, verify_release_metadata


def _write_release_files(root: Path, *, version: str) -> None:
    (root / "custom_components/linking_the_world_temp_ha").mkdir(parents=True)
    (root / "custom_components/linking_the_world_temp_ha/manifest.json").write_text(
        '{"version": "' + version + '"}\n', encoding="utf-8"
    )
    (root / "CHANGELOG.md").write_text(
        "# Changelog\n\n## " + version + " - Release\n", encoding="utf-8"
    )
    (root / "README.md").write_text(
        "# Integration\n\n当前版本 `" + version + "`。\n", encoding="utf-8"
    )


class ReleaseMetadataTests(unittest.TestCase):
    """Release metadata contract tests."""

    def test_accepts_one_version_fact(self) -> None:
        """Manifest, changelog, README and a release tag must agree."""
        with self.subTest("matching metadata"):
            root = Path(self.enterContext(tempfile.TemporaryDirectory()))
            _write_release_files(root, version="1.0.3")

            self.assertEqual(verify_release_metadata(root, tag="v1.0.3"), "1.0.3")

    def test_rejects_a_tag_for_another_release(self) -> None:
        """A tag must never publish files that describe a different version."""
        with self.subTest("mismatched tag"):
            root = Path(self.enterContext(tempfile.TemporaryDirectory()))
            _write_release_files(root, version="1.0.3")

            with self.assertRaisesRegex(ReleaseMetadataError, "Git tag"):
                verify_release_metadata(root, tag="v1.0.2")
