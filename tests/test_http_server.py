import json
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from roboscientist.web.server import create_server


class _FailingQwen:
    def status(self):
        return {
            "configured": True,
            "provider": "aliyun_model_studio",
            "model": "fixture-qwen",
        }

    def complete_structured(self, **kwargs):
        del kwargs
        raise RuntimeError("sensitive-fixture-value")


class HttpServerTests(unittest.TestCase):
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

    def start_server(self, qwen_client=None):
        self.server = create_server(
            "127.0.0.1",
            0,
            self.temporary.name,
            qwen_client=qwen_client,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    @staticmethod
    def post(url, payload):
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def test_real_http_campaign_and_retrieval_contract(self):
        base = self.start_server()
        status, campaign = self.post(
            f"{base}/api/campaigns",
            {
                "task_text": "把红色方块放到右侧目标区域",
                "scenario": "pose_offset",
                "mode": "mock",
                "use_qwen": False,
                "auto_run_p1": True,
            },
        )
        self.assertEqual(status, 201)
        campaign_id = campaign["campaign_id"]
        with urlopen(f"{base}/api/campaigns/{campaign_id}", timeout=5) as response:
            stored = json.loads(response.read().decode("utf-8"))
        self.assertEqual(stored["campaign_id"], campaign_id)
        self.assertEqual(stored["rounds_completed"], 2)

    def test_non_object_json_returns_structured_400(self):
        base = self.start_server()
        status, payload = self.post(f"{base}/api/campaigns", [])
        self.assertEqual(status, 400)
        self.assertIn("JSON object", payload["error"]["message"])

    def test_unexpected_qwen_error_returns_structured_502_without_secret(self):
        base = self.start_server(_FailingQwen())
        status, payload = self.post(
            f"{base}/api/campaigns",
            {
                "task_text": "把红色方块放到右侧目标区域",
                "scenario": "pose_offset",
                "mode": "mock",
                "use_qwen": True,
            },
        )
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(status, 502)
        self.assertEqual(payload["error"]["code"], "QWEN_PLANNING_FAILED")
        self.assertTrue(payload["error"]["campaign_id"].startswith("campaign-"))
        self.assertNotIn("sensitive-fixture-value", encoded)

    def test_unexpected_application_error_returns_structured_500_without_secret(self):
        base = self.start_server()

        def fail(*args, **kwargs):
            del args, kwargs
            raise RuntimeError("sensitive-fixture-value")

        self.server.RequestHandlerClass.application.run_campaign = fail
        status, payload = self.post(
            f"{base}/api/campaigns",
            {
                "task_text": "把红色方块放到右侧目标区域",
                "scenario": "pose_offset",
                "mode": "mock",
                "use_qwen": False,
            },
        )
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(status, 500)
        self.assertEqual(payload["error"]["code"], "INTERNAL_SERVER_ERROR")
        self.assertTrue(payload["error"]["request_id"].startswith("request-"))
        self.assertNotIn("sensitive-fixture-value", encoded)

    def test_stop_route_rejects_non_real_arm_mode(self):
        base = self.start_server()
        status, payload = self.post(
            f"{base}/api/stop",
            {"mode": "mock", "reason": "operator_test"},
        )
        self.assertEqual(status, 400)
        self.assertIn("only supports mode=real_arm", payload["error"]["message"])

    def test_stop_route_allows_empty_body_and_defaults_to_real_arm(self):
        base = self.start_server()
        request = Request(f"{base}/api/stop", method="POST")
        with urlopen(request, timeout=5) as response:
            status = response.status
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(status, 200)
        self.assertEqual(payload["mode"], "real_arm")
        self.assertEqual(payload["stop"]["action"], "stop")


if __name__ == "__main__":
    unittest.main()
