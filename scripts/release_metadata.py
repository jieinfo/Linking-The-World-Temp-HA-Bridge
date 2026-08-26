"""Validate the version facts that identify one native integration release."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


class ReleaseMetadataError(ValueError):
    """Raised when release metadata does not describe one version."""


_CHANGELOG_VERSION = re.compile(r"^##\s+([0-9]+\.[0-9]+\.[0-9]+)\b", re.MULTILINE)


def _read_manifest_version(root: Path) -> str:
    manifest = root / "custom_components/linking_the_world_temp_ha/manifest.json"
    try:
        version = json.loads(manifest.read_text(encoding="utf-8"))["version"]
    except (FileNotFoundError, json.JSONDecodeError, KeyError) as error:
        raise ReleaseMetadataError(f"Cannot read manifest version: {error}") from error
    if not isinstance(version, str):
        raise ReleaseMetadataError("Manifest version must be a string")
    return version


def _read_changelog_version(root: Path) -> str:
    changelog = root / "CHANGELOG.md"
    match = _CHANGELOG_VERSION.search(changelog.read_text(encoding="utf-8"))
    if match is None:
        raise ReleaseMetadataError("CHANGELOG.md must begin with a semantic version heading")
    return match.group(1)


def verify_release_metadata(root: Path, *, tag: str | None = None) -> str:
    """Return the release version after ensuring all published facts agree."""
    manifest_version = _read_manifest_version(root)
    changelog_version = _read_changelog_version(root)
    readme = (root / "README.md").read_text(encoding="utf-8")

    if manifest_version != changelog_version:
        raise ReleaseMetadataError(
            "Manifest and CHANGELOG versions differ: "
            f"{manifest_version} != {changelog_version}"
        )
    if f"`{manifest_version}`" not in readme:
        raise ReleaseMetadataError(
            f"README.md must identify the current release as `{manifest_version}`"
        )
    if tag is not None and tag.removeprefix("v") != manifest_version:
        raise ReleaseMetadataError(
            f"Git tag {tag} does not match release version {manifest_version}"
        )
    return manifest_version


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--tag", help="Release tag, for example v1.0.3")
    args = parser.parse_args()
    print(verify_release_metadata(args.root, tag=args.tag))


if __name__ == "__main__":
    main()
