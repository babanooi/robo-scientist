import csv
import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from roboscientist.web.server import DemoApplication


def _tar_bytes(name: str, content: bytes) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        info = tarfile.TarInfo(name)
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    return stream.getvalue()


def _joint_csv() -> bytes:
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=["wall_time_ns", "ros_sec", "ros_nsec", "sample_id", "shape", "phase", "joint_name", "position_rad", "velocity", "effort"],
    )
    writer.writeheader()
    for timestamp, position in ((1_000_000_000, 0.0), (1_100_000_000, 0.2)):
        for joint in ("joint1", "joint2"):
            writer.writerow({
                "wall_time_ns": timestamp,
                "ros_sec": 1,
                "ros_nsec": timestamp - 1_000_000_000,
                "sample_id": 1,
                "shape": "cuboid",
                "phase": "grasp",
                "joint_name": joint,
                "position_rad": position,
                "velocity": "nan",
                "effort": "nan",
            })
    return output.getvalue().encode()


class ReplayApiTests(unittest.TestCase):
    def test_unconfigured_replay_is_explicitly_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = DemoApplication(directory).replay()
        self.assertEqual(payload["status"], "unavailable")
        self.assertEqual(payload["data_source"], "historical_real_arm")
        self.assertTrue(payload["read_only"])
        self.assertFalse(payload["motion_requested"])

    def test_archive_replay_exposes_trajectory_but_not_success_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "run.tar.gz"
            archive_path.write_bytes(_tar_bytes(
                "grasp_run/joint_states_session.csv", _joint_csv()
            ))
            payload = DemoApplication(directory, replay_source=archive_path).replay()
            self.assertEqual(payload["status"], "available")
            run_id = payload["runs"][0]["run_id"]
            run = DemoApplication(directory, replay_source=archive_path).replay(run_id)
        self.assertEqual(run["data_source"], "historical_real_arm")
        self.assertTrue(run["read_only"])
        self.assertFalse(run["motion_requested"])
        self.assertEqual(run["evidence"]["success_labels_available"], False)
        self.assertGreater(run["trajectory"]["returned_points"], 0)
        self.assertTrue(any("成功率" in item for item in run["limitations"]))


if __name__ == "__main__":
    unittest.main()
