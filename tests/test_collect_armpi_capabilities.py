import json
import tempfile
import unittest
from pathlib import Path

from scripts.collect_armpi_capabilities import (
    _parse_typed_names,
    collect_capabilities,
)


class CollectArmPiCapabilitiesTests(unittest.TestCase):
    def test_collects_allowlisted_sources_without_executing_them(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_dir = root / "armpi_tasks"
            legacy_dir = root / "my_armpi"
            task_dir.mkdir()
            legacy_dir.mkdir()
            sentinel = root / "executed.txt"
            (task_dir / "run_final_dynamic_pick_place.sh").write_text(
                "#!/bin/sh\n"
                "echo 'STATUS: numeric_safety_passed'\n"
                "# check pick-place --parameters --parameters-sha256\n",
                encoding="utf-8",
            )
            (task_dir / "final_registered_pick_place.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(sentinel)!r}).write_text('executed')\n"
                "def execute(target, speed=0.1):\n"
                "    return target\n",
                encoding="utf-8",
            )
            (task_dir / "apply_final_tcp.py").write_text(
                "def apply_tcp(point):\n    return point\n", encoding="utf-8"
            )
            (task_dir / "final_dynamic_target.json").write_text(
                json.dumps({"target": {"x": 0.1, "valid": True}}), encoding="utf-8"
            )
            (legacy_dir / "move_to_pose.py").write_text(
                "def move_to_pose(pose, speed):\n    return True\n", encoding="utf-8"
            )
            output = root / "bundle"

            report = collect_capabilities(
                task_dir, legacy_dir, output, include_ros2=False
            )

            self.assertFalse(sentinel.exists())
            self.assertTrue(report["baseline_source_inventory_complete"])
            self.assertFalse(report["safety"]["target_code_executed"])
            self.assertEqual(len(report["artifacts"]), 5)
            copied_python = next(
                item for item in report["artifacts"]
                if item["role"] == "registered_pick_place_source"
            )
            self.assertEqual(copied_python["metadata"]["functions"][0]["name"], "execute")
            self.assertEqual(len(copied_python["sha256"]), 64)
            self.assertTrue((output / "manifest.sha256").is_file())
            saved = json.loads((output / "capability_report.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["schema_version"], "armpi_capability_report_v1")

    def test_reports_missing_baseline_sources_without_claiming_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_dir = root / "armpi_tasks"
            task_dir.mkdir()
            (task_dir / "run_final_dynamic_pick_place.sh").write_text(
                "#!/bin/sh\n", encoding="utf-8"
            )
            report = collect_capabilities(
                task_dir, root / "missing", root / "bundle", include_ros2=False
            )
        self.assertFalse(report["baseline_source_inventory_complete"])
        self.assertIn("dynamic_target_sample", report["missing_baseline_roles"])
        self.assertFalse(report["parameterized_wrapper_present"])

    def test_parses_ros2_typed_name_output(self):
        result = _parse_typed_names([
            "/camera/color/image_raw [sensor_msgs/msg/Image]",
            "/kinematics/get_current_pose [example_interfaces/srv/Trigger]",
        ])
        self.assertEqual(result[0]["name"], "/camera/color/image_raw")
        self.assertEqual(result[0]["types"], ["sensor_msgs/msg/Image"])


if __name__ == "__main__":
    unittest.main()
