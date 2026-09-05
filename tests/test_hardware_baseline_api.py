import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

from roboscientist.web.server import DemoApplication, create_server


class HardwareBaselineApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.server = create_server("127.0.0.1", 0, self.temporary.name)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base = "http://{}:{}".format(host, port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def get(self, path):
        try:
            with urlopen(self.base + path, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def test_list_route_returns_reference_only_baselines(self):
        status, payload = self.get("/api/hardware/baselines")

        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "available")
        self.assertEqual(payload["data_source"], "documented_hardware_reference")
        self.assertTrue(payload["read_only"])
        self.assertFalse(payload["motion_requested"])
        self.assertFalse(payload["current_hardware_verified"])
        self.assertEqual(payload["baseline_count"], 2)
        self.assertEqual(
            payload["baseline_ids"], ["fixed_a_to_b_p0", "multishape_factory"]
        )
        self.assertEqual(
            [item["baseline_id"] for item in payload["baselines"]],
            payload["baseline_ids"],
        )

    def test_detail_route_exposes_nested_and_flat_reference_fields(self):
        status, payload = self.get("/api/hardware/baselines/fixed_a_to_b_p0")

        self.assertEqual(status, 200)
        self.assertEqual(payload["baseline_id"], "fixed_a_to_b_p0")
        self.assertEqual(payload["baseline"]["baseline_id"], payload["baseline_id"])
        self.assertEqual(payload["parameters"]["close_position_pulse"], 540)
        self.assertFalse(payload["current_hardware_verified"])

    def test_unknown_detail_route_is_not_found_without_motion(self):
        status, payload = self.get("/api/hardware/baselines/unknown")

        self.assertEqual(status, 404)
        self.assertIn("unknown hardware baseline", payload["error"]["message"])

    def test_config_contains_only_a_compact_baseline_summary(self):
        config = DemoApplication(self.temporary.name).config()
        summary = config["hardware_baselines"]

        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["ids"], ["fixed_a_to_b_p0", "multishape_factory"])
        self.assertNotIn("parameters", summary)
        self.assertTrue(summary["read_only"])
        self.assertFalse(summary["current_hardware_verified"])


if __name__ == "__main__":
    unittest.main()
