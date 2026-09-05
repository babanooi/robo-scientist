"""Minimal, safety-gated HTTP bridge for an ArmPi motion backend.

The bridge has two modes.  It starts in ``dry_run`` mode by default and never
loads a robot-specific module in that mode.  Real motion is enabled only with
the ``--allow-real-motion`` flag and an explicitly configured backend factory.
"""

import argparse
import importlib
import json
import os
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Mapping, Optional, Tuple
from uuid import uuid4
from urllib.parse import urlparse


MAX_BODY_BYTES = 64 * 1024
REQUIRED_BACKEND_METHODS = (
    "health",
    "state",
    "preflight",
    "execute_pick_place",
    "stop",
)


class BridgeConfigurationError(ValueError):
    """Raised when real-motion mode has no usable high-level backend."""


class DryRunBackend:
    """Explicitly synthetic backend used until a physical backend is enabled."""

    name = "dry_run"

    def __init__(self) -> None:
        self._last_operation: Optional[str] = None

    def health(self) -> Dict[str, Any]:
        return {"available": True, "backend": self.name, "synthetic": True}

    def state(self) -> Dict[str, Any]:
        return {
            "state": "idle",
            "last_operation": self._last_operation,
            "synthetic": True,
        }

    def preflight(self, request: Mapping[str, Any]) -> Dict[str, Any]:
        del request
        self._last_operation = "preflight"
        return {
            "approved": True,
            "synthetic": True,
            "checks": ["dry_run_only"],
        }

    def execute_pick_place(self, request: Mapping[str, Any]) -> Dict[str, Any]:
        self._last_operation = "execute_pick_place"
        return {
            "execution_id": "dry-" + uuid4().hex[:12],
            "outcome": "synthetic_success",
            "synthetic": True,
            "echo": dict(request),
        }

    def stop(self, request: Mapping[str, Any]) -> Dict[str, Any]:
        self._last_operation = "stop"
        return {
            "stopped": True,
            "synthetic": True,
            "reason": request.get("reason", "operator_request"),
        }


def _mapping_result(value: Any, method_name: str) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise BridgeConfigurationError(
            "%s() must return a mapping, got %s" % (method_name, type(value).__name__)
        )
    return dict(value)


def load_backend(spec: str) -> Any:
    """Load a backend factory described as ``package.module:factory_name``."""

    module_name, separator, factory_name = spec.partition(":")
    if not module_name or not separator or not factory_name:
        raise BridgeConfigurationError(
            "backend must use the form 'package.module:factory_name'"
        )

    try:
        module = importlib.import_module(module_name)
        factory = getattr(module, factory_name)
    except (ImportError, AttributeError) as exc:
        raise BridgeConfigurationError("cannot load backend %r: %s" % (spec, exc)) from exc

    if not callable(factory):
        raise BridgeConfigurationError("backend factory %r is not callable" % spec)
    backend = factory()
    missing = [name for name in REQUIRED_BACKEND_METHODS if not callable(getattr(backend, name, None))]
    if missing:
        raise BridgeConfigurationError(
            "backend %r is missing methods: %s" % (spec, ", ".join(missing))
        )
    return backend


class BridgeService:
    """Backend-independent endpoint behavior and response envelopes."""

    def __init__(self, allow_real_motion: bool = False, backend: Any = None) -> None:
        if allow_real_motion:
            if backend is None:
                raise BridgeConfigurationError(
                    "real motion requires an explicitly configured backend"
                )
            self.mode = "real_motion"
            self.backend = backend
        else:
            self.mode = "dry_run"
            self.backend = DryRunBackend()
        self._motion_lock = threading.Lock()

    def response(self, data: Mapping[str, Any]) -> Dict[str, Any]:
        return {"ok": True, "mode": self.mode, "data": dict(data)}

    def error(self, code: str, message: str, data: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "ok": False,
            "mode": self.mode,
            "error": {"code": code, "message": message},
        }
        if data is not None:
            payload["data"] = dict(data)
        return payload

    def health(self) -> Tuple[int, Dict[str, Any]]:
        try:
            result = _mapping_result(self.backend.health(), "health")
        except Exception as exc:  # A hardware backend must not crash the HTTP server.
            return HTTPStatus.SERVICE_UNAVAILABLE, self.error("BACKEND_UNAVAILABLE", str(exc))
        result.update({
            "mode": self.mode,
            "motion_enabled": self.mode == "real_motion",
            "motion_busy": self._motion_lock.locked(),
        })
        return HTTPStatus.OK, self.response(result)

    def state(self) -> Tuple[int, Dict[str, Any]]:
        try:
            return HTTPStatus.OK, self.response(_mapping_result(self.backend.state(), "state"))
        except Exception as exc:
            return HTTPStatus.SERVICE_UNAVAILABLE, self.error("BACKEND_UNAVAILABLE", str(exc))

    def _busy(self) -> Tuple[int, Dict[str, Any]]:
        return HTTPStatus.CONFLICT, self.error(
            "ROBOT_BUSY", "another motion operation is already in progress"
        )

    def _preflight_unlocked(self, request: Mapping[str, Any]) -> Tuple[int, Dict[str, Any]]:
        try:
            result = _mapping_result(self.backend.preflight(request), "preflight")
        except Exception as exc:
            return HTTPStatus.SERVICE_UNAVAILABLE, self.error("PREFLIGHT_FAILED", str(exc))

        if result.get("approved") is not True:
            return HTTPStatus.CONFLICT, self.error(
                "PREFLIGHT_REJECTED",
                "backend did not approve this request",
                result,
            )
        return HTTPStatus.OK, self.response(result)

    def preflight(self, request: Mapping[str, Any]) -> Tuple[int, Dict[str, Any]]:
        if not self._motion_lock.acquire(blocking=False):
            return self._busy()
        try:
            return self._preflight_unlocked(request)
        finally:
            self._motion_lock.release()

    def execute_pick_place(self, request: Mapping[str, Any]) -> Tuple[int, Dict[str, Any]]:
        if not self._motion_lock.acquire(blocking=False):
            return self._busy()
        try:
            preflight_status, preflight_response = self._preflight_unlocked(request)
            if preflight_status != HTTPStatus.OK:
                return preflight_status, preflight_response
            try:
                result = _mapping_result(
                    self.backend.execute_pick_place(request), "execute_pick_place"
                )
            except Exception as exc:
                return HTTPStatus.SERVICE_UNAVAILABLE, self.error("EXECUTION_FAILED", str(exc))
            result["preflight"] = preflight_response["data"]
            return HTTPStatus.OK, self.response(result)
        finally:
            self._motion_lock.release()

    def stop(self, request: Mapping[str, Any]) -> Tuple[int, Dict[str, Any]]:
        try:
            return HTTPStatus.OK, self.response(
                _mapping_result(self.backend.stop(request), "stop")
            )
        except Exception as exc:
            return HTTPStatus.SERVICE_UNAVAILABLE, self.error("STOP_FAILED", str(exc))


