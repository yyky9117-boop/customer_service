"""真实 DeepSeek HTTP 接入；无密钥时明确报错，不替换为规则模型。"""
import asyncio
import json
import os
import socket
import time
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from .errors import InvalidModelOutput, ServiceFailure
from .needs import PATCH_SCHEMA

ROOT = Path(__file__).resolve().parents[1]


def load_env(path=ROOT / ".env"):
    """只读取简单 KEY=VALUE；不求值、不打印、不覆盖已有进程配置。"""
    if not Path(path).exists():
        return
    allowed = {"DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL", "DEEPSEEK_TIMEOUT_SECONDS"}
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() in allowed:
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # 防止认证头被第三方重定向带走。
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class DeepSeekModel:
    def __init__(self, api_key=None, base_url=None, model=None, timeout=None):
        self.api_key = api_key if api_key is not None else os.environ.get("DEEPSEEK_API_KEY", "")
        self.base_url = (base_url or os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")).rstrip("/")
        self.model = model or os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
        self.timeout = float(timeout or os.environ.get("DEEPSEEK_TIMEOUT_SECONDS", "45"))
        self._usage_lock = threading.Lock()
        self.usage = {"calls": 0, "reported_total_tokens": 0, "unreported_calls": 0, "reserved_upper_bound": 0, "charged_upper_bound": 0}
        # 单次服务运行的保守额度上限；本轮真实测试规模远低于该值。
        self.token_limit = 500_000
        if urlsplit(self.base_url).scheme != "https" or urlsplit(self.base_url).hostname != "api.deepseek.com":
            raise ValueError("此适配器只向官方 HTTPS DeepSeek 地址发送密钥")
        if not 1 <= self.timeout <= 120:
            raise ValueError("模型超时范围 1–120 秒")

    async def extract(self, payload, repair=None):
        if not self.api_key:
            raise ServiceFailure("data_unavailable", "尚未配置 DeepSeek 密钥，暂时无法理解需求。", False)
        return await asyncio.to_thread(self._call, payload, repair)

    def _call(self, payload, repair):
        system = (ROOT / "prompts" / "need_extraction.md").read_text(encoding="utf-8")
        system += "\nJSON Schema:\n" + json.dumps(PATCH_SCHEMA, ensure_ascii=False)
        return self._complete(system, payload, repair, 5000)

    async def generate_reasons(self, payload, repair=None):
        return await self._presentation_call("recommendation_reasons", payload, repair)

    async def compose_reply(self, payload, repair=None):
        return await self._presentation_call("customer_reply", payload, repair)

    async def review_reply(self, payload):
        return await self._presentation_call("reply_review", payload)

    async def _presentation_call(self, prompt, payload, repair=None):
        if not self.api_key:
            raise ServiceFailure("data_unavailable", "尚未配置 DeepSeek 密钥，暂时无法生成客服回复。", False)
        system = (ROOT / "prompts" / (prompt + ".md")).read_text(encoding="utf-8")
        return await asyncio.to_thread(self._complete, system, payload, repair, 3500)

    def _complete(self, system, payload, repair, max_tokens):
        messages = [{"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
        if repair:
            messages.append({"role": "user", "content": "上次输出未通过校验，请根据原始资料重写并修复这些问题：" + repair})
        body = {"model": self.model, "messages": messages, "response_format": {"type": "json_object"},
            "max_tokens": max_tokens, "stream": False, "thinking": {"type": "disabled"}}
        # UTF-8 字节数作输入 token 保守上界；为一次连接重试也预留完整额度。
        reserve = 2 * (len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) + max_tokens + 512)
        with self._usage_lock:
            if self.usage["charged_upper_bound"] + reserve > self.token_limit:
                raise ServiceFailure("data_unavailable", "本轮模型调用额度已达上限，请停止测试并检查用量。", False)
            self.usage["reserved_upper_bound"] += reserve
            self.usage["charged_upper_bound"] += reserve
        request = urllib.request.Request(self.base_url + "/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"}, method="POST")
        try:
            for attempt in range(2):
                try:
                    with urllib.request.build_opener(NoRedirect).open(request, timeout=self.timeout) as response:
                        envelope = json.loads(response.read(2_000_000).decode("utf-8"))
                    break
                except urllib.error.URLError as exc:
                    if isinstance(exc, urllib.error.HTTPError) or isinstance(exc.reason, (TimeoutError, socket.timeout)) or attempt:
                        raise
                    # 仅对连接类失败重试一次；认证和限流交给统一错误处理。
                    time.sleep(.3)
            choice = envelope["choices"][0]
            with self._usage_lock:
                self.usage["calls"] += 1
                reported = envelope.get("usage", {}).get("total_tokens")
                if type(reported) is int and reported >= 0:
                    self.usage["reported_total_tokens"] += reported
                    # 有供应商用量的成功请求按实际结算；连接重试前的未知请求保留上界。
                    self.usage["charged_upper_bound"] -= reserve - reported - (reserve // 2 if attempt else 0)
                else:
                    self.usage["unreported_calls"] += 1
            if choice.get("finish_reason") != "stop":
                raise InvalidModelOutput("模型输出未完整结束")
            content = choice["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise InvalidModelOutput("模型输出为空")
            return json.loads(content)
        except urllib.error.HTTPError as exc:
            # 不回传供应商原始响应、请求头或密钥。
            retryable = exc.code in {429, 500, 502, 503, 504}
            text = "模型服务暂时不可用，请稍后重试。" if retryable else "模型连接配置或授权未通过，请检查本地配置。"
            raise ServiceFailure("data_unavailable", text, retryable) from None
        except (TimeoutError, socket.timeout):
            raise ServiceFailure("model_timeout", "模型处理超时，请重试；本轮未保存。", True) from None
        except urllib.error.URLError:
            raise ServiceFailure("data_unavailable", "暂时无法连接模型服务，请稍后重试。", True) from None
        except (KeyError, IndexError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
            raise InvalidModelOutput("模型响应结构异常") from exc
