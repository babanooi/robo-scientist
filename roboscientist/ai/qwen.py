"""Minimal Alibaba Cloud Model Studio client with auditable JSON output."""

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Type
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel, ValidationError


DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen3.7-plus"
ENV_KEYS = (
    "DASHSCOPE_API_KEY",
    "DASHSCOPE_BASE_URL",
    "QWEN_MODEL",
    "QWEN_TIMEOUT_S",
)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _local_qwen_settings() -> Dict[str, str]:
    """Read only Qwen settings from an ignored local file, never execute it."""

    configured_path = os.environ.get("ROBO_ENV_FILE")
    path = Path(configured_path) if configured_path else Path(__file__).resolve().parents[2] / ".env"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}

    settings: Dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, value = line.partition("=")
        name = name.strip()
        if not separator or name not in ENV_KEYS:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        settings[name] = value
    return settings


class QwenConfigurationError(RuntimeError):
    pass


class QwenCallError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        request_payload: Optional[dict] = None,
        response_payload: Optional[dict] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        super().__init__(message)
        self.request_payload = request_payload or {}
        self.response_payload = response_payload or {}
        self.metadata = metadata or {}


@dataclass
class StructuredQwenCall:
    output: BaseModel
    request_payload: Dict[str, Any]
    response_payload: Dict[str, Any]
    metadata: Dict[str, Any]


class QwenClient:
    """Call Qwen through Model Studio's OpenAI-compatible chat endpoint."""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout_s: float = 90.0,
        urlopen_func=urlopen,
    ) -> None:
        if not api_key:
            raise QwenConfigurationError("DASHSCOPE_API_KEY is required")
        if not base_url.startswith(("https://", "http://")):
            raise QwenConfigurationError("DASHSCOPE_BASE_URL must be an HTTP URL")
        if not model:
            raise QwenConfigurationError("QWEN_MODEL must not be empty")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self._urlopen = urlopen_func

    @classmethod
    def from_env(cls, required: bool = False) -> Optional["QwenClient"]:
        local_settings = _local_qwen_settings()
        api_key = os.environ.get(
            "DASHSCOPE_API_KEY", local_settings.get("DASHSCOPE_API_KEY", "")
        ).strip()
        if not api_key:
            if required:
                raise QwenConfigurationError(
                    "Qwen is required but DASHSCOPE_API_KEY is not configured"
                )
            return None
        try:
            timeout_s = float(
                os.environ.get("QWEN_TIMEOUT_S", local_settings.get("QWEN_TIMEOUT_S", "90"))
            )
        except ValueError as error:
            raise QwenConfigurationError("QWEN_TIMEOUT_S must be numeric") from error
        if not 1 <= timeout_s <= 300:
            raise QwenConfigurationError("QWEN_TIMEOUT_S must be between 1 and 300")
        return cls(
            api_key=api_key,
            base_url=os.environ.get(
                "DASHSCOPE_BASE_URL", local_settings.get("DASHSCOPE_BASE_URL", DEFAULT_BASE_URL)
            ),
            model=os.environ.get("QWEN_MODEL", local_settings.get("QWEN_MODEL", DEFAULT_MODEL)),
            timeout_s=timeout_s,
        )

    def status(self) -> dict:
        return {
            "configured": True,
            "provider": "aliyun_model_studio",
            "protocol": "openai_compatible_chat_completions",
            "model": self.model,
            "base_url": self.base_url,
        }

    @property
    def endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def complete_structured(
        self,
        *,
        phase: str,
        system_prompt: str,
        user_context: Dict[str, Any],
        output_model: Type[BaseModel],
        schema_name: str,
    ) -> StructuredQwenCall:
        user_text = json.dumps(user_context, ensure_ascii=False, sort_keys=True)
        schema = output_model.model_json_schema()
        request_payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        request_bytes = json.dumps(
            request_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        started_at = _utc_iso()
        base_metadata = {
            "phase": phase,
            "provider": "aliyun_model_studio",
            "protocol": "openai_compatible_chat_completions",
            "model": self.model,
            "endpoint": self.endpoint,
            "started_at": started_at,
            "prompt_sha256": _sha256((system_prompt + "\n" + user_text).encode("utf-8")),
            "request_sha256": _sha256(request_bytes),
            "schema_name": schema_name,
            "schema_valid": False,
        }
        request = Request(
            self.endpoint,
            data=request_bytes,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        response_payload: Dict[str, Any] = {}
        try:
            with self._urlopen(request, timeout=self.timeout_s) as response:
                raw = response.read()
                response_headers = response.headers
            response_payload = json.loads(raw.decode("utf-8"))
            choices = response_payload.get("choices")
            if not isinstance(choices, list) or not choices:
                raise ValueError("Qwen response has no choices")
            content = choices[0].get("message", {}).get("content")
            if not isinstance(content, str):
                raise ValueError("Qwen response has no structured text content")
            parsed = output_model.model_validate_json(content)
        except HTTPError as error:
            try:
                response_payload = json.loads(error.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                response_payload = {"status": error.code, "reason": str(error)}
            metadata = {**base_metadata, "completed_at": _utc_iso(), "error": str(error)}
            raise QwenCallError(
                f"Qwen API returned HTTP {error.code}",
                request_payload=request_payload,
                response_payload=response_payload,
                metadata=metadata,
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            metadata = {**base_metadata, "completed_at": _utc_iso(), "error": str(error)}
            raise QwenCallError(
                f"Qwen API is unavailable: {error}",
                request_payload=request_payload,
                response_payload=response_payload,
                metadata=metadata,
            ) from error
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as error:
            metadata = {**base_metadata, "completed_at": _utc_iso(), "error": str(error)}
            raise QwenCallError(
                f"Qwen structured output is invalid: {error}",
                request_payload=request_payload,
                response_payload=response_payload,
                metadata=metadata,
            ) from error

        response_bytes = json.dumps(
            response_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        request_id = None
        for name in ("x-request-id", "x-dashscope-request-id", "request-id"):
            request_id = response_headers.get(name)
            if request_id:
                break
        metadata = {
            **base_metadata,
            "completed_at": _utc_iso(),
            "schema_valid": True,
            "completion_id": response_payload.get("id"),
            "request_id": request_id,
            "response_sha256": _sha256(response_bytes),
            "usage": response_payload.get("usage", {}),
        }
        return StructuredQwenCall(parsed, request_payload, response_payload, metadata)
