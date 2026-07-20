import tempfile
import unittest

from roboscientist.adapters import MockAdapter
from roboscientist.core.orchestrator import Orchestrator
from roboscientist.core.planner import build_plan
from roboscientist.core.safety import check_plan
from roboscientist.core.task_parser import parse_task
from roboscientist.schemas import ErrorCode, SafetyConstraints, SkillVersion
from roboscientist.storage import ExperimentStore


class SafetyTests(unittest.TestCase):
    def test_low_confidence_is_rejected(self):
        constraints = SafetyConstraints()
        plan = build_plan(parse_task("把红色方块放到目标区域"), SkillVersion(version="p0"), "mock", constraints, confidence=0.2)
        check = check_plan(plan, "mock")
        self.assertFalse(check.allowed)
        self.assertIn(ErrorCode.LOW_CONFIDENCE, check.errors)

    def test_out_of_workspace_is_rejected(self):
        constraints = SafetyConstraints()
        plan = build_plan(parse_task("把红色方块放到目标区域"), SkillVersion(version="p0"), "mock", constraints)
        plan.target_pose.x = 1.0
        check = check_plan(plan, "mock")
        self.assertFalse(check.allowed)
        self.assertIn(ErrorCode.OUT_OF_WORKSPACE, check.errors)

    def test_rejected_plan_is_persisted_without_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            result = Orchestrator(MockAdapter(), ExperimentStore(directory)).run(
                "把红色方块放到目标区域", SkillVersion(version="p0"), confidence=0.1
            )
        self.assertEqual(result.failure.code, ErrorCode.LOW_CONFIDENCE)
        self.assertFalse(result.safety_check.allowed)

    def test_mismatched_data_source_is_rejected(self):
        constraints = SafetyConstraints(allow_real_robot=True)
        plan = build_plan(
            parse_task("把红色方块放到目标区域"),
            SkillVersion(version="p0"),
            "mock",
            constraints,
        )
        plan.expected_data_source = "hardware_unverified"
        check = check_plan(plan, "mock")
        self.assertFalse(check.allowed)
        self.assertIn(ErrorCode.INVALID_PARAMETERS, check.errors)
