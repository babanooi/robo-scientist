"""Run one deterministic Mock experiment from the command line."""

import argparse
import os
from pathlib import Path

from roboscientist.adapters import (
    ArmPiAdapterStub,
    MockAdapter,
    MockScenario,
    RealArmAdapter,
    SimulationAdapter,
    VirtualScenario,
    VirtualSimulationAdapter,
    load_real_arm_profile,
)
from roboscientist.core.orchestrator import Orchestrator
from roboscientist.core.scientific_campaign import ScientificCampaignRunner
from roboscientist.schemas import SkillVersion
from roboscientist.storage import ExperimentStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a RoboScientist virtual experiment")
    parser.add_argument(
        "--scenario",
        choices=sorted({scenario.value for scenario in MockScenario} | {scenario.value for scenario in VirtualScenario}),
        default="pose_offset",
    )
    parser.add_argument("--mode", choices=["mock", "simulation", "real_arm"], default="simulation")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--real-arm-profile", help="approved real-arm profile JSON")
    parser.add_argument("--enable-real-arm", action="store_true", help="sets this process's required real-motion gate")
    parser.add_argument("--campaign", action="store_true", help="run bounded P0 -> feedback -> P1 campaign")
    parser.add_argument("--use-qwen", action=argparse.BooleanOptionalAction, default=False, help="use Qwen for campaign planning/feedback")
    parser.add_argument("--max-rounds", type=int, choices=(1, 2), default=2)
    args = parser.parse_args()
    skill = SkillVersion(version="p0")
    if args.enable_real_arm:
        os.environ["ROBO_ALLOW_REAL_ARM"] = "1"
    real_adapter = (
        RealArmAdapter(load_real_arm_profile(args.real_arm_profile))
        if args.real_arm_profile else ArmPiAdapterStub()
    )
    if args.mode == "mock":
        try:
            mock_scenario = MockScenario(args.scenario)
        except ValueError:
            mock_scenario = MockScenario.SUCCESS
        adapter = MockAdapter(mock_scenario)
    elif args.mode == "simulation":
        # Keep CLI runs auditable in the same way as the public Web mode: the
        # virtual workcell writes scene/trajectory/evaluation manifests under
        # the caller's data root instead of returning only virtual:// refs.
        adapter = VirtualSimulationAdapter(
            VirtualScenario(args.scenario),
            artifact_root=Path(args.data_root) / "virtual_artifacts",
        )
    else:
        adapter = real_adapter
    profile = getattr(adapter, "profile", None)
    orchestrator = Orchestrator(
        adapter, ExperimentStore(Path(args.data_root)),
        constraints=profile.safety_constraints if profile else None,
        scene_id=(
            profile.scene_id
            if profile
            else getattr(adapter, "scene_id", "mock-fixed-workbench-v0")
        ),
        target_pose=profile.target_pose if profile else None,
        destination_pose=profile.destination_pose if profile else None,
        execution_scenario=args.scenario,
    )
    task_text = "把红色方块放到右侧目标区域"
    if args.campaign:
        campaign = ScientificCampaignRunner(ExperimentStore(Path(args.data_root))).run(
            task_text,
            orchestrator,
            use_qwen=args.use_qwen,
            auto_run_p1=args.max_rounds == 2,
        )
        print(f"campaign_id={campaign['campaign_id']}")
        print(f"status={campaign['status']}")
        print(f"classification={campaign.get('classification')}")
        print(f"rounds_completed={len(campaign.get('records', []))}")
        return
    result = orchestrator.run(task_text, skill)
    print(f"experiment_id={result.experiment_id}")
    print(f"status={result.status.value}")
    print(f"data_source={result.data_source}")
    print(f"hardware_status={result.hardware_status}")
    print(f"candidate_skill_version={result.candidate_skill_version or 'none'}")


if __name__ == "__main__":
    main()
