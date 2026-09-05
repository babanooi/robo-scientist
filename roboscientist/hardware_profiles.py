"""Documented ArmPi hardware baselines.

The values in this module are traceable starting points extracted from the two
hardware delivery archives.  They are deliberately kept separate from the
real-arm execution profile: a documented value is not an authorization to move
the robot, and neither baseline is evidence of the *current* robot state.
"""

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple


@dataclass(frozen=True)
class HardwareBaseline:
    """A named, documented hardware parameter snapshot.

    ``parameters`` is intentionally an unopinionated mapping because the two
    source archives describe different control flows.  Consumers must still
    perform their own profile, hash, preflight, stop, and runtime checks before
    using any value for a real execution.
    """

    baseline_id: str
    parameters: Dict[str, Any]
    evidence_level: str
    documented_claim: str
    source_notes: Tuple[str, ...]
    current_hardware_verified: bool = False

    @property
    def id(self) -> str:
        """Short alias for callers that use ``baseline.id``."""

        return self.baseline_id

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-friendly copy without exposing mutable internals."""

        return {
            "baseline_id": self.baseline_id,
            "parameters": deepcopy(self.parameters),
            "evidence_level": self.evidence_level,
            "documented_claim": self.documented_claim,
            "source_notes": list(self.source_notes),
            "current_hardware_verified": self.current_hardware_verified,
        }


_FIXED_A_TO_B_P0 = HardwareBaseline(
    baseline_id="fixed_a_to_b_p0",
    parameters={
        "point_a_m": (0.22, 0.08, 0.10),
        "point_b_m": (0.22, -0.08, 0.10),
        "pitch_deg": 80.0,
        "grasp_yaw_pulse": 500,
        "close_position_pulse": 540,
        "open_position_pulse": 200,
        "pick_z_offset_m": 0.02,
        "service_timeout_s": 20.0,
        "home_servo_pulses": (
            (6, 500),
            (5, 600),
            (4, 825),
            (3, 110),
            (2, 500),
            (1, 200),
        ),
    },
    evidence_level="documented_success_claim_without_current_run_artifacts",
    documented_claim=(
        "The Tasks 01-10 delivery materials document this fixed A-to-B flow "
        "as the P0 control baseline and describe a successful P0/P1 workflow. "
        "The archive does not establish that these values are the current "
        "robot configuration or a freshly verified run."
    ),
    source_notes=(
        "ArmPi_Ultra_Tasks_01_10_Full_Source_Downloadable.zip: "
        "source/pick_a_to_b.py defaults and HOME_POSITION.",
        "ArmPi_Ultra_Tasks_01_10_Detailed_CN_Downloadable.docx: "
        "Tasks 2, 7-10 describe the P0 baseline and success claim.",
        "No paired raw current-run evaluation, stage images, or runtime log "
        "is included here; treat as reference-only until revalidated on the robot.",
    ),
)


_MULTISHAPE_FACTORY = HardwareBaseline(
    baseline_id="multishape_factory",
    parameters={
        "depth_scale": (0.995, 1.03, 1.0),
        "depth_offset_m": (-0.047, -0.009, 0.002),
        "kinematics_scale": (1.0, 1.14, 1.0),
        "kinematics_offset_m": (0.02, 0.002, -0.008),
        "pick_pitch_deg": 85.0,
        "close_position_pulse": 570,
        "pick_z_offset_m": 0.02,
        "hull_blend_alpha": 0.25,
        "right_bottom_x_correction_max_m": 0.003,
        "corner_weighting": {
            "bottom_start_m": -0.005,
            "bottom_full_m": -0.020,
            "right_start_m": -0.055,
            "right_full_m": -0.070,
            "hull_blend_min_right_weight": 0.90,
            "x_correction_full_weight": 0.70,
        },
        "shape_to_action_group": {
            "sphere": "target_1",
            "cylinder": "target_2",
            "cuboid": "target_3",
        },
    },
    evidence_level="documented_parameter_reference_without_current_run_artifacts",
    documented_claim=(
        "The multi-shape delivery materials label these calibration and factory "
        "flow values as current/final reference parameters.  They are source "
        "snippets and a reference YAML, not proof of the configuration presently "
        "loaded on the robot or of a current successful run."
    ),
    source_notes=(
        "ArmPi_Ultra_多形状抓取放置与数据采集_代码详版资料包.zip: "
        "reference/final_parameters_reference.yaml.",
        "The same archive's reference snippets/document describe the 25% Hull "
        "blend, 3 mm right-bottom X correction, pitch=85, close=570, and "
        "cuboid -> target_3 mapping.",
        "The archive provides export tools rather than a complete current active "
        "shape_recognition.py/pick_and_place.py snapshot; revalidate on hardware.",
    ),
)


_BASELINES: Tuple[HardwareBaseline, ...] = (
    _FIXED_A_TO_B_P0,
    _MULTISHAPE_FACTORY,
)
_BASELINES_BY_ID = {baseline.baseline_id: baseline for baseline in _BASELINES}


def _copy_baseline(baseline: HardwareBaseline) -> HardwareBaseline:
    """Copy mutable fields so callers cannot alter the registry."""

    return HardwareBaseline(
        baseline_id=baseline.baseline_id,
        parameters=deepcopy(baseline.parameters),
        evidence_level=baseline.evidence_level,
        documented_claim=baseline.documented_claim,
        source_notes=baseline.source_notes,
        current_hardware_verified=baseline.current_hardware_verified,
    )


def list_hardware_baselines() -> List[HardwareBaseline]:
    """List the documented hardware baselines in stable order."""

    return [_copy_baseline(baseline) for baseline in _BASELINES]


def get_hardware_baseline(baseline_id: str) -> HardwareBaseline:
    """Get a documented baseline by ID.

    Raises:
        KeyError: if ``baseline_id`` is not one of the published IDs.
    """

    try:
        baseline = _BASELINES_BY_ID[baseline_id]
    except (KeyError, TypeError) as error:
        available = ", ".join(_BASELINES_BY_ID)
        raise KeyError(
            f"unknown hardware baseline {baseline_id!r}; available: {available}"
        ) from error
    return _copy_baseline(baseline)


__all__ = [
    "HardwareBaseline",
    "get_hardware_baseline",
    "list_hardware_baselines",
]