class _BridgeRequestHandler(BaseHTTPRequestHandler):
    server_version = "RoboScientistHardwareBridge/0.1"

    @property
    def bridge(self) -> BridgeService:
        return self.server.bridge_service  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        """Keep the stdlib server quiet; robot operators use their own supervisor logs."""

    def _send(self, status: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Tuple[Optional[Dict[str, Any]], Optional[Tuple[int, Dict[str, Any]]]]:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError:
            return None, (HTTPStatus.BAD_REQUEST, self.bridge.error("INVALID_JSON", "invalid Content-Length"))
        if length < 0 or length > MAX_BODY_BYTES:
            return None, (HTTPStatus.REQUEST_ENTITY_TOO_LARGE, self.bridge.error("PAYLOAD_TOO_LARGE", "request body exceeds 64 KiB"))
        try:
            raw = self.rfile.read(length) if length else b"{}"
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, (HTTPStatus.BAD_REQUEST, self.bridge.error("INVALID_JSON", "request body must be a JSON object"))
        if not isinstance(value, dict):
            return None, (HTTPStatus.BAD_REQUEST, self.bridge.error("INVALID_JSON", "request body must be a JSON object"))
        return value, None

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            status, payload = self.bridge.health()
        elif path == "/state":
            status, payload = self.bridge.state()
        else:
            status, payload = HTTPStatus.NOT_FOUND, self.bridge.error("NOT_FOUND", "unknown endpoint")
        self._send(status, payload)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        request, error = self._read_json()
        if error is not None:
            self._send(*error)
            return
        assert request is not None
        if path == "/preflight":
            status, payload = self.bridge.preflight(request)
        elif path == "/execute_pick_place":
            status, payload = self.bridge.execute_pick_place(request)
        elif path == "/stop":
            status, payload = self.bridge.stop(request)
        else:
            status, payload = HTTPStatus.NOT_FOUND, self.bridge.error("NOT_FOUND", "unknown endpoint")
        self._send(status, payload)


def create_server(
    host: str = "127.0.0.1",
    port: int = 8060,
    allow_real_motion: bool = False,
    backend: Any = None,
) -> ThreadingHTTPServer:
    """Construct a server. ``backend`` is ignored unless real motion is enabled."""

    service = BridgeService(allow_real_motion, backend)
    server = ThreadingHTTPServer((host, port), _BridgeRequestHandler)
    server.daemon_threads = True
    server.bridge_service = service  # type: ignore[attr-defined]
    return server


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RoboScientist ArmPi hardware bridge")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8060)
    parser.add_argument(
        "--allow-real-motion",
        action="store_true",
        help="enable a robot-specific high-level motion backend",
    )
    parser.add_argument(
        "--backend",
        default=os.environ.get("ARMPI_BRIDGE_BACKEND"),
        help="backend factory, formatted as package.module:factory_name",
    )
    args = parser.parse_args(argv)
    if args.backend and not args.allow_real_motion:
        parser.error("--backend requires --allow-real-motion")
    if args.allow_real_motion and not args.backend:
        parser.error("--allow-real-motion requires --backend or ARMPI_BRIDGE_BACKEND")
    return args


def main(argv: Optional[list] = None) -> None:
    args = parse_args(argv)
    backend = load_backend(args.backend) if args.allow_real_motion else None
    server = create_server(args.host, args.port, args.allow_real_motion, backend)
    print("Hardware bridge listening on http://%s:%s (%s)" % (args.host, args.port, server.bridge_service.mode))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
