import json
import tempfile
import unittest

from roboscientist.web.server import MockApplication, STATIC_DIR


class WebApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.application = MockApplication(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_task_submission_get_and_iterate(self):
        record = self.application.run_task(
            "把红色方块放到右侧目标区域", "pose_offset"
        )
        self.assertEqual(record["result"]["data_source"], "mock")
        self.assertTrue(record["result"]["safety_check"]["allowed"])
        self.assertIsNotNone(record["analysis"])
        experiment_id = record["result"]["experiment_id"]

        stored = self.application.experiment(experiment_id)
        self.assertEqual(stored["plan"]["experiment_id"], experiment_id)

        next_record = self.application.iterate(experiment_id, "success")
        self.assertEqual(next_record["result"]["status"], "succeeded")

    def test_skills_and_page_are_available(self):
        self.application.run_task(
            "把红色方块放到目标区域", "grasp_failed"
        )
        self.assertGreaterEqual(len(self.application.store.list_skills()), 2)
        page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn("Gazebo + MoveIt2", page)

    def test_simulation_mode_returns_contract_data_not_mock_data(self):
        record = self.application.run_task(
            "把红色方块放到目标区域", "success", mode="simulation"
        )
        result = record["result"]
        self.assertEqual(result["data_source"], "simulation")
        self.assertEqual(result["hardware_status"], "simulation_runtime_unverified")
        self.assertEqual(result["scene_id"], "simulation-virtual-workcell-v0")
        self.assertFalse(result["simulation"]["planning_success"])
        self.assertEqual(record["execution_mode"], "simulation")
