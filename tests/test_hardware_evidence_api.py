import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from roboscientist.web.server import DemoApplication, create_server


class HardwareEvidenceApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.server = None
        self.thread = None

    def tearDown(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2)
        self.temporary.cleanup()

    def start_server(self, evidence_source=None):
        self.server = create_server(
            "127.0.0.1",
            0,
            self.temporary.name,
            hardware_evidence_source=evidence_source,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        return "http://{}:{}".format(host, port)

    @staticmethod
    def get(url):
        try:
            with urlopen(url, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def test_unconfigured_source_is_explicitly_unavailable(self):
        payload = DemoApplication(self.temporary.name).hardware_evidence()

        self.assertEqual(payload["status"], "unavailable")
        self.assertEqual(payload["reason_code"], "HARDWARE_EVIDENCE_SOURCE_NOT_CONFIGURED")
        self.assertEqual(payload["data_source"], "hardware_evidence_package")
        self.assertTrue(payload["read_only"])
        self.assertFalse(payload["motion_requested"])
        self.assertIsNone(payload["validation"])

    def test_configured_source_is_validated_read_only(self):
        source = Path(self.temporary.name) / "delivery"
        source.mkdir()
        (source / "README.txt").write_text("P0 success is documented only\n", encoding="utf-8")

        application = DemoApplication(
            self.temporary.name,
            hardware_evidence_source=source,
        )
        payload = application.hardware_evidence()

        self.assertEqual(payload["status"], "incomplete")
        self.assertEqual(payload["reason_code"], "HARDWARE_EVIDENCE_INCOMPLETE")
        self.assertTrue(payload["source_configured"])
        self.assertEqual(payload["source_name"], "delivery")
        self.assertEqual(payload["package_type"], "directory")
        self.assertIn("README.txt", payload["files_checked"])
        self.assertEqual(list(source.iterdir())[0].name, "README.txt")

    def test_get_route_exposes_report_and_post_cannot_choose_a_path(self):
        source = Path(self.temporary.name) / "delivery"
        source.mkdir()
        (source / "README.txt").write_text("source-only notes\n", encoding="utf-8")
        base = self.start_server(source)

        status, payload = self.get(base + "/api/hardware/evidence")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "incomplete")
        self.assertEqual(payload["validation"]["package_type"], "directory")
        self.assertTrue(payload["read_only"])
        self.assertFalse(payload["motion_requested"])

        request = Request(
            base + "/api/hardware/evidence",
            data=json.dumps({"path": "/etc/passwd"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as context:
            urlopen(request, timeout=5)
        self.assertEqual(context.exception.code, 404)

    def test_config_reports_source_name_without_parameters_or_path_override(self):
        source = Path(self.temporary.name) / "delivery.tar.gz"
        source.write_bytes(b"not-a-tar")
        config = DemoApplication(
            self.temporary.name,
            hardware_evidence_source=source,
        ).config()
        summary = config["hardware_evidence"]

        self.assertTrue(summary["configured"])
        self.assertEqual(summary["source_name"], "delivery.tar.gz")
        self.assertEqual(summary["validator"], "evidence_package_validator")
        self.assertTrue(summary["read_only"])
        self.assertFalse(summary["motion_requested"])


if __name__ == "__main__":
    unittest.main()
