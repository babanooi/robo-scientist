"""Dependency-free local API and static UI for Mock, simulation, and real-arm modes."""

import argparse
import json
import os
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Optional, Union
from urllib.parse import parse_qs, urlparse

from roboscientist.adapters import (
    ArmPiAdapterStub,
    MockAdapter,
    MockScenario,
    RealArmAdapter,
    SimulationAdapter,
    VirtualSimulationAdapter,
    load_real_arm_profile,
)
from roboscientist.ai import QwenClient, QwenConfigurationError
from roboscientist.core.orchestrator import Orchestrator
from roboscientist.core.scientific_campaign import ScientificCampaignError, ScientificCampaignRunner
from roboscientist.core.validation import run_repeated_validation
from roboscientist.hardware_profiles import get_hardware_baseline, list_hardware_baselines
from roboscientist.hardware_bridge.evidence_package_validator import validate_evidence_package
from roboscientist.replay import ReplayService
from roboscientist.schemas import ExecutionMode, SkillVersion, new_id
from roboscientist.storage import ExperimentStore


STATIC_DIR = Path(__file__).parent / "static"
EXPERIMENT_PATH = re.compile(r"^/api/experiments/([A-Za-z0-9-]+)$")
ITERATE_PATH = re.compile(r"^/api/experiments/([A-Za-z0-9-]+)/iterate$")
CAMPAIGN_PATH = re.compile(r"^/api/campaigns/([A-Za-z0-9-]+)$")
CAMPAIGN_VALIDATION_PATH = re.compile(r"^/api/campaigns/([A-Za-z0-9-]+)/validation$")
REPLAY_PATH = re.compile(r"^/api/replay/([A-Za-z0-9._-]+)$")
HARDWARE_BASELINE_PATH = re.compile(r"^/api/hardware/baselines/([A-Za-z0-9._-]+)$")


HARDWARE_REFERENCE_SOURCE = "documented_hardware_reference"
HARDWARE_EVIDENCE_SOURCE = "hardware_evidence_package"
HARDWARE_EVIDENCE_ENV = "ROBO_HARDWARE_EVIDENCE_ROOT"


