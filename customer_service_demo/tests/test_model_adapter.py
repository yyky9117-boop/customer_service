import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch as mock_patch

from runtime.errors import InvalidModelOutput, ServiceFailure
from runtime.model import DeepSeekModel, NoRedirect, load_env
from core_helpers import WorkspaceTemp


class Reply:
    def __init__(self, choice):
        self.choice = choice
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self, limit):
        return json.dumps({"choices": [self.choice]}).encode()


class ModelAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_token_budget_stops_before_network_request(self):
        model=DeepSeekModel(api_key='test_only_key');model.token_limit=1
        with mock_patch('runtime.model.urllib.request.build_opener') as opener:
            with self.assertRaises(ServiceFailure):await model.extract({})
            opener.assert_not_called()
        self.assertEqual(model.usage['calls'],0)

    async def test_reported_tokens_settle_reservation(self):
        class MeteredReply(Reply):
            def read(self, limit):
                return json.dumps({'choices':[self.choice],'usage':{'total_tokens':123}}).encode()
        model=DeepSeekModel(api_key='test_only_key')
        with mock_patch('runtime.model.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value=MeteredReply({'finish_reason':'stop','message':{'content':'{}'}})
            await model.extract({})
        self.assertEqual(model.usage['reported_total_tokens'],123)
        self.assertEqual(model.usage['charged_upper_bound'],123)
        self.assertEqual(model.usage['unreported_calls'],0)
    async def test_transient_connection_failure_gets_only_one_transport_retry(self):
        with mock_patch("runtime.model.urllib.request.build_opener") as opener, mock_patch("runtime.model.time.sleep"):
            opener.return_value.open.side_effect=[urllib.error.URLError(ConnectionRefusedError()),Reply({"finish_reason":"stop","message":{"content":"{}"}})]
            self.assertEqual(await DeepSeekModel(api_key="test_only_key").extract({}),{})
            self.assertEqual(opener.return_value.open.call_count,2)
            opener.return_value.open.reset_mock()
            opener.return_value.open.side_effect=urllib.error.URLError(ConnectionRefusedError())
            with self.assertRaises(ServiceFailure):await DeepSeekModel(api_key="test_only_key").extract({})
            self.assertEqual(opener.return_value.open.call_count,2)

    async def test_reasons_reply_and_review_use_distinct_prompts(self):
        model=DeepSeekModel(api_key="test_only_key")
        for method,marker in [(model.generate_reasons,"推荐理由生成器"),(model.compose_reply,"客服话术生成器"),(model.review_reply,"推荐回复质检器")]:
            with self.subTest(marker=marker), mock_patch("runtime.model.urllib.request.build_opener") as opener:
                opener.return_value.open.return_value=Reply({"finish_reason":"stop","message":{"content":"{}"}})
                await method({"plans":[]})
                body=json.loads(opener.return_value.open.call_args.args[0].data)
                self.assertIn(marker,body["messages"][0]["content"])
                self.assertNotIn("test_only_key",str(body))

    async def test_missing_key_is_explicit_failure(self):
        with self.assertRaises(ServiceFailure) as error:
            await DeepSeekModel(api_key="").extract({"message": "需求"})
        self.assertEqual(error.exception.code, "data_unavailable")

    async def test_http_payload_uses_real_json_mode_and_no_fallback(self):
        with mock_patch("runtime.model.urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = Reply({"finish_reason": "stop", "message": {"content": '{"intent":"recommend"}'}})
            result = await DeepSeekModel(api_key="test_only_key").extract({"message": "需求"})
            request = opener.return_value.open.call_args.args[0]
            body = json.loads(request.data)
            self.assertEqual(body["response_format"], {"type": "json_object"})
            self.assertEqual(body["messages"][0]["role"], "system")
            self.assertEqual(result["intent"], "recommend")
            self.assertNotIn("test_only_key", str(body))

    async def test_truncated_empty_and_non_json_outputs_rejected(self):
        for reason, content in [("length", "{}"), ("stop", ""), ("stop", "不是 JSON")]:
            with self.subTest(reason=reason, content=content), mock_patch("runtime.model.urllib.request.build_opener") as opener:
                opener.return_value.open.return_value = Reply({"finish_reason": reason, "message": {"content": content}})
                with self.assertRaises(InvalidModelOutput):
                    await DeepSeekModel(api_key="test_only_key").extract({})

    async def test_timeout_and_http_errors_sanitized(self):
        cases = [(TimeoutError(), "model_timeout", True),
            (urllib.error.HTTPError("https://api.deepseek.com", 401, "SECRET_RAW_BODY", {}, None), "data_unavailable", False),
            (urllib.error.HTTPError("https://api.deepseek.com", 429, "SECRET_RAW_BODY", {}, None), "data_unavailable", True)]
        for failure, code, retryable in cases:
            with self.subTest(code=code), mock_patch("runtime.model.urllib.request.build_opener") as opener:
                opener.return_value.open.side_effect = failure
                with self.assertRaises(ServiceFailure) as error:
                    await DeepSeekModel(api_key="test_only_key").extract({})
                self.assertEqual((error.exception.code, error.exception.retryable), (code, retryable))
                self.assertNotIn("SECRET_RAW_BODY", error.exception.public_message)
                self.assertNotIn("test_only_key", error.exception.public_message)

    def test_no_redirect_auth_forwarding(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.example"))

    def test_local_env_not_evaluated_and_existing_env_not_overwritten(self):
        with WorkspaceTemp() as folder, mock_patch.dict(os.environ, {"DEEPSEEK_MODEL": "existing_model"}):
            path = Path(folder) / ".env"
            path.write_text("DEEPSEEK_MODEL=replaced\nDEEPSEEK_API_KEY='$(not_executed)'\nUNRELATED_VARIABLE=bad\n", encoding="utf-8")
            load_env(path)
            self.assertEqual(os.environ["DEEPSEEK_MODEL"], "existing_model")
            self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "$(not_executed)")
            self.assertNotIn("UNRELATED_VARIABLE", os.environ)

    def test_key_destination_restricted_to_official_https(self):
        for url in ["http://api.deepseek.com", "https://api.deepseek.com.attacker.example", "https://attacker.example"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                DeepSeekModel(api_key="test_only_key", base_url=url)
