import json
import tempfile
import unittest
from pathlib import Path
from urllib.request import Request, urlopen
import threading

from roboscientist.web.server import DemoApplication, create_server


TASK = "把红色方块放到右侧目标区域"


class RepeatedValidationTests(unittest.TestCase):
    def test_virtual_validation_proves_repeatable_improvement(self):
        with tempfile.TemporaryDirectory() as directory:
            app = DemoApplication(directory)
            campaign = app.run_validation(
                TASK, "pose_offset", mode="simulation", repeats=10, use_qwen=False
            )
            validation = campaign["validation"]
            self.assertEqual(validation["data_source"], "simulation")
            self.assertEqual(validation["scene_id"], "simulation-virtual-workcell-v0")
            self.assertEqual(
                campaign["records"][0]["plan"]["scene_id"],
                validation["scene_id"],
            )
            self.assertEqual(validation["repeats_per_version"], 10)
            self.assertEqual(validation["summary"]["p0"]["sample_count"], 10)
            self.assertEqual(validation["summary"]["p1"]["sample_count"], 10)
            self.assertTrue(validation["comparison"]["samples_sufficient"])
            self.assertTrue(validation["comparison"]["improvement_supported"])
            self.assertEqual(
                validation["comparison"]["decision"], "promote_candidate_for_review"
            )
            objective = validation["comparison"]["objective"]
            self.assertEqual(objective["primary_metric"], "task_success_rate")
            self.assertFalse(objective["efficiency_metrics_are_promotion_criteria"])
            self.assertIn("path_length_mean_m", validation["comparison"]["deltas"])
            path = Path(directory) / "campaigns" / campaign["campaign_id"] / "validation.json"
            self.assertTrue(path.is_file())
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["campaign_id"], campaign["campaign_id"])

    def test_insufficient_samples_never_promote_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            app = DemoApplication(directory)
            campaign = app.run_validation(
                TASK, "pose_offset", mode="simulation", repeats=2, use_qwen=False
            )
            comparison = campaign["validation"]["comparison"]
            self.assertFalse(comparison["samples_sufficient"])
            self.assertTrue(comparison["improvement_supported"])
            self.assertEqual(
                comparison["decision"], "candidate_only_insufficient_samples"
            )

    def test_http_validation_and_campaign_validation_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            server = create_server("127.0.0.1", 0, directory)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.shutdown)
            self.addCleanup(server.server_close)
            self.addCleanup(lambda: thread.join(timeout=2))
            host, port = server.server_address
            base = f"http://{host}:{port}"
            request = Request(
                f"{base}/api/validation",
                data=json.dumps(
                    {
                        "task_text": TASK,
                        "scenario": "pose_offset",
                        "mode": "simulation",
                        "repeats": 10,
                        "use_qwen": False,
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=10) as response:
                self.assertEqual(response.status, 201)
                payload = json.loads(response.read().decode("utf-8"))
            campaign_id = payload["campaign_id"]
            self.assertEqual(
                payload["validation"]["comparison"]["decision"],
                "promote_candidate_for_review",
            )
            with urlopen(f"{base}/api/campaigns/{campaign_id}/validation", timeout=10) as response:
                stored = json.loads(response.read().decode("utf-8"))
            self.assertEqual(stored["campaign_id"], campaign_id)
            self.assertEqual(stored["summary"]["p1"]["success_count"], 10)


if __name__ == "__main__":
    unittest.main()
