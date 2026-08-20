import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from armpi_parameterized_runner import run_experiment, validate_parameters


PARAMETERS = {
    "grasp_offset_m": [-0.01, 0.0, 0.0],
    "approach_height_m": 0.03,
    "transit_height_m": 0.12,
    "speed_m_s": 0.1,
}


class ArmPiParameterizedRunnerTests(unittest.TestCase):
    def test_rejects_parameters_outside_fixed_allow_list(self):
        invalid = dict(PARAMETERS)
        invalid["grasp_offset_m"] = [0.03, 0.0, 0.0]
        with self.assertRaises(ValueError):
            validate_parameters(invalid)

    def test_wrapper_must_acknowledge_exact_parameter_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wrapper = root / "parameterized.sh"
            wrapper.write_text(
                "#!/bin/sh\n"
                "echo \"STATUS: skill_parameters_applied sha256=$5\"\n"
                "if [ \"$1\" = check ]; then\n"
                "  echo 'STATUS: numeric_safety_passed'\n"
                "  echo 'STATUS: preflight_passed'\n"
                "else\n"
                "  echo 'STATUS: pick_place_sequence_completed'\n"
                "fi\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ, {"ARMPI_PARAMETERIZED_WRAPPER": str(wrapper)}, clear=True
            ):
                result = run_experiment(
                    "check",
                    {"skill": {"parameters": PARAMETERS}},
                    {"run_dir": str(root / "run"), "task_dir": str(root), "timeout_s": 5},
                )
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["executed_parameters"], PARAMETERS)
        self.assertIn("numeric_safety_passed", result["output"])

    def test_missing_acknowledgement_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wrapper = root / "bad.sh"
            wrapper.write_text("#!/bin/sh\necho 'STATUS: preflight_passed'\n", encoding="utf-8")
            with patch.dict(
                os.environ, {"ARMPI_PARAMETERIZED_WRAPPER": str(wrapper)}, clear=True
            ):
                result = run_experiment(
                    "check",
                    {"skill": {"parameters": PARAMETERS}},
                    {"run_dir": str(root / "run"), "task_dir": str(root), "timeout_s": 5},
                )
        self.assertNotEqual(result["returncode"], 0)


if __name__ == "__main__":
    unittest.main()
