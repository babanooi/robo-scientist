import unittest

import tempfile

from roboscientist.adapters import ArmPiAdapterStub, SimulationAdapter
from roboscientist.core.orchestrator import Orchestrator
from roboscientist.core.planner import build_plan
from roboscientist.core.task_parser import parse_task
from roboscientist.schemas import ErrorCode, RunStatus, SafetyConstraints, SkillVersion
from roboscientist.storage import ExperimentStore


class AdapterContractTests(unittest.TestCase):
    def test_armpi_stub_explicitly_blocks_unverified_hardware(self):
        adapter = ArmPiAdapterStub()
        plan = build_plan(
            parse_task("把红色方块放到目标区域"),
            SkillVersion(version="p0"),
            adapter.name,
            SafetyConstraints(),
        )
        preflight = adapter.preflight(plan)
        self.assertEqual(preflight.status, RunStatus.REJECTED)
        self.assertEqual(preflight.error_code, ErrorCode.HARDWARE_UNVERIFIED)

    def test_simulation_adapter_reports_unverified_runtime_without_fake_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            result = Orchestrator(
                SimulationAdapter(), ExperimentStore(directory)
            ).run("把红色方块放到目标区域", SkillVersion(version="p0"))
        self.assertEqual(result.data_source, "simulation")
        self.assertEqual(result.hardware_status, "simulation_runtime_unverified")
        self.assertEqual(result.scene_id, "simulation-virtual-workcell-v0")
        self.assertEqual(result.status, RunStatus.REJECTED)
        self.assertFalse(result.simulation.planning_success)
        self.assertFalse(result.simulation.execution_success)
        self.assertIsNone(result.simulation.collision_detected)
        self.assertIsNone(result.candidate_skill_version)
