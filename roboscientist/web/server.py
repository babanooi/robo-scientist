"""Dependency-free local API and static UI for Mock, simulation, and real-arm modes."""

import argparse
import json
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Optional, Union

from roboscientist.adapters import (
    ArmPiAdapterStub,
    MockAdapter,
    MockScenario,
    SimulationAdapter,
)
from roboscientist.core.orchestrator import Orchestrator
from roboscientist.schemas import ExecutionMode, SkillVersion
from roboscientist.storage import ExperimentStore


STATIC_DIR = Path(__file__).parent / "static"
EXPERIMENT_PATH = re.compile(r"^/api/experiments/([A-Za-z0-9-]+)$")
ITERATE_PATH = re.compile(r"^/api/experiments/([A-Za-z0-9-]+)/iterate$")


class DemoApplication:
    """Selects an adapter without ever issuing real hardware commands."""

    def __init__(self, data_root: Union[Path, str] = "data"):
        self.store = ExperimentStore(data_root)

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
        return ArmPiAdapterStub()

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
        result = Orchestrator(adapter, self.store).run(task_text.strip(), selected_skill)
        return self.experiment(result.experiment_id)

    def experiment(self, experiment_id: str) -> dict:
        record = self.store.read_experiment(experiment_id)
        candidate = record["result"].get("candidate_skill_version") if record["result"] else None
        record["candidate_skill"] = (
            self.store.read_skill(candidate).model_dump(mode="json") if candidate else None
        )
        data_source = record["result"]["data_source"]
        notices = {
            "mock": "Mock workflow simulation. This is not real robot data.",
            "simulation": "Gazebo/MoveIt2 virtual simulation. Runtime verification status is shown below.",
            "real_arm": "Real-arm mode is disabled until an explicit safety-confirmed adapter is delivered.",
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
        if self.path in ("/", "/index.html"):
            self._send_file("index.html", "text/html; charset=utf-8")
            return
        if self.path == "/app.js":
            self._send_file("app.js", "application/javascript; charset=utf-8")
            return
        if self.path == "/app.css":
            self._send_file("app.css", "text/css; charset=utf-8")
            return
        if self.path == "/api/skills":
            self._send_json(
                HTTPStatus.OK,
                {"skills": self.application.store.list_skills()},
            )
            return
        matched = EXPERIMENT_PATH.match(self.path)
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
    host: str = "127.0.0.1", port: int = 8001, data_root: Union[Path, str] = "data"
) -> ThreadingHTTPServer:
    RoboScientistHandler.application = DemoApplication(data_root)
    return ThreadingHTTPServer((host, port), RoboScientistHandler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the RoboScientist simulation demo")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--data-root", default="data")
    args = parser.parse_args()
    server = create_server(args.host, args.port, args.data_root)
    print(f"RoboScientist demo: http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
