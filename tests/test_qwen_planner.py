import json
import os
import unittest
from urllib.error import URLError
from unittest.mock import patch

from roboscientist.ai import QwenCallError, QwenClient, QwenConfigurationError
from roboscientist.schemas import ScientificPlanDraft


VALID_PLAN = {
    "research_question": "Can signed grasp-offset compensation improve placement?",
    "hypothesis": "Compensating the measured residual will reduce placement error.",
    "controlled_variables": ["scene", "target pose", "destination pose"],
    "success_criteria": ["object placed", "position error below 5 mm"],
    "stop_conditions": ["safety rejection", "missing visual evidence"],
    "expected_observation": "P1 has lower position error than P0.",
}


class _FakeResponse:
    def __init__(self, payload, headers=None):
        self._payload = payload
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


class QwenPlannerTests(unittest.TestCase):
    def test_structured_output_is_strict_auditable_and_does_not_leak_api_key(self):
        secret = "sk-test-never-persist-this"
        captured = {}

        def fake_urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return _FakeResponse(
                {
                    "id": "chatcmpl-test-1",
                    "choices": [
                        {"message": {"content": json.dumps(VALID_PLAN)}}
                    ],
                    "usage": {
                        "prompt_tokens": 120,
                        "completion_tokens": 48,
                        "total_tokens": 168,
                    },
                },
                {"x-request-id": "req-test-1"},
            )

        client = QwenClient(
            api_key=secret,
            base_url="https://example.test/compatible-mode/v1",
            model="qwen-test",
            timeout_s=12,
            urlopen_func=fake_urlopen,
        )
        call = client.complete_structured(
            phase="scientific_planning",
            system_prompt="Return a scientific plan only.",
            user_context={"task": "pick and place a red cube"},
            output_model=ScientificPlanDraft,
            schema_name="scientific_plan",
        )

        sent = json.loads(captured["request"].data.decode("utf-8"))
        response_format = sent["response_format"]["json_schema"]
        self.assertTrue(response_format["strict"])
        self.assertEqual(response_format["name"], "scientific_plan")
        self.assertFalse(response_format["schema"]["additionalProperties"])
        self.assertEqual(call.output.research_question, VALID_PLAN["research_question"])
        self.assertEqual(call.metadata["model"], "qwen-test")
        self.assertEqual(call.metadata["request_id"], "req-test-1")
        self.assertEqual(call.metadata["completion_id"], "chatcmpl-test-1")
        self.assertTrue(call.metadata["schema_valid"])
        self.assertRegex(call.metadata["prompt_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(call.metadata["request_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(call.metadata["response_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(call.metadata["usage"]["total_tokens"], 168)
        self.assertEqual(captured["timeout"], 12)

        persistable_evidence = json.dumps(
            {
                "request": call.request_payload,
                "response": call.response_payload,
                "metadata": call.metadata,
            },
            sort_keys=True,
        )
        self.assertNotIn(secret, persistable_evidence)

    def test_schema_violation_is_an_explicit_call_failure(self):
        invalid_plan = {**VALID_PLAN, "unapproved_robot_command": "move_joint(1.0)"}

        def fake_urlopen(request, timeout):
            del request, timeout
            return _FakeResponse(
                {
                    "id": "chatcmpl-invalid",
                    "choices": [
                        {"message": {"content": json.dumps(invalid_plan)}}
                    ],
                }
            )

        client = QwenClient(api_key="secret", urlopen_func=fake_urlopen)
        with self.assertRaisesRegex(QwenCallError, "structured output is invalid") as raised:
            client.complete_structured(
                phase="scientific_planning",
                system_prompt="Return JSON.",
                user_context={"task": "pick cube"},
                output_model=ScientificPlanDraft,
                schema_name="scientific_plan",
            )

        self.assertFalse(raised.exception.metadata["schema_valid"])
        self.assertEqual(
            raised.exception.response_payload["id"], "chatcmpl-invalid"
        )

    def test_missing_key_is_explicit_when_qwen_is_required(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(QwenClient.from_env(required=False))
            with self.assertRaisesRegex(
                QwenConfigurationError, "DASHSCOPE_API_KEY is not configured"
            ):
                QwenClient.from_env(required=True)

    def test_transport_failure_preserves_auditable_error_without_secret(self):
        secret = "sk-test-transport-secret"

        def unavailable(request, timeout):
            del request, timeout
            raise URLError("temporary DNS failure")

        client = QwenClient(api_key=secret, urlopen_func=unavailable)
        with self.assertRaisesRegex(QwenCallError, "API is unavailable") as raised:
            client.complete_structured(
                phase="scientific_planning",
                system_prompt="Return JSON.",
                user_context={"task": "pick cube"},
                output_model=ScientificPlanDraft,
                schema_name="scientific_plan",
            )

        error = raised.exception
        self.assertFalse(error.metadata["schema_valid"])
        self.assertRegex(error.metadata["request_sha256"], r"^[0-9a-f]{64}$")
        persisted_error = json.dumps(
            {
                "request": error.request_payload,
                "response": error.response_payload,
                "metadata": error.metadata,
            },
            sort_keys=True,
        )
        self.assertNotIn(secret, persisted_error)


if __name__ == "__main__":
    unittest.main()
