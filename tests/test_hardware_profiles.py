import unittest

from roboscientist.hardware_profiles import (
    get_hardware_baseline,
    list_hardware_baselines,
)


class HardwareProfilesTests(unittest.TestCase):
    def test_lists_exactly_the_two_distinct_documented_baselines(self):
        baselines = list_hardware_baselines()

        self.assertEqual(
            [baseline.baseline_id for baseline in baselines],
            ["fixed_a_to_b_p0", "multishape_factory"],
        )
        self.assertNotEqual(
            baselines[0].parameters,
            baselines[1].parameters,
        )

    def test_fixed_baseline_contains_source_aligned_parameters_and_metadata(self):
        baseline = get_hardware_baseline("fixed_a_to_b_p0")

        self.assertEqual(baseline.parameters["point_a_m"], (0.22, 0.08, 0.10))
        self.assertEqual(baseline.parameters["point_b_m"], (0.22, -0.08, 0.10))
        self.assertEqual(baseline.parameters["pitch_deg"], 80.0)
        self.assertEqual(baseline.parameters["grasp_yaw_pulse"], 500)
        self.assertEqual(baseline.parameters["close_position_pulse"], 540)
        self.assertEqual(baseline.parameters["open_position_pulse"], 200)
        self.assertFalse(baseline.current_hardware_verified)
        self.assertIn("not", baseline.documented_claim.lower())
        self.assertTrue(baseline.source_notes)

    def test_multishape_baseline_contains_calibration_and_factory_parameters(self):
        baseline = get_hardware_baseline("multishape_factory")

        self.assertEqual(baseline.parameters["depth_scale"], (0.995, 1.03, 1.0))
        self.assertEqual(baseline.parameters["depth_offset_m"], (-0.047, -0.009, 0.002))
        self.assertEqual(baseline.parameters["kinematics_scale"], (1.0, 1.14, 1.0))
        self.assertEqual(baseline.parameters["pick_pitch_deg"], 85.0)
        self.assertEqual(baseline.parameters["close_position_pulse"], 570)
        self.assertEqual(baseline.parameters["hull_blend_alpha"], 0.25)
        self.assertEqual(
            baseline.parameters["right_bottom_x_correction_max_m"],
            0.003,
        )
        self.assertEqual(
            baseline.parameters["shape_to_action_group"]["cuboid"],
            "target_3",
        )
        self.assertFalse(baseline.current_hardware_verified)
        self.assertIn("not proof", baseline.documented_claim.lower())

    def test_get_rejects_unknown_id_and_returns_an_isolated_copy(self):
        with self.assertRaises(KeyError):
            get_hardware_baseline("does_not_exist")

        first = get_hardware_baseline("fixed_a_to_b_p0")
        first.parameters["point_a_m"] = (999.0, 999.0, 999.0)
        second = get_hardware_baseline("fixed_a_to_b_p0")
        self.assertEqual(second.parameters["point_a_m"], (0.22, 0.08, 0.10))

    def test_to_dict_exposes_metadata_without_mutating_the_baseline(self):
        baseline = get_hardware_baseline("multishape_factory")
        data = baseline.to_dict()

        self.assertEqual(data["baseline_id"], "multishape_factory")
        self.assertIsInstance(data["source_notes"], list)
        self.assertFalse(data["current_hardware_verified"])
        data["parameters"]["hull_blend_alpha"] = 99.0
        self.assertEqual(baseline.parameters["hull_blend_alpha"], 0.25)


if __name__ == "__main__":
    unittest.main()
