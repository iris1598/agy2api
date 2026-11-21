"""严格模式、按凭证限流与签名回注的单元测试。"""

import asyncio
import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import pytest  # noqa: E402

from src import thought_signature_cache  # noqa: E402
from src.credential_limiter import CredentialLimiter  # noqa: E402
from src.fidelity import FidelityError, validate_client_request  # noqa: E402
from src.request_pacer import RequestPacer  # noqa: E402

KNOWN_MODEL = "gemini-4-hydrogen-medium"


class TestModelEnumMap:
    """模型枚举表来自 2.34.0 fetchAvailableModels 真实响应。"""

    def test_placeholder_and_google_enums(self):
        from src.model_enum import has_model_enum, resolve_model_enum

        assert resolve_model_enum("gemini-4-hydrogen-medium") == "MODEL_PLACEHOLDER_M526"
        assert resolve_model_enum("gemini-4-neon-medium") == "MODEL_PLACEHOLDER_M533"
        assert resolve_model_enum("gemini-2.5-flash") == "MODEL_GOOGLE_GEMINI_2_5_FLASH"
        assert has_model_enum("gemini-2.5-flash")
        assert not has_model_enum("totally-unknown-model")

    def test_parse_ignores_model_provider(self):
        from src.model_enum import parse_model_enums

        data = {
            "models": {
                "gemini-2.5-flash": {
                    "model": "MODEL_GOOGLE_GEMINI_2_5_FLASH",
                    "modelProvider": "MODEL_PROVIDER_GOOGLE",
                }
            }
        }
        assert parse_model_enums(data) == {
            "gemini-2.5-flash": "MODEL_GOOGLE_GEMINI_2_5_FLASH"
        }


class TestFidelityError:
    def test_status_mapping(self):
        assert FidelityError("unknown_model_enum").status_code == 400
        assert FidelityError("tls_sidecar_unavailable").status_code == 503
        assert FidelityError("envelope_schema_mismatch").status_code == 500

    def test_payload_shape(self):
        payload = FidelityError("unsupported_client_field", "safetySettings").to_payload()
        assert payload["error"]["type"] == "fidelity_rejected"
        assert payload["error"]["reason"] == "unsupported_client_field"
        assert "safetySettings" in payload["error"]["detail"]


class TestValidateClientRequest:
    def test_accepts_whitelisted_fields(self):
        request = {"contents": [], "systemInstruction": {"parts": []},
                   "tools": [], "generationConfig": {}}
        validate_client_request(request, KNOWN_MODEL)

    def test_rejects_unknown_field(self):
        request = {"contents": [], "safetySettings": [{"category": "HARM"}]}
        with pytest.raises(FidelityError) as exc_info:
            validate_client_request(request, KNOWN_MODEL)
        assert exc_info.value.reason == "unsupported_client_field"

    def test_rejects_model_without_enum(self):
        with pytest.raises(FidelityError) as exc_info:
            validate_client_request({"contents": []}, "gemini-9-unknown")
        assert exc_info.value.reason == "unknown_model_enum"

    def test_injects_thinking_level_from_suffix(self):
        request = {"contents": []}
        validate_client_request(request, KNOWN_MODEL)
        thinking = request["generationConfig"]["thinkingConfig"]
        assert thinking["thinkingLevel"] == "MEDIUM"
        assert thinking["includeThoughts"] is True

    def test_conflicting_thinking_level_rejected(self):
        request = {
            "contents": [],
            "generationConfig": {"thinkingConfig": {"thinkingLevel": "HIGH"}},
        }
        with pytest.raises(FidelityError) as exc_info:
            validate_client_request(request, KNOWN_MODEL)
        assert exc_info.value.reason == "generation_config_mismatch"

    def test_does_not_mutate_caller_generation_config(self):
        original = {"thinkingConfig": {"thinkingLevel": "MEDIUM"}}
        request = {"contents": [], "generationConfig": original}
        validate_client_request(request, KNOWN_MODEL)
        assert request["generationConfig"] is not original
        assert original == {"thinkingConfig": {"thinkingLevel": "MEDIUM"}}


class TestThoughtSignatureCache:
    def setup_method(self):
        thought_signature_cache.clear()

    def test_replays_signature_for_same_text(self):
        contents = [
            {"role": "model", "parts": [{"text": "hello", "thoughtSignature": "sig-1"}]}
        ]
        thought_signature_cache.replay_into_contents(contents, "traj-1")
        # 下一轮里同一段文本没有签名
        next_turn = [{"role": "model", "parts": [{"text": "hello"}]}]
        restored = thought_signature_cache.replay_into_contents(next_turn, "traj-1")
        assert restored == 1
        assert next_turn[0]["parts"][0]["thoughtSignature"] == "sig-1"

    def test_isolated_by_trajectory(self):
        thought_signature_cache.replay_into_contents(
            [{"role": "model", "parts": [{"text": "x", "thoughtSignature": "sig"}]}],
            "traj-a",
        )
        other = [{"role": "model", "parts": [{"text": "x"}]}]
        assert thought_signature_cache.replay_into_contents(other, "traj-b") == 0
        assert "thoughtSignature" not in other[0]["parts"][0]


class TestCredentialLimiter:
    async def test_limits_per_credential(self, monkeypatch):
        async def _limit_one():
            return 1

        monkeypatch.setattr(
            "src.credential_limiter.get_credential_max_concurrent", _limit_one
        )
        limiter = CredentialLimiter()
        await limiter.acquire("cred-a")

        blocked = asyncio.create_task(limiter.acquire("cred-a"))
        await asyncio.sleep(0)
        assert not blocked.done(), "同一凭证第二个请求应排队"

        other = asyncio.create_task(limiter.acquire("cred-b"))
        await asyncio.wait_for(other, 1.0)

        limiter.release("cred-a")
        await asyncio.wait_for(blocked, 1.0)
        limiter.release("cred-a")
        limiter.release("cred-b")


class TestPerCredentialPacing:
    async def test_pace_for_isolated_between_credentials(self):
        pacer = RequestPacer()
        pacer._enabled = True
        pacer._serial_mode = False
        pacer._min_interval = 0.2
        pacer._jitter = 0.0
        pacer._last_refresh = time.monotonic()

        await pacer.pace_for("cred-a")
        started = time.monotonic()
        await pacer.pace_for("cred-b")
        assert time.monotonic() - started < 0.1, "不同凭证不应互相节流"

        started = time.monotonic()
        await pacer.pace_for("cred-a")
        assert time.monotonic() - started >= 0.15, "同一凭证应等待最小间隔"
