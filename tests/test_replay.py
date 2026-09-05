import io
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from roboscientist.replay import (
    DATA_SOURCE,
    ReplayService,
    STATUS_AVAILABLE,
    STATUS_INVALID_DATA,
    STATUS_UNAVAILABLE,
)


def _joint_csv(rows=None, include_required=True):
    rows = rows or [
        [1_000_000_000, 1, 0, "1", "cuboid", "grasp", "joint1", "0.0", "nan", "nan"],
        [1_000_000_000, 1, 0, "1", "cuboid", "grasp", "joint2", "0.1", "nan", "nan"],
        [2_000_000_000, 2, 0, "1", "cuboid", "grasp", "joint1", "0.5", "0.2", "nan"],
        [2_000_000_000, 2, 0, "1", "cuboid", "grasp", "joint2", "0.3", "0.4", "nan"],
        [3_000_000_000, 3, 0, "1", "cuboid", "grasp", "joint1", "0.7", "0.2", "nan"],
        [3_000_000_000, 3, 0, "1", "cuboid", "grasp", "joint2", "0.4", "0.1", "nan"],
    ]
    header = (
        "wall_time_ns,ros_sec,ros_nsec,sample_id,shape,phase,joint_name,"
        "position_rad,velocity,effort\n"
    )
    if not include_required:
        header = "wall_time_ns,joint_name\n"
        rows = [[row[0], row[6]] for row in rows]
    return header + "".join(
        ",".join(str(value) for value in row) + "\n" for row in rows
    )


