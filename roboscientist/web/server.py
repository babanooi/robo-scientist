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
from roboscientist.core.orchestrator import Orchestrator
from roboscientist.schemas import ExecutionMode, SkillVersion
from roboscientist.storage import ExperimentStore


STATIC_DIR = Path(__file__).parent / "static"
EXPERIMENT_PATH = re.compile(r"^/api/experiments/([A-Za-z0-9-]+)$")
ITERATE_PATH = re.compile(r"^/api/experiments/([A-Za-z0-9-]+)/iterate$")


class DemoApplication:
    """Selects an adapter; real motion needs both a profile and an enable gate."""

    def __init__(
        self, data_root: Union[Path, str] = "data", real_arm_profile: Optional[Union[Path, str]] = None
    ):
        self.store = ExperimentStore(data_root)
        self.real_arm_profile = (
            load_real_arm_profile(real_arm_profile) if real_arm_profile else None
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
            return SimulationAdapter()
        if self.real_arm_profile:
            return RealArmAdapter(self.real_arm_profile)
        return ArmPiAdapterStub()

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
        adapter = self._adapter_for(mode, scenario)
        profile = getattr(adapter, "profile", None)
        result = Orchestrator(
            adapter,
            self.store,
            constraints=profile.safety_constraints if profile else None,
            scene_id=profile.scene_id if profile else "mock-fixed-workbench-v0",
            target_pose=profile.target_pose if profile else None,
            destination_pose=profile.destination_pose if profile else None,
        ).run(task_text.strip(), selected_skill)
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

    def iterate(self, experiment_id: str, scenario: str, mode: Optional[str] = None) -> dict:
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
        selected_mode = mode or previous["result"]["data_source"]
        return self.run_task(task_text, scenario, selected_mode, skill)

    def run_campaign(
        self, task_text: str, scenario: str, mode: str, max_rounds: int = 2
    ) -> dict:
        """Run one baseline and bounded candidate attempts under the same safety gates.

        This deliberately leaves every generated version as a candidate. A first
        successful attempt is evidence, not enough data for automatic promotion.
        """
        if not isinstance(max_rounds, int) or not 1 <= max_rounds <= 3:
            raise ValueError("max_rounds must be an integer from 1 to 3")
        records = [self.run_task(task_text, scenario, mode)]
        while len(records) < max_rounds:
            current = records[-1]
            if not current["candidate_skill"]:
                break
            records.append(self.iterate(
                current["result"]["experiment_id"], scenario, mode
            ))
        latest = records[-1]
        return {
            "records": records,
            "rounds_completed": len(records),
            "promotion": "candidate_only",
            "message": (
                "The campaign keeps every new skill as a candidate; repeated "
                "independent validation is required before promotion."
            ),
            "latest_experiment_id": latest["result"]["experiment_id"],
        }


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

    def _body(self) -> Dict:
        length = int(self.headers.get("Content-Length", "0"))
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as error:
            raise ValueError("request body must be JSON") from error

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._send_json(status, {"error": message})

    def do_GET(self) -> None:
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
        self._error(HTTPStatus.NOT_FOUND, "route not found")

    def do_POST(self) -> None:
        try:
            body = self._body()
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
                )
                self._send_json(HTTPStatus.CREATED, response)
                return
            matched = ITERATE_PATH.match(self.path)
            if matched:
                response = self.application.iterate(
                    matched.group(1),
                    body.get("scenario", "success"),
                    body.get("mode"),
                )
                self._send_json(HTTPStatus.CREATED, response)
                return
            self._error(HTTPStatus.NOT_FOUND, "route not found")
        except ValueError as error:
            self._error(HTTPStatus.BAD_REQUEST, str(error))
        except FileNotFoundError as error:
            self._error(HTTPStatus.NOT_FOUND, str(error))


def create_server(
    host: str = "127.0.0.1", port: int = 8001, data_root: Union[Path, str] = "data",
    real_arm_profile: Optional[Union[Path, str]] = None,
) -> ThreadingHTTPServer:
    RoboScientistHandler.application = DemoApplication(data_root, real_arm_profile)
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
