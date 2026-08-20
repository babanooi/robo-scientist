"""Run one deterministic Mock experiment from the command line."""

import argparse
import os
from pathlib import Path

from roboscientist.adapters import (
    ArmPiAdapterStub, MockAdapter, MockScenario, RealArmAdapter, SimulationAdapter,
    load_real_arm_profile,
)
from roboscientist.core.orchestrator import Orchestrator
from roboscientist.schemas import SkillVersion
from roboscientist.storage import ExperimentStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a RoboScientist Mock S0/S1 experiment")
    parser.add_argument("--scenario", choices=[scenario.value for scenario in MockScenario], default="success")
    parser.add_argument("--mode", choices=["mock", "simulation", "real_arm"], default="mock")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--real-arm-profile", help="approved real-arm profile JSON")
    parser.add_argument("--enable-real-arm", action="store_true", help="sets this process's required real-motion gate")
    args = parser.parse_args()
    skill = SkillVersion(version="p0")
    if args.enable_real_arm:
        os.environ["ROBO_ALLOW_REAL_ARM"] = "1"
    real_adapter = (
        RealArmAdapter(load_real_arm_profile(args.real_arm_profile))
        if args.real_arm_profile else ArmPiAdapterStub()
    )
    adapter = {
        "mock": MockAdapter(MockScenario(args.scenario)),
        "simulation": SimulationAdapter(),
        "real_arm": real_adapter,
    }[args.mode]
    profile = getattr(adapter, "profile", None)
    orchestrator = Orchestrator(
        adapter, ExperimentStore(Path(args.data_root)),
        constraints=profile.safety_constraints if profile else None,
        scene_id=profile.scene_id if profile else "mock-fixed-workbench-v0",
        target_pose=profile.target_pose if profile else None,
        destination_pose=profile.destination_pose if profile else None,
    )
    result = orchestrator.run("把红色方块放到右侧目标区域", skill)
    print(f"experiment_id={result.experiment_id}")
    print(f"status={result.status.value}")
    print(f"data_source={result.data_source}")
    print(f"hardware_status={result.hardware_status}")
    print(f"candidate_skill_version={result.candidate_skill_version or 'none'}")


if __name__ == "__main__":
    main()
