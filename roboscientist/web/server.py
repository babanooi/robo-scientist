"""Dependency-free local API and static UI for Mock, simulation, and real-arm modes."""

import argparse
import json
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
    load_real_arm_profile,
)
from roboscientist.ai import QwenClient, QwenConfigurationError
from roboscientist.core.orchestrator import Orchestrator
from roboscientist.core.scientific_campaign import ScientificCampaignError, ScientificCampaignRunner
from roboscientist.schemas import ExecutionMode, SkillVersion, new_id
from roboscientist.storage import ExperimentStore


STATIC_DIR = Path(__file__).parent / "static"
EXPERIMENT_PATH = re.compile(r"^/api/experiments/([A-Za-z0-9-]+)$")
ITERATE_PATH = re.compile(r"^/api/experiments/([A-Za-z0-9-]+)/iterate$")
CAMPAIGN_PATH = re.compile(r"^/api/campaigns/([A-Za-z0-9-]+)$")


class DemoApplication:
    """Selects an adapter; real motion needs both a profile and an enable gate."""

    def __init__(
        self,
        data_root: Union[Path, str] = "data",
        real_arm_profile: Optional[Union[Path, str]] = None,
        qwen_client: Optional[QwenClient] = None,
    ):
        self.store = ExperimentStore(data_root)
        self.real_arm_profile = (
            load_real_arm_profile(real_arm_profile) if real_arm_profile else None
        )
        self.qwen_client = qwen_client

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
            return SimulationAdapter()
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
            scene_id=profile.scene_id if profile else "mock-fixed-workbench-v0",
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
            },
        }

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
            "simulation": "Gazebo/MoveIt2 虚拟仿真；运行时是否接通以本轮状态为准。",
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

    def campaign(self, campaign_id: str) -> dict:
        payload = self.store.read_campaign(campaign_id)
        payload["records"] = [
            self.experiment(record["result"]["experiment_id"])
            for record in payload.get("records", [])
            if record.get("result", {}).get("experiment_id")
        ]
        payload["rounds_completed"] = len(payload["records"])
        return payload


MockApplication = DemoApplication


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
        if path == "/api/runtime":
            try:
                mode = parse_qs(parsed.query).get("mode", ["mock"])[0]
                self._send_json(HTTPStatus.OK, self.application.runtime(mode))
            except ValueError as error:
                self._error(HTTPStatus.BAD_REQUEST, str(error))
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
) -> ThreadingHTTPServer:
    RoboScientistHandler.application = DemoApplication(data_root, real_arm_profile, qwen_client)
    return ThreadingHTTPServer((host, port), RoboScientistHandler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the RoboScientist simulation demo")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--real-arm-profile", help="approved real-arm profile JSON; absent means real-arm mode is blocked")
    args = parser.parse_args()
    server = create_server(args.host, args.port, args.data_root, args.real_arm_profile)
    print(f"RoboScientist demo: http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