class DemoApplication:
    """Selects an adapter; real motion needs both a profile and an enable gate."""

    def __init__(
        self,
        data_root: Union[Path, str] = "data",
        real_arm_profile: Optional[Union[Path, str]] = None,
        qwen_client: Optional[QwenClient] = None,
        replay_source: Optional[Union[Path, str]] = None,
        hardware_evidence_source: Optional[Union[Path, str]] = None,
        virtual_simulation: bool = True,
    ):
        self.store = ExperimentStore(data_root)
        self.real_arm_profile = (
            load_real_arm_profile(real_arm_profile) if real_arm_profile else None
        )
        self.qwen_client = qwen_client
        self.virtual_simulation = bool(virtual_simulation)
        self.replay_service = ReplayService(replay_source)
        configured_evidence = (
            hardware_evidence_source
            if hardware_evidence_source is not None
            else os.environ.get(HARDWARE_EVIDENCE_ENV)
        )
        self.hardware_evidence_source = (
            Path(configured_evidence).expanduser() if configured_evidence else None
        )

    @staticmethod
    def _scenario(value: str) -> MockScenario:
        try:
            return MockScenario(value)
        except ValueError as error:
            names = ", ".join(item.value for item in MockScenario)
            raise ValueError(f"scenario must be one of: {names}") from error

    def _adapter_for(self, mode: str, scenario: str):
        try:
            selected_mode = ExecutionMode(mode)
        except ValueError as error:
            names = ", ".join(item.value for item in ExecutionMode)
            raise ValueError(f"mode must be one of: {names}") from error
        if selected_mode is ExecutionMode.MOCK:
            return MockAdapter(self._scenario(scenario))
        if selected_mode is ExecutionMode.SIMULATION:
            # Public ``simulation`` mode uses the self-contained, deterministic
            # virtual workcell.  The legacy SimulationAdapter remains an inert
            # Gazebo/MoveIt2 contract and is intentionally not used as a
            # source of fabricated execution results.
            return (
                VirtualSimulationAdapter(
                    scenario,
                    artifact_root=self.store.root / "virtual_artifacts",
                )
                if self.virtual_simulation
                else SimulationAdapter()
            )
        if self.real_arm_profile:
            return RealArmAdapter(self.real_arm_profile)
        return ArmPiAdapterStub()

    def _orchestrator_for(self, mode: str, scenario: str):
        adapter = self._adapter_for(mode, scenario)
        profile = getattr(adapter, "profile", None)
        return Orchestrator(
            adapter,
            self.store,
            constraints=profile.safety_constraints if profile else None,
            # Prefer the selected adapter's pinned scene identifier.  The
            # virtual workcell has no hardware profile, but its scene is still
            # part of the immutable experimental conditions and must not fall
            # back to the legacy mock identifier.
            scene_id=(
                profile.scene_id
                if profile
                else getattr(adapter, "scene_id", "mock-fixed-workbench-v0")
            ),
            target_pose=profile.target_pose if profile else None,
            destination_pose=profile.destination_pose if profile else None,
            execution_scenario=scenario,
        )

    def config(self) -> dict:
        qwen = {
            "enabled": False,
            "configured": False,
            "provider": "aliyun_model_studio",
            "model": None,
            "error": None,
        }
        try:
            client = self.qwen_client or QwenClient.from_env(required=False)
            if client is not None:
                status = client.status() if hasattr(client, "status") else {
                    "configured": True,
                    "provider": "aliyun_model_studio",
                    "model": getattr(client, "model", "unknown"),
                }
                qwen.update({"enabled": True, **status})
        except QwenConfigurationError as error:
            qwen["error"] = str(error)
        baselines = list_hardware_baselines()
        return {
            "qwen": qwen,
            "project_policy": {
                "minimum_runs_per_version": 10,
                "minimum_runs_is_internal_policy": True,
            },
            "supported_modes": [mode.value for mode in ExecutionMode],
            "campaign_api": {
                "use_qwen_default": True,
                "deterministic_only_requires_explicit_false": True,
                "validation_endpoint": "/api/validation",
            },
            "virtual_runtime": {
                "enabled": True,
                "runtime_name": "roboscientist.virtual_workcell",
                "runtime_version": "1.0.0",
                "engine": "python_deterministic",
                "data_source": "simulation",
                "hardware_status": "simulation_runtime_verified",
                "physical_robot_connected": False,
                "gazebo_moveit2": "not_used",
            },
            # Keep /api/config compact: callers that need the documented
            # parameter snapshots should use /api/hardware/baselines.
            "hardware_baselines": {
                "count": len(baselines),
                "ids": [baseline.baseline_id for baseline in baselines],
                "data_source": HARDWARE_REFERENCE_SOURCE,
                "current_hardware_verified": False,
                "read_only": True,
                "motion_requested": False,
            },
            "hardware_evidence": {
                "configured": self.hardware_evidence_source is not None,
                "source_name": (
                    self.hardware_evidence_source.name
                    if self.hardware_evidence_source is not None
                    else None
                ),
                "validator": "evidence_package_validator",
                "read_only": True,
                "motion_requested": False,
            },
            "replay": {
                "configured": self.replay_service.configured,
                "read_only": True,
                "motion_requested": False,
                "source_name": self.replay_service.root.name if self.replay_service.root else None,
            },
        }

    @staticmethod
    def health() -> dict:
        """Small deployment probe with an explicit no-physical-arm boundary."""
        return {
            "status": "ok",
            "service": "roboscientist",
            "api_version": "v1",
            "virtual_runtime": "ready",
            "physical_robot": "disabled",
            "claims": "software_and_virtual_experiment_only",
        }

    @staticmethod
    def hardware_baselines() -> dict:
        """Return documented hardware snapshots without enabling execution.

        These values are reference material extracted from delivery archives;
        they are intentionally not wired into ``RealArmAdapter`` or any motion
        path.  The explicit boundary fields make that distinction visible to
        API and UI consumers.
        """

        baselines = [baseline.to_dict() for baseline in list_hardware_baselines()]
        return {
            "status": "available",
            "data_source": HARDWARE_REFERENCE_SOURCE,
            "read_only": True,
            "motion_requested": False,
            "current_hardware_verified": False,
            "baseline_count": len(baselines),
            "baseline_ids": [baseline["baseline_id"] for baseline in baselines],
            "baselines": baselines,
        }

    @staticmethod
    def hardware_baseline(baseline_id: str) -> dict:
        """Return one documented baseline, or raise ``KeyError`` for unknown IDs."""

        baseline = get_hardware_baseline(baseline_id)
        data = baseline.to_dict()
        # Keep the object nested for new clients while exposing the common
        # fields at the top level for simple callers and backwards-compatible
        # inspection in scripts.
        return {
            "status": "available",
            "data_source": HARDWARE_REFERENCE_SOURCE,
            "read_only": True,
            "motion_requested": False,
            "current_hardware_verified": baseline.current_hardware_verified,
            "baseline": data,
            **data,
        }

    def hardware_evidence(self) -> dict:
        """Validate the configured delivery package without accepting a path from HTTP.

        The source is fixed when the application starts (constructor, CLI, or
        ``ROBO_HARDWARE_EVIDENCE_ROOT``).  A browser can therefore inspect the
        report but cannot ask the server to read an arbitrary local path.
        """

        if self.hardware_evidence_source is None:
            return {
                "status": "unavailable",
                "data_source": HARDWARE_EVIDENCE_SOURCE,
                "read_only": True,
                "motion_requested": False,
                "source_configured": False,
                "source_name": None,
                "reason_code": "HARDWARE_EVIDENCE_SOURCE_NOT_CONFIGURED",
                "message": "no hardware evidence package is configured",
                "validation": None,
            }

        validation = validate_evidence_package(self.hardware_evidence_source)
        status = validation.get("status", "invalid")
        return {
            "status": status,
            "data_source": HARDWARE_EVIDENCE_SOURCE,
            "read_only": True,
            "motion_requested": False,
            "source_configured": True,
            "source_name": self.hardware_evidence_source.name,
            "reason_code": "HARDWARE_EVIDENCE_{}".format(status.upper()),
            "validation": validation,
            # Flatten the stable report fields for small clients; the nested
            # object remains the authoritative validator response.
            "package_type": validation.get("package_type"),
            "files_checked": validation.get("files_checked", []),
            "missing": validation.get("missing", []),
            "warnings": validation.get("warnings", []),
            "claims_supported": validation.get("claims_supported", {}),
            "expected_claims": validation.get("expected_claims", []),
        }

    def replay(self, run_id: Optional[str] = None) -> dict:
        """Return historical motion evidence without selecting an execution adapter."""
        return (
            self.replay_service.get_run(run_id)
            if run_id is not None
            else self.replay_service.list_runs()
        )

    def runtime(self, mode: str) -> dict:
        adapter = self._adapter_for(mode, MockScenario.SUCCESS.value)
        state = adapter.get_robot_state()
        if not isinstance(state, dict):
            state = {"available": False, "message": "adapter returned invalid runtime state"}
        return {
            "mode": mode,
            "adapter": adapter.name,
            "data_source": adapter.data_source,
            "hardware_status": adapter.hardware_status,
            **state,
        }

    def stop(self, mode: str = ExecutionMode.REAL_ARM.value, reason: str = "operator_request") -> dict:
        """Request the adapter's fastest safe stop without starting a new experiment."""
        if mode != ExecutionMode.REAL_ARM.value:
            raise ValueError("stop endpoint only supports mode=real_arm")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be a non-empty string")
        adapter = self._adapter_for(mode, MockScenario.SUCCESS.value)
        action = adapter.stop(reason.strip())
        return {
            "stop_id": new_id("stop"),
            "mode": adapter.data_source,
            "data_source": adapter.data_source,
            "hardware_status": adapter.hardware_status,
            "stop": action.model_dump(mode="json") if hasattr(action, "model_dump") else action,
        }

    def run_task(
        self,
        task_text: str,
        scenario: str,
        mode: str = ExecutionMode.MOCK.value,
        skill: Optional[SkillVersion] = None,
    ) -> dict:
        if not isinstance(task_text, str) or not task_text.strip():
            raise ValueError("task_text is required")
        selected_skill = skill or SkillVersion(version="p0")
        result = self._orchestrator_for(mode, scenario).run(task_text.strip(), selected_skill)
        return self.experiment(result.experiment_id)

    def experiment(self, experiment_id: str) -> dict:
        record = self.store.read_experiment(experiment_id)
        candidate = record["result"].get("candidate_skill_version") if record["result"] else None
        record["candidate_skill"] = (
            self.store.read_skill(candidate).model_dump(mode="json") if candidate else None
        )
        data_source = record["result"]["data_source"]
        notices = {
            "mock": "流程 Mock 只验证规划、归因和版本演进，不代表真实机械臂数据。",
            "simulation": "纯 Python 软件虚拟工作单元；不等同于 Gazebo/MoveIt2 或真实机械臂。",
            "real_arm": "真实机械臂结果；运动需通过本地配置、桥接健康检查和双重放行门。",
        }
        record["execution_mode"] = data_source
        record["mode_notice"] = notices[data_source]
        return record

    def iterate(
        self,
        experiment_id: str,
        scenario: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> dict:
        previous = self.store.read_experiment(experiment_id)
        if not previous["plan"] or not previous["result"]:
            raise FileNotFoundError(f"incomplete experiment: {experiment_id}")
        candidate = previous["result"].get("candidate_skill_version")
        skill = (
            self.store.read_skill(candidate)
            if candidate
            else SkillVersion.model_validate(previous["plan"]["skill"])
        )
        task_text = previous["plan"]["task"]["source_text"]
        selected_mode = previous["result"]["data_source"]
        selected_scenario = previous["plan"].get("execution_scenario", "default")
        if scenario is not None and scenario != selected_scenario:
            raise ValueError("P1 must inherit P0 execution_scenario; changing scenario is not allowed")
        if mode is not None and mode != selected_mode:
            raise ValueError("P1 must inherit P0 data_source; changing mode is not allowed")
        return self.run_task(task_text, selected_scenario, selected_mode, skill)

    def run_campaign(
        self,
        task_text: str,
        scenario: str,
        mode: str,
        max_rounds: int = 2,
        use_qwen: bool = True,
        auto_run_p1: bool = True,
    ) -> dict:
        """Run a bounded scientific campaign with immutable P0 conditions.

        The official path defaults to Qwen mode. Offline tests and development
        callers must pass ``use_qwen=false`` explicitly to select the
        deterministic-only path.
        """
        if not isinstance(max_rounds, int) or max_rounds not in (1, 2):
            raise ValueError("max_rounds must be 1 or 2 for a bounded P0/P1 campaign")
        if not isinstance(use_qwen, bool):
            raise ValueError("use_qwen must be boolean")
        if not isinstance(auto_run_p1, bool):
            raise ValueError("auto_run_p1 must be boolean")
        orchestrator = self._orchestrator_for(mode, scenario)
        campaign = ScientificCampaignRunner(
            self.store, qwen_client=self.qwen_client
        ).run(
            task_text,
            orchestrator,
            use_qwen=use_qwen,
            auto_run_p1=auto_run_p1 and max_rounds == 2,
        )
        # Reuse the same presentation enrichment as individual experiments.
        campaign["records"] = [
            self.experiment(record["result"]["experiment_id"])
            for record in campaign.get("records", [])
        ]
        campaign["rounds_completed"] = len(campaign["records"])
        campaign["latest_experiment_id"] = (
            campaign["records"][-1]["result"]["experiment_id"]
            if campaign["records"] else None
        )
        campaign["promotion"] = "candidate_only"
        return campaign

    def run_validation(
        self,
        task_text: str,
        scenario: str,
        mode: str = ExecutionMode.SIMULATION.value,
        repeats: int = 10,
        use_qwen: bool = False,
    ) -> dict:
        """Run a campaign plus repeated P0/P1 observations for submission evidence."""

        if mode == ExecutionMode.REAL_ARM.value:
            raise ValueError("repeated validation is disabled for real_arm in the no-physical-arm build")
        campaign = self.run_campaign(
            task_text,
            scenario,
            mode,
            max_rounds=2,
            use_qwen=use_qwen,
            auto_run_p1=True,
        )
        records = campaign.get("records", [])
        if len(records) < 2:
            campaign["validation"] = {
                "format_version": "validation-v1",
                "status": "blocked",
                "reason": "campaign did not produce both P0 and P1 records",
            }
            return campaign
        p0_record = records[0]
        candidate_payload = p0_record.get("candidate_skill")
        if not candidate_payload:
            campaign["validation"] = {
                "format_version": "validation-v1",
                "status": "blocked",
                "reason": "P0 did not produce an allowed candidate Skill",
            }
            return campaign
        baseline_skill = SkillVersion.model_validate(p0_record["plan"]["skill"])
        candidate_skill = SkillVersion.model_validate(candidate_payload)
        orchestrator = self._orchestrator_for(mode, scenario)
        scientific_plan = campaign.get("scientific_plan")
        feedback = campaign.get("feedback_adjustment")
        from roboscientist.schemas import FeedbackAdjustmentDraft, ScientificPlanDraft

        validation = run_repeated_validation(
            store=self.store,
            orchestrator=orchestrator,
            task_text=task_text,
            baseline_skill=baseline_skill,
            candidate_skill=candidate_skill,
            repeats=repeats,
            scientific_plan=(ScientificPlanDraft.model_validate(scientific_plan) if scientific_plan else None),
            feedback_adjustment=(FeedbackAdjustmentDraft.model_validate(feedback) if feedback else None),
            planning_source="qwen" if use_qwen else "deterministic",
        )
        validation["status"] = "completed"
        validation["campaign_id"] = campaign["campaign_id"]
        self.store.write_campaign_validation(campaign["campaign_id"], validation)
        campaign["validation"] = validation
        campaign["validation_status"] = validation["comparison"]["decision"]
        return campaign

    def campaign(self, campaign_id: str) -> dict:
        payload = self.store.read_campaign(campaign_id)
        payload["records"] = [
            self.experiment(record["result"]["experiment_id"])
            for record in payload.get("records", [])
            if record.get("result", {}).get("experiment_id")
        ]
        payload["rounds_completed"] = len(payload["records"])
        return payload

    def campaign_validation(self, campaign_id: str) -> dict:
        payload = self.store.read_campaign(campaign_id)
        validation = payload.get("validation")
        if validation is None:
            raise FileNotFoundError(f"validation does not exist: {campaign_id}")
        return validation


class MockApplication(DemoApplication):
    """Backwards-compatible fixture facade used by the legacy unit tests.

    The production HTTP server uses :class:`DemoApplication` and therefore
    enables the verified, self-contained virtual workcell.  Older callers
    imported ``MockApplication`` to assert the inert SimulationAdapter
    contract; keeping that facade avoids silently changing their expectations
    while still exposing ``virtual_simulation=True`` when desired.
    """

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("virtual_simulation", False)
        super().__init__(*args, **kwargs)


class RoboScientistHandler(BaseHTTPRequestHandler):
    application: DemoApplication

    def log_message(self, format: str, *args) -> None:
        return

    def _send_json(self, status: HTTPStatus, payload: dict) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_file(self, filename: str, content_type: str) -> None:
        path = STATIC_DIR / filename
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _body(self, *, allow_empty: bool = False) -> Dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("Content-Length must be an integer") from error
        if length <= 0 and not allow_empty:
            raise ValueError("request body is required")
        if length <= 0:
            return {}
        if length > 1_000_000:
            raise ValueError("request body is too large")
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("request body must be JSON") from error
        if not isinstance(body, dict):
            raise ValueError("request body must be a JSON object")
        return body

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._send_json(status, {"error": message if isinstance(message, dict) else {"message": message}})

    def _internal_error(self, request_id: str, error: Exception) -> None:
        self.log_error(
            "unhandled request failure request_id=%s error_type=%s",
            request_id,
            type(error).__name__,
        )
        self._send_json(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            {
                "error": {
                    "code": "INTERNAL_SERVER_ERROR",
                    "message": "unexpected server error",
                    "request_id": request_id,
                }
            },
        )

    def do_GET(self) -> None:
        request_id = new_id("request")
        try:
            self._do_GET()
        except Exception as error:
            self._internal_error(request_id, error)

    def _do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self._send_file("index.html", "text/html; charset=utf-8")
            return
        if path == "/app.js":
            self._send_file("app.js", "application/javascript; charset=utf-8")
            return
        if path == "/app.css":
            self._send_file("app.css", "text/css; charset=utf-8")
            return
        if path == "/api/skills":
            self._send_json(
                HTTPStatus.OK,
                {"skills": self.application.store.list_skills()},
            )
            return
        if path == "/api/config":
            self._send_json(HTTPStatus.OK, self.application.config())
            return
        if path == "/api/health":
            self._send_json(HTTPStatus.OK, self.application.health())
            return
        if path == "/api/hardware/baselines":
            self._send_json(HTTPStatus.OK, self.application.hardware_baselines())
            return
        matched = HARDWARE_BASELINE_PATH.match(path)
        if matched:
            try:
                self._send_json(
                    HTTPStatus.OK,
                    self.application.hardware_baseline(matched.group(1)),
                )
            except KeyError as error:
                self._error(HTTPStatus.NOT_FOUND, str(error))
            return
        if path == "/api/hardware/evidence":
            self._send_json(HTTPStatus.OK, self.application.hardware_evidence())
            return
        if path == "/api/runtime":
            try:
                mode = parse_qs(parsed.query).get("mode", ["mock"])[0]
                self._send_json(HTTPStatus.OK, self.application.runtime(mode))
            except ValueError as error:
                self._error(HTTPStatus.BAD_REQUEST, str(error))
            return
        if path == "/api/replay":
            query = parse_qs(parsed.query)
            run_id = query.get("run_id", [None])[0]
            self._send_json(HTTPStatus.OK, self.application.replay(run_id))
            return
        matched = REPLAY_PATH.match(path)
        if matched:
            self._send_json(HTTPStatus.OK, self.application.replay(matched.group(1)))
            return
        matched = EXPERIMENT_PATH.match(path)
        if matched:
            try:
                self._send_json(HTTPStatus.OK, self.application.experiment(matched.group(1)))
            except FileNotFoundError as error:
                self._error(HTTPStatus.NOT_FOUND, str(error))
            return
        matched = CAMPAIGN_PATH.match(path)
        if matched:
            try:
                self._send_json(HTTPStatus.OK, self.application.campaign(matched.group(1)))
            except FileNotFoundError as error:
                self._error(HTTPStatus.NOT_FOUND, str(error))
            return
        matched = CAMPAIGN_VALIDATION_PATH.match(path)
        if matched:
            try:
                self._send_json(
                    HTTPStatus.OK,
                    self.application.campaign_validation(matched.group(1)),
                )
            except FileNotFoundError as error:
                self._error(HTTPStatus.NOT_FOUND, str(error))
            return
        self._error(HTTPStatus.NOT_FOUND, "route not found")

    def do_POST(self) -> None:
        request_id = new_id("request")
        try:
            body = self._body(allow_empty=self.path == "/api/stop")
            if self.path == "/api/tasks":
                response = self.application.run_task(
                    body.get("task_text"),
                    body.get("scenario", "success"),
                    body.get("mode", "simulation"),
                )
                self._send_json(HTTPStatus.CREATED, response)
                return
            if self.path == "/api/campaigns":
                response = self.application.run_campaign(
                    body.get("task_text"),
                    body.get("scenario", "success"),
                    body.get("mode", "simulation"),
                    body.get("max_rounds", 2),
                    body.get("use_qwen", True),
                    body.get("auto_run_p1", True),
                )
                self._send_json(HTTPStatus.CREATED, response)
                return
            if self.path == "/api/validation":
                response = self.application.run_validation(
                    body.get("task_text"),
                    body.get("scenario", "pose_offset"),
                    body.get("mode", ExecutionMode.SIMULATION.value),
                    body.get("repeats", 10),
                    body.get("use_qwen", False),
                )
                self._send_json(HTTPStatus.CREATED, response)
                return
            if self.path == "/api/stop":
                response = self.application.stop(
                    body.get("mode", ExecutionMode.REAL_ARM.value),
                    body.get("reason", "operator_request"),
                )
                self._send_json(HTTPStatus.OK, response)
                return
            matched = ITERATE_PATH.match(self.path)
            if matched:
                response = self.application.iterate(matched.group(1))
                self._send_json(HTTPStatus.CREATED, response)
                return
            self._error(HTTPStatus.NOT_FOUND, "route not found")
        except ValueError as error:
            self._error(HTTPStatus.BAD_REQUEST, str(error))
        except ScientificCampaignError as error:
            self._send_json(
                HTTPStatus.BAD_GATEWAY,
                {
                    "error": {
                        "code": error.code,
                        "stage": error.stage,
                        "message": str(error),
                        "campaign_id": error.campaign_id,
                    }
                },
            )
        except FileNotFoundError as error:
            self._error(HTTPStatus.NOT_FOUND, str(error))
        except Exception as error:
            self._internal_error(request_id, error)


def create_server(
    host: str = "127.0.0.1", port: int = 8001, data_root: Union[Path, str] = "data",
    real_arm_profile: Optional[Union[Path, str]] = None,
    qwen_client: Optional[QwenClient] = None,
    replay_source: Optional[Union[Path, str]] = None,
    hardware_evidence_source: Optional[Union[Path, str]] = None,
) -> ThreadingHTTPServer:
    RoboScientistHandler.application = DemoApplication(
        data_root,
        real_arm_profile,
        qwen_client,
        replay_source,
        hardware_evidence_source,
    )
    return ThreadingHTTPServer((host, port), RoboScientistHandler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the RoboScientist simulation demo")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--real-arm-profile", help="approved real-arm profile JSON; absent means real-arm mode is blocked")
    parser.add_argument(
        "--replay-source",
        help="historical ArmPi run directory or tar archive for read-only replay",
    )
    parser.add_argument(
        "--hardware-evidence-source",
        help="hardware P0/P1 evidence directory or archive for read-only validation",
    )
    args = parser.parse_args()
    server = create_server(
        args.host, args.port, args.data_root, args.real_arm_profile,
        replay_source=args.replay_source,
        hardware_evidence_source=args.hardware_evidence_source,
    )
    print(f"RoboScientist demo: http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
