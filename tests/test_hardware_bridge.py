"""Contract tests for the hardware bridge; no physical robot is required."""

import unittest

from roboscientist.hardware_bridge.server import (
    BridgeConfigurationError,
    BridgeService,
    create_server,
)


class RecordingBackend:
    def __init__(self):
        self.requests = []

    def health(self):
        return {"available": True, "backend": "recording"}

    def state(self):
        return {"state": "idle"}

    def preflight(self, request):
        self.requests.append(("preflight", dict(request)))
        return {"approved": request.get("safe", False), "checks": ["workspace"]}

    def execute_pick_place(self, request):
        self.requests.append(("execute_pick_place", dict(request)))
        return {"execution_id": "real-test-1", "outcome": "completed"}

    def stop(self, request):
        self.requests.append(("stop", dict(request)))
        return {"stopped": True}


class HardwareBridgeTests(unittest.TestCase):
    def test_dry_run_is_explicit_and_executes_no_real_motion(self):
        service = BridgeService()
        status, health = service.health()
        self.assertEqual(status, 200)
        self.assertEqual(health["mode"], "dry_run")
        self.assertFalse(health["data"]["motion_enabled"])

        status, result = service.execute_pick_place({"task_id": "exp-1"})
        self.assertEqual(status, 200)
        self.assertEqual(result["data"]["outcome"], "synthetic_success")
        self.assertTrue(result["data"]["synthetic"])

    def test_real_backend_requires_preflight_before_execution(self):
        backend = RecordingBackend()
        service = BridgeService(allow_real_motion=True, backend=backend)

        status, rejected = service.execute_pick_place({"safe": False})
        self.assertEqual(status, 409)
        self.assertEqual(rejected["error"]["code"], "PREFLIGHT_REJECTED")
        self.assertEqual([name for name, _ in backend.requests], ["preflight"])

        status, result = service.execute_pick_place({"safe": True, "task_id": "exp-2"})
        self.assertEqual(status, 200)
        self.assertEqual(result["mode"], "real_motion")
        self.assertEqual(result["data"]["execution_id"], "real-test-1")
        self.assertEqual([name for name, _ in backend.requests], ["preflight", "preflight", "execute_pick_place"])

    def test_real_motion_cannot_start_without_backend(self):
        with self.assertRaises(BridgeConfigurationError):
            BridgeService(allow_real_motion=True)

    def test_concurrent_motion_operation_is_rejected_but_stop_remains_available(self):
        backend = RecordingBackend()
        service = BridgeService(allow_real_motion=True, backend=backend)
        service._motion_lock.acquire()
        try:
            status, result = service.execute_pick_place({"safe": True})
            stop_status, stop_result = service.stop({"reason": "operator_request"})
        finally:
            service._motion_lock.release()

        self.assertEqual(status, 409)
        self.assertEqual(result["error"]["code"], "ROBOT_BUSY")
        self.assertEqual(stop_status, 200)
        self.assertTrue(stop_result["data"]["stopped"])


if __name__ == "__main__":
    unittest.main()
