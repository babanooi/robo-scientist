"""Run one deterministic Mock experiment from the command line."""

import argparse
from pathlib import Path

from roboscientist.adapters import ArmPiAdapterStub, MockAdapter, MockScenario, SimulationAdapter
from roboscientist.core.orchestrator import Orchestrator
from roboscientist.schemas import SkillVersion
from roboscientist.storage import ExperimentStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a RoboScientist Mock S0/S1 experiment")
    parser.add_argument("--scenario", choices=[scenario.value for scenario in MockScenario], default="success")
    parser.add_argument("--mode", choices=["mock", "simulation", "real_arm"], default="mock")
    parser.add_argument("--data-root", default="data")
    args = parser.parse_args()
    skill = SkillVersion(version="p0")
    adapter = {
        "mock": MockAdapter(MockScenario(args.scenario)),
        "simulation": SimulationAdapter(),
        "real_arm": ArmPiAdapterStub(),
    }[args.mode]
    orchestrator = Orchestrator(adapter, ExperimentStore(Path(args.data_root)))
    result = orchestrator.run("把红色方块放到右侧目标区域", skill)
    print(f"experiment_id={result.experiment_id}")
    print(f"status={result.status.value}")
    print(f"data_source={result.data_source}")
    print(f"hardware_status={result.hardware_status}")
    print(f"candidate_skill_version={result.candidate_skill_version or 'none'}")


if __name__ == "__main__":
    main()
