"""Deterministic fixed-workbench plan generator, independent of device details."""

from roboscientist.schemas import ExperimentPlan, ObjectPose, SafetyConstraints, SkillVersion, TaskSpec


def build_plan(
    task: TaskSpec,
    skill: SkillVersion,
    adapter_name: str,
    constraints: SafetyConstraints,
    scene_id: str = "mock-fixed-workbench-v0",
    confidence: float = 0.95,
    data_source: str = "mock",
) -> ExperimentPlan:
    resolved_scene_id = scene_id
    if scene_id == "mock-fixed-workbench-v0" and data_source == "simulation":
        resolved_scene_id = "simulation-virtual-workcell-v0"
    return ExperimentPlan(
        task=task,
        skill=skill,
        scene_id=resolved_scene_id,
        adapter=adapter_name,
        expected_data_source=data_source,
        target_pose=ObjectPose(
            x=0.22, y=0.0, z=0.03, confidence=confidence,
            calibration_version=(
                "mock-calibration-v0"
                if data_source == "mock"
                else "simulation-model-unverified"
                if data_source == "simulation"
                else None
            ),
        ),
        safety_constraints=constraints,
    )