class ReplayServiceTests(unittest.TestCase):
    def _make_run(self, root: Path, run_id="run-001") -> Path:
        run = root / run_id
        run.mkdir(parents=True)
        (run / "joint_states_session.csv").write_text(_joint_csv(), encoding="utf-8")
        (run / "events.csv").write_text(
            "sample_id,shape,start_time_ns,end_time_ns,result\n"
            "1,cuboid,1000000000,3000000000,not_recorded\n",
            encoding="utf-8",
        )
        (run / "session_start_ns").write_text("1000000000\n", encoding="utf-8")
        (run / "session_end_ns").write_text("3000000000\n", encoding="utf-8")
        (run / "config").mkdir()
        (run / "config" / "calibration.yaml").write_text(
            "depth:\n  offset: [0.0, 0.0, 0.0]\n", encoding="utf-8"
        )
        (run / "grasp_events_session.log").write_text(
            "[3.0] FACTORY_FINAL_PICK_TARGET x=+0.2, y=-0.1, "
            "z=+0.03, shape=cuboid, yaw=12\n",
            encoding="utf-8",
        )
        return run

    def test_unconfigured_source_is_truthfully_unavailable(self):
        with patch.dict(os.environ, {}, clear=True):
            response = ReplayService().list_runs()
        self.assertEqual(response["status"], STATUS_UNAVAILABLE)
        self.assertEqual(response["data_source"], DATA_SOURCE)
        self.assertTrue(response["read_only"])
        self.assertFalse(response["motion_requested"])
        self.assertEqual(response["reason_code"], "REPLAY_SOURCE_NOT_FOUND")
        self.assertEqual(response["runs"], [])

    def test_missing_directory_is_unavailable(self):
        response = ReplayService("/tmp/path-that-should-not-exist-robo-replay").list_runs()
        self.assertEqual(response["status"], STATUS_UNAVAILABLE)
        self.assertEqual(response["reason_code"], "REPLAY_SOURCE_NOT_FOUND")

    def test_directory_run_is_parsed_with_bounded_trajectory_and_no_inferred_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._make_run(root)
            service = ReplayService(root, max_points=2)
            listed = service.list_runs()
            self.assertEqual(listed["status"], STATUS_AVAILABLE)
            self.assertEqual(listed["run_count"], 1)
            self.assertEqual(listed["runs"][0]["joint_state_file"], "joint_states_session.csv")

            response = service.get_run("run-001")
            self.assertEqual(response["status"], STATUS_AVAILABLE)
            self.assertEqual(response["data_source"], DATA_SOURCE)
            self.assertTrue(response["read_only"])
            self.assertFalse(response["motion_requested"])
            self.assertEqual(response["summary"]["joint_row_count"], 6)
            self.assertEqual(response["trajectory"]["original_points"], 3)
            self.assertEqual(response["trajectory"]["returned_points"], 2)
            self.assertTrue(response["trajectory"]["sampled"])
            self.assertEqual(len(response["trajectory"]["points"]), 2)
            self.assertEqual(response["trajectory"]["points"][0]["time_from_start_s"], 0.0)
            self.assertEqual(response["trajectory"]["points"][-1]["time_from_start_s"], 2.0)
            self.assertEqual(len(response["events"]), 1)
            self.assertEqual(response["vision_targets"][0]["shape"], "cuboid")
            self.assertTrue(response["calibration"]["sha256"])
            self.assertFalse(response["evidence"]["success_labels_available"])
            self.assertEqual(response["evidence"]["structured_event_rows"], 1)
            self.assertEqual(response["outcome"]["status"], STATUS_UNAVAILABLE)
            # A replay must not manufacture task outcome or success-rate claims.
            self.assertNotIn("success_rate", response)
            self.assertNotIn("object_grasped", response)
            self.assertTrue(any("只读回放" in item for item in response["limitations"]))

    def test_empty_events_are_reported_without_claiming_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = self._make_run(root)
            (run / "events.csv").write_text(
                "sample_id,shape,start_time_ns,end_time_ns,result\n", encoding="utf-8"
            )
            response = ReplayService(root).get_run("run-001")
            self.assertEqual(response["status"], STATUS_AVAILABLE)
            self.assertEqual(response["events"], [])
            self.assertEqual(response["evidence"]["event_file_status"], "empty")
            self.assertFalse(response["evidence"]["success_labels_available"])
            self.assertTrue(any("成功率无法" in item for item in response["limitations"]))

    def test_missing_joint_columns_is_invalid_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "bad-run"
            run.mkdir()
            (run / "joint_states_session.csv").write_text(
                _joint_csv(include_required=False), encoding="utf-8"
            )
            response = ReplayService(root).get_run("bad-run")
            self.assertEqual(response["status"], STATUS_INVALID_DATA)
            self.assertEqual(response["reason_code"], "REPLAY_JOINT_SCHEMA_MISSING")
            self.assertFalse(response["motion_requested"])

    def test_path_traversal_and_nested_run_ids_are_rejected(self):
        service = ReplayService("/tmp/does-not-matter")
        for run_id in ("../secret", "run/child", "run\\child", "", ".", ".."):
            response = service.get_run(run_id)
            self.assertEqual(response["status"], STATUS_INVALID_DATA)
            self.assertEqual(response["reason_code"], "REPLAY_INVALID_RUN_ID")

    def test_tar_archive_is_read_without_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = self._make_run(root, "archived-run")
            archive_path = root / "archived-run.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                for path in run.rglob("*"):
                    if path.is_file():
                        archive.add(path, arcname=path.relative_to(root).as_posix())
            response = ReplayService(archive_path, max_points=3).get_run("archived-run")
            self.assertEqual(response["status"], STATUS_AVAILABLE)
            self.assertEqual(response["source_kind"], "archive")
            self.assertEqual(response["summary"]["joint_row_count"], 6)
            self.assertEqual(response["trajectory"]["returned_points"], 3)
            self.assertFalse((root / "archived-run.tar.gz.extract").exists())

    def test_unsafe_tar_member_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "unsafe.tar.gz"
            payload = b"wall_time_ns,joint_name,position_rad\n1,joint1,0\n"
            with tarfile.open(archive_path, "w:gz") as archive:
                info = tarfile.TarInfo("../escape/joint_states_session.csv")
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            response = ReplayService(archive_path).list_runs()
            self.assertEqual(response["status"], STATUS_INVALID_DATA)
            self.assertEqual(response["reason_code"], "REPLAY_ARCHIVE_UNSAFE_PATH")

    def test_environment_variable_is_used_at_construction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._make_run(root)
            with patch.dict(os.environ, {"ROBO_REPLAY_ROOT": str(root)}, clear=True):
                response = ReplayService().list_runs()
            self.assertEqual(response["status"], STATUS_AVAILABLE)
            self.assertEqual(response["runs"][0]["run_id"], "run-001")


if __name__ == "__main__":
    unittest.main()
