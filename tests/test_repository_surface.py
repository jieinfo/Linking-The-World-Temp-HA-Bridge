"""Repository-level checks for the HACS-only distribution."""

from __future__ import annotations

from pathlib import Path
import json
import unittest


ROOT = Path(__file__).resolve().parents[1]
OLD_REPOSITORY_URL = "https://github.com/jieinfo/" + (
    "Linking-The-World-Temp-HA-" + "Bridge"
)
NEW_REPOSITORY_URL = (
    "https://github.com/jieinfo/Linking-The-World-Temp-HA-Integration"
)


class RepositorySurfaceTests(unittest.TestCase):
    """Ensure the repository contains only the native integration delivery path."""

    def test_legacy_addon_delivery_files_are_absent(self) -> None:
        """The removed MQTT add-on must not remain installable from main."""
        retired_paths = (
            "bridge.py",
            "test_protocol.py",
            "scripts/sync_addon_bridge.py",
            "linking_the_world_temp_ha_bridge_addon",
        )

        self.assertEqual(
            [path for path in retired_paths if (ROOT / path).exists()],
            [],
        )

    def test_documentation_no_longer_describes_legacy_bridge_installation(self) -> None:
        """Public documentation must advertise the sole supported delivery path."""
        for path in (ROOT / "README.md", ROOT / "docs/TROUBLESHOOTING.md"):
            self.assertNotIn("旧 Bridge", path.read_text(encoding="utf-8"))

    def test_ci_gates_tagged_releases_on_release_metadata(self) -> None:
        """Tag builds must compare the tag with the three release documents."""
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

        self.assertIn("tags: ['v*']", workflow)
        self.assertIn("scripts/release_metadata.py --tag", workflow)
        self.assertNotIn("docker build", workflow)

    def test_public_repository_urls_use_integration_name(self) -> None:
        """Every maintained public link must use the renamed repository."""
        text_suffixes = {".json", ".md", ".py", ".yaml", ".yml"}
        old_url_locations: list[str] = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or path.suffix not in text_suffixes:
                continue
            if any(part in {".git", ".venv", "__pycache__"} for part in path.parts):
                continue
            if OLD_REPOSITORY_URL in path.read_text(encoding="utf-8"):
                old_url_locations.append(str(path.relative_to(ROOT)))

        self.assertEqual(old_url_locations, [])

        manifest = json.loads(
            (
                ROOT
                / "custom_components/linking_the_world_temp_ha/manifest.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["documentation"], NEW_REPOSITORY_URL)
        self.assertEqual(
            manifest["issue_tracker"], f"{NEW_REPOSITORY_URL}/issues"
        )

    def test_readme_is_task_oriented_and_points_to_detailed_guides(self) -> None:
        """The README must lead a new household from install to support."""
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        for heading in (
            "## 快速开始",
            "## 支持的设备与功能",
            "## 配置集成",
            "## 设备与实体",
            "## 控制规则",
            "## 故障提醒与 Repairs",
            "## HomeKit",
            "## 升级与卸载",
            "## 获取帮助",
        ):
            self.assertIn(heading, readme)
        self.assertIn(NEW_REPOSITORY_URL, readme)
        self.assertIn("docs/TROUBLESHOOTING.md", readme)
        self.assertIn("docs/PRIVACY.md", readme)

    def test_system_environment_names_do_not_claim_controller_measurements(self) -> None:
        """Temperature and humidity come from field sensors, not the MC7021 itself."""
        for relative_path in (
            "custom_components/linking_the_world_temp_ha/strings.json",
            "custom_components/linking_the_world_temp_ha/translations/zh-Hans.json",
        ):
            document = json.loads((ROOT / relative_path).read_text(encoding="utf-8"))
            sensors = document["entity"]["sensor"]
            self.assertEqual(sensors["system_temperature"]["name"], "温度")
            self.assertEqual(sensors["system_humidity"]["name"], "湿度")

    def test_fault_names_follow_official_terminology(self) -> None:
        """Fault sensors and Repairs must use the two official fault names."""
        for relative_path in (
            "custom_components/linking_the_world_temp_ha/strings.json",
            "custom_components/linking_the_world_temp_ha/translations/zh-Hans.json",
        ):
            document = json.loads((ROOT / relative_path).read_text(encoding="utf-8"))
            sensors = document["entity"]["sensor"]
            issues = document["issues"]
            self.assertEqual(
                sensors["system_fault_code"]["name"], "主机故障原始码"
            )
            self.assertEqual(
                sensors["filter_fault_code"]["name"], "新风滤网故障原始码"
            )
            self.assertEqual(issues["system_fault"]["title"], "主机故障")
            self.assertEqual(issues["filter_fault"]["title"], "新风滤网故障")

        english = json.loads(
            (
                ROOT
                / "custom_components/linking_the_world_temp_ha/translations/en.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            english["entity"]["sensor"]["system_fault_code"]["name"],
            "Controller fault raw code",
        )
        self.assertEqual(
            english["entity"]["sensor"]["filter_fault_code"]["name"],
            "Fresh-air filter fault raw code",
        )
        self.assertEqual(english["issues"]["system_fault"]["title"], "Controller fault")
        self.assertEqual(
            english["issues"]["filter_fault"]["title"],
            "Fresh-air filter fault",
        )

    def test_sensor_units_use_current_home_assistant_enums(self) -> None:
        """HA 2026.7+ concentration units must not emit deprecation warnings."""
        sensor_source = (
            ROOT / "custom_components/linking_the_world_temp_ha/sensor.py"
        ).read_text(encoding="utf-8")

        self.assertIn("UnitOfDensity.MICROGRAMS_PER_CUBIC_METER", sensor_source)
        self.assertIn("UnitOfRatio.PARTS_PER_MILLION", sensor_source)
        self.assertNotIn("CONCENTRATION_MICROGRAMS_PER_CUBIC_METER", sensor_source)
        self.assertNotIn("CONCENTRATION_PARTS_PER_MILLION", sensor_source)
