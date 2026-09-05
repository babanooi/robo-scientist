import tempfile
import unittest

from roboscientist.web.server import DemoApplication


class PublicVirtualRuntimeTests(unittest.TestCase):
    def test_public_simulation_runtime_is_explicitly_software_only(self):
        with tempfile.TemporaryDirectory() as directory:
            application = DemoApplication(directory)
            runtime = application.runtime("simulation")

            self.assertTrue(runtime["available"])
            self.assertEqual(runtime["hardware_status"], "simulation_runtime_verified")
            self.assertEqual(runtime["runtime_name"], "roboscientist.virtual_workcell")
            self.assertEqual(runtime["engine"], "python_deterministic")
            self.assertFalse(runtime["physical_robot_connected"])
            self.assertEqual(runtime["motion_state"], "idle")


if __name__ == "__main__":
    unittest.main()
