import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.generate_virtual_submission import (
    _copy_and_sanitize_json_tree,
    _refresh_virtual_artifact_manifests,
    _verify_virtual_artifact_manifests,
)


class VirtualSubmissionIntegrityTests(unittest.TestCase):
    def test_sanitized_virtual_artifact_manifests_are_refreshed_and_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            artifact = source / "virtual_artifacts" / "exp-test"
            artifact.mkdir(parents=True)
            payload_names = (
                "scene.json",
                "trajectory.json",
                "evaluation.json",
                "execution_trace.json",
                "replay.json",
            )
            original_digests = {}
            for name in payload_names:
                payload_path = artifact / name
                payload_path.write_text(
                    json.dumps(
                        {"name": name, "temporary_path": str(source)},
                        separators=(",", ":"),
                    ),
                    encoding="utf-8",
                )
                original_digests[name] = hashlib.sha256(
                    payload_path.read_bytes()
                ).hexdigest()
            (artifact / "manifest.json").write_text(
                json.dumps(
                    {
                        "format_version": "virtual-evidence-v1",
                        "synthetic": True,
                        "physical_robot_connected": False,
                        "files": original_digests,
                    }
                ),
                encoding="utf-8",
            )

            copied = Path(directory) / "copied"
            _copy_and_sanitize_json_tree(source, copied, source)
            self.assertEqual(
                _refresh_virtual_artifact_manifests(copied),
                1,
            )
            self.assertEqual(_verify_virtual_artifact_manifests(copied), 1)

            manifest = json.loads(
                (copied / "virtual_artifacts" / "exp-test" / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            copied_scene = copied / "virtual_artifacts" / "exp-test" / "scene.json"
            self.assertNotEqual(
                manifest["files"]["scene.json"], original_digests["scene.json"]
            )
            self.assertEqual(
                manifest["files"]["scene.json"],
                hashlib.sha256(copied_scene.read_bytes()).hexdigest(),
            )

            copied_scene.write_text('{"tampered":true}\n', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "virtual artifact digest mismatch"):
                _verify_virtual_artifact_manifests(copied)


if __name__ == "__main__":
    unittest.main()
