import json
import tempfile
import unittest
from pathlib import Path

from roboscientist.adapters import MockAdapter, MockScenario
from roboscientist.core.orchestrator import Orchestrator
from roboscientist.core.planner import build_plan
from roboscientist.core.task_parser import parse_task
from roboscientist.schemas import RunStatus, SafetyConstraints, SkillVersion
from roboscientist.storage import ExperimentStore


class MockClosedLoopTests(unittest.TestCase):
    def run_scenario(self, scenario):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = ExperimentStore(temporary.name)
        result = Orchestrator(MockAdapter(scenario), store).run("把红色方块放到右侧目标区域", SkillVersion(version="p0"))
        return result, Path(temporary.name)

    def test_text_task_generates_task_spec(self):
        task = parse_task("把红色方块放到右侧目标区域")
        self.assertEqual(task.target_color, "red")
        self.assertEqual(task.target_zone, "right")

    def test_plan_has_required_s0_fields(self):
        task = parse_task("把红色方块放到目标区域")
        plan = build_plan(task, SkillVersion(version="p0"), "mock", SafetyConstraints())
        self.assertTrue(plan.experiment_id)
        self.assertEqual(plan.adapter, "mock")
        self.assertEqual(plan.expected_data_source, "mock")
        self.assertEqual(plan.skill.version, "p0")

    def test_success_writes_complete_mock_evidence(self):
        result, root = self.run_scenario(MockScenario.SUCCESS)
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual(result.data_source, "mock")
        package = root / "experiments" / result.experiment_id
        self.assertTrue((package / "plan.json").exists())
        self.assertTrue((root / "skills" / "p0.json").exists())
        payload = json.loads((package / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["data_source"], "mock")
        self.assertTrue(payload["safety_check"]["allowed"])
        self.assertTrue(payload["outcome"]["object_placed"])

    def test_pose_offset_generates_single_family_candidate(self):
        result, root = self.run_scenario(MockScenario.POSE_OFFSET)
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertIsNotNone(result.failure_analysis)
        self.assertEqual(result.failure_analysis.recommended_parameter_family, "grasp_offset")
        candidate = json.loads((root / "skills" / f"{result.candidate_skill_version}.json").read_text(encoding="utf-8"))
        self.assertEqual(candidate["changed_parameter_family"], "grasp_offset")

    def test_grasp_failure_is_classified_and_gets_candidate(self):
        result, _ = self.run_scenario(MockScenario.GRASP_FAILED)
        self.assertEqual(result.failure.code.value, "GRASP_FAILED")
        self.assertIsNotNone(result.candidate_skill_version)

    def test_timeout_never_generates_candidate(self):
        result, _ = self.run_scenario(MockScenario.TIMEOUT)
        self.assertEqual(result.status, RunStatus.TIMED_OUT)
        self.assertIsNone(result.candidate_skill_version)
